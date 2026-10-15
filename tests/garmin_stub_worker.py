"""Stand-in for the garmin_mcp worker in tests/test_stateless_worker.py.

Same contract as garmin_mcp ≥ e79df6f — env (GARMIN_MCP_HOST/PORT, GARMINTOKENS,
GARMIN_MCP_STATELESS_HTTP), FastMCP over streamable-http on the same MCP SDK,
/healthz + /readyz — but no Garmin: its one tool answers with the account
named in the token file the gateway materialized, so a test can tell which
account's worker served a call. Never imports garmin_mcp (CLAUDE.md).
"""
import json
import os

from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse, PlainTextResponse


def main() -> None:
    tokens = os.path.join(os.environ["GARMINTOKENS"], "garmin_tokens.json")
    app = FastMCP("garmin-stub", host=os.environ["GARMIN_MCP_HOST"],
                  port=int(os.environ["GARMIN_MCP_PORT"]),
                  stateless_http=os.environ["GARMIN_MCP_STATELESS_HTTP"] == "true")

    @app.tool()
    def whoami() -> str:
        """The synthetic account this worker serves."""
        with open(tokens, encoding="utf-8") as f:
            return json.load(f)["owner"]

    @app.custom_route("/healthz", methods=["GET"])
    async def healthz(_request):
        return PlainTextResponse("ok")

    @app.custom_route("/readyz", methods=["GET"])
    async def readyz(_request):
        return JSONResponse({"status": "ready"})

    app.run(transport="streamable-http")


if __name__ == "__main__":
    main()
