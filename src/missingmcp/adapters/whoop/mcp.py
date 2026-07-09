"""The /whoop/mcp server itself: a hand-rolled, stateless, tools-only MCP —
JSON-RPC over streamable HTTP, single (non-batch) requests, application/json
responses, no sessions. Claude is the only targeted client; the surface is
initialize / notifications/* / tools/list / tools/call / ping. Tool payloads
are WHOOP's v2 JSON passed through verbatim as text content."""
from __future__ import annotations
import json
from urllib.parse import quote
import httpx
from ..base import SessionExpired
from .api import WhoopApi, WhoopAuthError

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "missingmcp-whoop", "version": "1.0.0"}

_EMPTY_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


def _collection_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "start": {"type": "string",
                      "description": "Only records from this ISO 8601 time on (inclusive), e.g. 2026-07-01T00:00:00.000Z"},
            "end": {"type": "string",
                    "description": "Only records before this ISO 8601 time (exclusive); defaults to now"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 25,
                      "description": "Records per page (default 10, max 25)"},
            "next_token": {"type": "string",
                           "description": "Pagination token from the previous response's next_token"},
        },
        "additionalProperties": False,
    }


def _id_schema(desc: str) -> dict:
    return {"type": "object",
            "properties": {"id": {"type": "string", "description": desc}},
            "required": ["id"], "additionalProperties": False}


def _collection_params(args: dict) -> dict:
    params = {}
    if args.get("start"):
        params["start"] = args["start"]
    if args.get("end"):
        params["end"] = args["end"]
    if args.get("limit"):
        params["limit"] = int(args["limit"])
    if args.get("next_token"):
        params["nextToken"] = args["next_token"]   # camelCase on the wire (WHOOP spec)
    return params


def _plain(path: str):
    return lambda args: (path, {})


def _collection(path: str):
    return lambda args: (path, _collection_params(args))


def _by_id(path_tpl: str):
    return lambda args: (path_tpl.format(id=quote(str(args["id"]), safe="")), {})


# (name, description, input schema, resolve(args) -> (path, query)).
# scripts/gen_whoop_tools.py renders the landing page's tool list from this.
TOOLS = [
    ("get_profile", "The connected user's WHOOP profile: name and account email.",
     _EMPTY_SCHEMA, _plain("/v2/user/profile/basic")),
    ("get_body_measurements", "Height, weight, and max heart rate on record.",
     _EMPTY_SCHEMA, _plain("/v2/user/measurement/body")),
    ("get_cycles", "Physiological (day) cycles: strain, average/max heart rate, energy burned. Paginated.",
     _collection_schema(), _collection("/v2/cycle")),
    ("get_recoveries", "Recovery scores: recovery %, HRV (rmssd), resting heart rate, SpO2, skin temp. Paginated.",
     _collection_schema(), _collection("/v2/recovery")),
    ("get_sleeps", "Sleep activities: stages, time in bed, efficiency, respiratory rate, sleep need. Paginated.",
     _collection_schema(), _collection("/v2/activity/sleep")),
    ("get_sleep", "One sleep activity by its UUID.",
     _id_schema("Sleep UUID (from get_sleeps)"), _by_id("/v2/activity/sleep/{id}")),
    ("get_workouts", "Workouts: sport, strain, heart rate, energy, distance where available. Paginated.",
     _collection_schema(), _collection("/v2/activity/workout")),
    ("get_workout", "One workout by its UUID.",
     _id_schema("Workout UUID (from get_workouts)"), _by_id("/v2/activity/workout/{id}")),
]


def _http_json(status: int, obj) -> "tuple[int, dict, bytes]":
    return status, {"Content-Type": "application/json"}, json.dumps(obj).encode()


def _result(rid, result) -> "tuple[int, dict, bytes]":
    return _http_json(200, {"jsonrpc": "2.0", "id": rid, "result": result})


def _rpc_error(rid, code: int, message: str) -> "tuple[int, dict, bytes]":
    return _http_json(200, {"jsonrpc": "2.0", "id": rid,
                            "error": {"code": code, "message": message}})


def _tool_error(rid, message: str) -> "tuple[int, dict, bytes]":
    # MCP tool-level failure: a *result* with isError, not a protocol error.
    return _result(rid, {"content": [{"type": "text", "text": message}],
                         "isError": True})


class WhoopLocalForward:
    """LocalForward strategy C for whoop: the whole MCP server, in-process."""

    def __init__(self, config):
        self.api = WhoopApi(config)

    async def handle(self, conn, account_key: str, blob: str,
                     body: bytes) -> "tuple[int, dict, bytes]":
        try:
            req = json.loads(body)
        except (ValueError, TypeError):
            req = None
        if not isinstance(req, dict):          # garbage or JSON-RPC batch
            return _http_json(400, {"error": "invalid_request"})
        method = req.get("method", "")
        rid = req.get("id")
        if rid is None:                        # notification: acknowledge, no body
            return 202, {"Content-Type": "application/json"}, b""
        if method == "initialize":
            params = req.get("params")
            params = params if isinstance(params, dict) else {}
            client_ver = params.get("protocolVersion", "")
            ver = client_ver if client_ver in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return _result(rid, {"protocolVersion": ver,
                                 "capabilities": {"tools": {}},
                                 "serverInfo": SERVER_INFO})
        if method == "ping":
            return _result(rid, {})
        if method == "tools/list":
            return _result(rid, {"tools": [
                {"name": name, "description": desc, "inputSchema": schema}
                for name, desc, schema, _resolve in TOOLS]})
        if method == "tools/call":
            params = req.get("params")
            if not isinstance(params, dict):
                return _rpc_error(rid, -32602, "params must be an object")
            return await self._call(conn, account_key, blob, rid, params)
        return _rpc_error(rid, -32601, f"Method not found: {method}")

    async def _call(self, conn, account_key, blob, rid, params):
        name = params.get("name", "")
        tool = next((t for t in TOOLS if t[0] == name), None)
        if tool is None:
            return _rpc_error(rid, -32602, f"Unknown tool: {name}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _rpc_error(rid, -32602, "arguments must be an object")
        try:
            path, query = tool[3](args)
        except (KeyError, ValueError, TypeError, OverflowError) as e:
            # Any failure coercing client-supplied arguments is a client error
            # (-32602 / tool error), never an escaped 502. OverflowError covers
            # JSON's Infinity/-Infinity reaching int().
            return _tool_error(rid, f"Invalid or missing argument: {e}")
        try:
            status, payload = await self.api.get(conn, account_key,
                                                 json.loads(blob), path, query)
        except WhoopAuthError as e:
            raise SessionExpired(str(e)) from e
        except httpx.HTTPError:
            return _tool_error(rid, "WHOOP could not be reached — try again shortly.")
        if status == 429:
            return _tool_error(rid, "WHOOP rate limit hit — try again in a minute.")
        if status >= 400:
            return _tool_error(rid, f"WHOOP returned an error (HTTP {status}).")
        return _result(rid, {"content": [{"type": "text",
                                          "text": json.dumps(payload)}],
                             "isError": False})
