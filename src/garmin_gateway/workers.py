from __future__ import annotations
import asyncio
import json
import os
import re
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass
import httpx
from .log import log, log_exc

_SAFE = re.compile(r"[^A-Za-z0-9_.@-]")


class WorkerStartError(Exception):
    pass


@dataclass
class WorkerHandle:
    key: str
    port: int
    process: object
    last_active: float


class WorkerManager:
    def __init__(self, config, spawn=None, clock=time.monotonic):
        self._cfg = config
        self._clock = clock
        self._spawn_fn = spawn or self._default_spawn
        self._workers: dict[str, WorkerHandle] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # --- public ---------------------------------------------------------

    async def ensure_worker(self, key: str, tokens_json: str) -> int:
        async with self._locks[key]:
            h = self._workers.get(key)
            if h is not None and h.process.poll() is None and await self._healthy(h.port):
                h.last_active = self._clock()
                return h.port
            if h is not None:
                self._terminate(h)
            self._enforce_cap()
            token_dir = self._materialize_tokens(key, tokens_json)
            port = self._alloc_port()
            log("worker-spawn", port=port, cmd=" ".join(self._cfg.garmin_mcp_cmd),
                token_dir=token_dir)
            try:
                proc = self._spawn_fn(key, port, token_dir)
            except Exception as e:  # noqa: BLE001 - spawn failed (e.g. binary not on PATH)
                log_exc("worker-spawn-failed", e, error=str(e),
                        cmd=" ".join(self._cfg.garmin_mcp_cmd))
                raise WorkerStartError(f"spawn failed: {type(e).__name__}") from e
            if not await self._wait_healthy(port, proc):
                rc = proc.poll()
                log("worker-unhealthy", port=port, returncode=rc,
                    startup_timeout=self._cfg.worker_startup_timeout)
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
                raise WorkerStartError(f"worker for {key[:3]}*** failed to become healthy")
            self._workers[key] = WorkerHandle(key, port, proc, self._clock())
            log("worker-started", port=port)
            self.write_snapshot()
            return port

    async def reap_idle(self) -> None:
        now = self._clock()
        reaped = False
        for key, h in list(self._workers.items()):
            if now - h.last_active > self._cfg.worker_idle_ttl or h.process.poll() is not None:
                self._terminate(h)
                self._workers.pop(key, None)
                log("worker-reaped", port=h.port)
                reaped = True
        if reaped:
            self.write_snapshot()

    def active_count(self) -> int:
        """Number of per-user workers currently running (for monitoring)."""
        return len(self._workers)

    def snapshot(self) -> list[dict]:
        """Per-worker state for monitoring: account, port, pid, alive, idle secs."""
        now = self._clock()
        return [
            {
                "key": h.key,
                "port": h.port,
                "pid": getattr(h.process, "pid", None),
                "alive": h.process.poll() is None,
                "idle_seconds": round(now - h.last_active, 1),
            }
            for h in self._workers.values()
        ]

    def write_snapshot(self) -> None:
        """Persist worker state to DATA_DIR/workers.json (atomic) for monitoring
        (scripts/status.py). Best-effort — never raises into the caller."""
        path = os.path.join(self._cfg.data_dir, "workers.json")
        data = {"updated": time.strftime("%H:%M:%S"), "workers": self.snapshot()}
        tmp = f"{path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, path)
        except OSError:
            pass

    def shutdown(self) -> None:
        for h in list(self._workers.values()):
            self._terminate(h)
        self._workers.clear()
        self.write_snapshot()

    # --- internals ------------------------------------------------------

    def _enforce_cap(self) -> None:
        while len(self._workers) >= self._cfg.max_workers:
            oldest = min(self._workers.values(), key=lambda h: h.last_active)
            self._terminate(oldest)
            self._workers.pop(oldest.key, None)
            log("worker-evicted", port=oldest.port)

    def _materialize_tokens(self, key: str, tokens_json: str) -> str:
        safe = _SAFE.sub("_", key)
        user_dir = os.path.join(self._cfg.data_dir, "users", safe)
        token_dir = os.path.join(user_dir, "tokens")
        os.makedirs(token_dir, exist_ok=True)
        os.chmod(user_dir, 0o700)
        os.chmod(token_dir, 0o700)
        path = os.path.join(token_dir, "garmin_tokens.json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(tokens_json)
        return token_dir

    def _alloc_port(self) -> int:
        used = {h.port for h in self._workers.values()}
        for p in range(self._cfg.worker_port_start, self._cfg.worker_port_end + 1):
            if p not in used:
                return p
        raise WorkerStartError("no free worker port")

    def _default_spawn(self, key: str, port: int, token_dir: str):
        env = dict(os.environ)
        env.update({
            "GARMIN_MCP_TRANSPORT": "streamable-http",
            "GARMIN_MCP_HOST": "127.0.0.1",
            "GARMIN_MCP_PORT": str(port),
            "GARMINTOKENS": token_dir,
        })
        return subprocess.Popen(self._cfg.garmin_mcp_cmd, env=env)

    def _terminate(self, h: WorkerHandle) -> None:
        try:
            if h.process.poll() is None:
                h.process.terminate()
        except Exception:  # noqa: BLE001
            pass

    async def _healthy(self, port: int) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as c:
                r = await c.get(f"http://127.0.0.1:{port}/healthz")
                return r.status_code == 200
        except (httpx.HTTPError, OSError):
            return False

    async def _wait_healthy(self, port: int, proc) -> bool:
        deadline = self._clock() + self._cfg.worker_startup_timeout
        while self._clock() < deadline:
            if proc.poll() is not None:
                return False
            if await self._healthy(port):
                return True
            await asyncio.sleep(0.25)
        return False
