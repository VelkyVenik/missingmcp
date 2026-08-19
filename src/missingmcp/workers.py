from __future__ import annotations
import asyncio
import json
import os
import re
import subprocess
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
import httpx
from .log import log, log_error, log_exc

_SAFE = re.compile(r"[^A-Za-z0-9_.@-]")
# Worker lines that indicate a real problem get error severity so Railway
# surfaces them; everything else is info. Deliberately loose — false negatives
# just stay info-level and remain searchable.
_WORKER_ERROR = re.compile(r"\b(ERROR|CRITICAL|Traceback|Exception)\b")
# The one deliberate exception to that loose filter: the worker's uvicorn
# prints this on every routine MCP session teardown (the client hung up its
# open listen stream, the gateway stopped reading) — not a fault, and at
# production volume it alone kept the pager loud (reliability ticket 10).
_WORKER_ROUTINE = "ASGI callable returned without completing response"


def _pump_worker_output(stream, account: str) -> None:
    """Forward a worker's merged stdout/stderr line-by-line into the structured
    log (event `worker-log`, filterable by account in Railway). Runs on a daemon
    thread until the pipe closes; replaces the old per-user worker.log files on
    the volume (unbounded, only reachable over ssh)."""
    try:
        for raw in stream:
            line = raw.rstrip()
            if not line:
                continue
            elevated = _WORKER_ERROR.search(line) and _WORKER_ROUTINE not in line
            emit = log_error if elevated else log
            emit("worker-log", account=account, line=line)
    except Exception:  # noqa: BLE001 - a logging pump must never take anything down
        pass
    finally:
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass


class WorkerStartError(Exception):
    """The worker could not be brought up and an operator may need to look:
    the spawn itself failed, no port was free, or the process stayed alive but
    never answered /healthz within `worker_startup_timeout`."""


class WorkerCredentialsRejected(WorkerStartError):
    """The worker came up, decided it can't serve this account, and exited
    *cleanly* (rc 0) during startup — for `garmin_mcp` that's "OAuth tokens not
    found ... Exiting." once the stored tokens go stale. Expected and
    self-healing: the account needs a fresh sign-in, not an operator. Kept a
    subclass of WorkerStartError so any `except WorkerStartError` still catches
    it and the caller's re-auth handling stays a single path.

    A non-zero or signalled exit is deliberately NOT this — that's a crash, and
    it stays a plain WorkerStartError so it keeps reaching the ops alert."""


@dataclass
class WorkerHandle:
    key: str
    port: int
    process: object
    last_active: float
    inflight: int = 0          # proxied requests currently streaming through


class WorkerManager:
    def __init__(self, config, forward, spawn=None, clock=time.monotonic, persist=None):
        self._cfg = config
        self._forward = forward
        self._clock = clock
        self._spawn_fn = spawn or self._default_spawn
        self._persist = persist            # (key, blob) -> None; writes the store
        self._workers: dict[str, WorkerHandle] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._reserved: set[int] = set()   # ports being spawned but not yet registered
        # Last blob known to be in the store, per account — the baseline a
        # worker-rewritten token file is compared against. Process-local: after
        # a restart the first materialize re-seeds it from the store's blob.
        self._persisted: dict[str, str] = {}

    # --- public ---------------------------------------------------------

    async def ensure_worker(self, key: str, blob: str) -> int:
        async with self._locks[key]:
            h = self._workers.get(key)
            if h is not None and h.process.poll() is None:
                # Hold the worker busy across the awaited /healthz probe so a
                # concurrent reap_idle / _enforce_cap (neither takes this key's
                # lock) can't evict it during the yield (TOCTOU). Always released
                # in `finally`, so no accounting is leaked.
                h.inflight += 1
                try:
                    healthy = await self._healthy(h.port)
                finally:
                    h.inflight -= 1
                # Reuse when healthy, OR when a request is still streaming through
                # it: a momentarily slow /healthz on a busy worker must not kill
                # the live stream (mirrors the inflight guard in reap_idle /
                # _enforce_cap). Only an idle *and* unhealthy worker is replaced.
                if healthy or h.inflight > 0:
                    h.last_active = self._clock()
                    return h.port
            if h is not None:
                self._terminate(h)
                self._workers.pop(key, None)
            self._enforce_cap()
            # The previous worker may have rotated its tokens after the caller
            # read `blob` from the store (dead worker, or a replace) — persist
            # the rotation and materialize IT; writing the stale argument would
            # replay a token the upstream already retired.
            rotated = self._read_back_and_persist(key, "respawn")
            if rotated is not None:
                blob = rotated
            token_dir = self._materialize(key, blob)
            # Reserve the port across the awaited spawn/health-check. Without this,
            # a concurrent ensure_worker for a *different* key (own lock) would see
            # the same lowest free port — _alloc_port reads _workers, which isn't
            # updated until after the awaits below — and collide (EADDRINUSE).
            port = self._alloc_port()
            self._reserved.add(port)
            try:
                t0 = self._clock()
                log("worker-spawn", port=port, account=key,
                    cmd=" ".join(self._forward.command()), token_dir=token_dir)
                try:
                    proc = self._spawn_fn(key, port, token_dir)
                except Exception as e:  # noqa: BLE001 - spawn failed (e.g. binary not on PATH)
                    log_exc("worker-spawn-failed", e, error=str(e),
                            cmd=" ".join(self._forward.command()))
                    raise WorkerStartError(f"spawn failed: {type(e).__name__}") from e
                outcome = await self._wait_healthy(port, proc)
                if outcome != "healthy":
                    rc = proc.poll()
                    try:
                        proc.terminate()
                    except Exception:  # noqa: BLE001
                        pass
                    # Two very different failures used to share one event (and one
                    # error-level alert): a worker that quit by itself because the
                    # account's credentials are stale — routine, the user fixes it
                    # by signing in again — and a worker that broke. Keep them apart
                    # so the noisy one can't drown out the one worth waking up for.
                    #
                    # Only a CLEAN exit is the routine one: garmin_mcp prints
                    # "OAuth tokens not found ... Exiting." and returns 0. A non-zero
                    # or signalled exit (crash, rc=137 OOM kill, segfault) is a real
                    # fault and must stay loud — filing it as stale credentials would
                    # hide exactly the outage this split exists to surface.
                    if outcome == "exited" and rc == 0:
                        log("worker-exited-early", port=port, returncode=rc, account=key)
                        raise WorkerCredentialsRejected(
                            f"worker for {key[:3]}*** exited during startup (rc={rc})")
                    if outcome == "exited":
                        log("worker-died", port=port, returncode=rc, account=key)
                        raise WorkerStartError(
                            f"worker for {key[:3]}*** died during startup (rc={rc})")
                    log("worker-unhealthy", port=port, returncode=rc,
                        startup_timeout=self._cfg.worker_startup_timeout)
                    raise WorkerStartError(f"worker for {key[:3]}*** failed to become healthy")
                self._workers[key] = WorkerHandle(key, port, proc, self._clock())
                log("worker-started", port=port, account=key,
                    ms=int((self._clock() - t0) * 1000))
                self.write_snapshot()
                return port
            finally:
                self._reserved.discard(port)

    async def persist_rotated(self) -> None:
        """Capture worker-written token rotations into the store — the periodic
        tick of the read-back path (driven from the lifespan loop, like
        reap_idle). Takes the same per-account lock ensure_worker holds so a
        read can't interleave with a materialize; a held lock is skipped, not
        awaited — a spawn in progress does its own read-back."""
        for key in list(self._workers):
            lock = self._locks[key]
            if lock.locked():
                continue
            async with lock:
                if key in self._workers:
                    self._read_back_and_persist(key, "periodic")

    async def reap_idle(self) -> None:
        now = self._clock()
        reaped = False
        for key, h in list(self._workers.items()):
            dead = h.process.poll() is not None
            idle_expired = now - h.last_active > self._cfg.worker_idle_ttl
            # Reap dead processes always; reap idle ones only when no request is
            # streaming through them.
            if dead or (idle_expired and h.inflight == 0):
                self._terminate(h)
                self._workers.pop(key, None)
                # Capture any rotation written since the last tick — after the
                # pop no periodic tick sees this account again. Skip (don't
                # await) a held lock: reap_idle may run inside ensure_worker's
                # own critical section, and that path reads the file itself.
                if not self._locks[key].locked():
                    self._read_back_and_persist(key, "reap")
                log("worker-reaped", port=h.port, account=h.key)
                reaped = True
        if reaped:
            self.write_snapshot()

    def active_count(self) -> int:
        """Number of per-user workers currently running (for monitoring)."""
        return len(self._workers)

    def request_started(self, key: str) -> None:
        """Mark a proxied request in-flight for a worker so reap/evict won't kill
        it mid-stream; also refreshes its activity timestamp."""
        h = self._workers.get(key)
        if h is not None:
            h.inflight += 1
            h.last_active = self._clock()

    def request_finished(self, key: str) -> None:
        h = self._workers.get(key)
        if h is not None:
            h.inflight = max(0, h.inflight - 1)
            h.last_active = self._clock()

    def snapshot(self) -> list[dict]:
        """Per-worker state for monitoring: account, port, pid, alive, idle secs."""
        now = self._clock()
        return [
            {
                "key": h.key,
                "port": h.port,
                "pid": getattr(h.process, "pid", None),
                "alive": h.process.poll() is None,
                "inflight": h.inflight,
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
            # Last chance before the restart: an uncaptured rotation would make
            # the next boot materialize a spent token from the store. No lock
            # guard — the server is past accepting requests, and a concurrent
            # materialize would only make this a no-op (file == store blob).
            self._read_back_and_persist(h.key, "shutdown")
        self._workers.clear()
        self.write_snapshot()

    # --- internals ------------------------------------------------------

    def _enforce_cap(self) -> None:
        # Count in-flight spawns (ports reserved but not yet registered in
        # _workers) toward the cap — mirrors _alloc_port's union of _reserved —
        # so concurrent distinct-key spawns can't overshoot MAX_WORKERS.
        while len(self._workers) + len(self._reserved) >= self._cfg.max_workers:
            idle = [h for h in self._workers.values() if h.inflight == 0]
            if not idle:
                # Every worker is mid-request; evicting one would abort a live
                # stream. Let the pool exceed the cap transiently instead.
                log("worker-cap-all-busy", workers=len(self._workers))
                break
            oldest = min(idle, key=lambda h: h.last_active)
            self._terminate(oldest)
            self._workers.pop(oldest.key, None)
            # Same as the reap hook: capture the evictee's last rotation before
            # it leaves the registry; skip when its lock is held (that spawn
            # reads the file itself).
            if not self._locks[oldest.key].locked():
                self._read_back_and_persist(oldest.key, "evict")
            log("worker-evicted", port=oldest.port, account=oldest.key)

    def _workdir(self, key: str) -> str:
        safe = _SAFE.sub("_", key)
        return os.path.join(self._cfg.data_dir, "users", safe, "tokens")

    def _materialize(self, key: str, blob: str) -> str:
        workdir = self._workdir(key)
        user_dir = os.path.dirname(workdir)
        os.makedirs(workdir, exist_ok=True)
        os.chmod(user_dir, 0o700)
        os.chmod(workdir, 0o700)
        self._forward.materialize(blob, workdir)
        self._persisted[key] = blob        # the file now mirrors the store
        return workdir

    def _read_back_and_persist(self, key: str, trigger: str) -> str | None:
        """Persist the worker-rewritten token file to the store when it differs
        from the last store state this process knows (the WHOOP persist-before-use
        rule, worker edition). Returns the captured content, else None. With no
        known baseline (fresh process) it does nothing: a differing file may be
        OLDER than a re-login that just reached the store, so the store wins —
        repairing pre-fix drift is the explicit backfill's job, never this path's."""
        read_back = getattr(self._forward, "read_back", None)
        if self._persist is None or read_back is None:
            return None
        last = self._persisted.get(key)
        if last is None:
            return None
        try:
            content = read_back(self._workdir(key))
        except Exception as e:  # noqa: BLE001 - callers are batch contexts (tick, evict inside
            # another account's spawn, shutdown): one account's disk problem is
            # logged and skipped, never propagated into the batch.
            log_exc("worker-tokens-persist-failed", e, account=key,
                    trigger=trigger, error=str(e))
            return None
        if content is None or content == last:
            return None
        try:
            self._persist(key, content)
        except Exception as e:  # noqa: BLE001 - store hiccup: keep the baseline, retry next tick
            log_exc("worker-tokens-persist-failed", e, account=key,
                    trigger=trigger, error=str(e))
            return None
        self._persisted[key] = content
        log("worker-tokens-persisted", account=key, trigger=trigger)
        return content

    def _alloc_port(self) -> int:
        used = {h.port for h in self._workers.values()} | self._reserved
        for p in range(self._cfg.worker_port_start, self._cfg.worker_port_end + 1):
            if p not in used:
                return p
        raise WorkerStartError("no free worker port")

    def _default_spawn(self, key: str, port: int, workdir: str):
        env = dict(os.environ)
        env.update(self._forward.env(port, workdir))
        proc = subprocess.Popen(self._forward.command(), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", bufsize=1)
        threading.Thread(target=_pump_worker_output, args=(proc.stdout, key),
                         name=f"worker-log-{key[:8]}", daemon=True).start()
        return proc

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

    async def _wait_healthy(self, port: int, proc) -> str:
        """Poll /healthz until the worker answers, dies, or the deadline passes.
        Returns *why* it stopped waiting — `healthy`, `exited` (the process is
        gone, so waiting longer is pointless) or `timeout` (still running, still
        silent) — because the caller reports those as different failures."""
        deadline = self._clock() + self._cfg.worker_startup_timeout
        while self._clock() < deadline:
            if proc.poll() is not None:
                return "exited"
            if await self._healthy(port):
                return "healthy"
            await asyncio.sleep(0.25)
        return "timeout"
