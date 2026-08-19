import os
import stat
import time
import pytest
from missingmcp import workers
from missingmcp.adapters.garmin import GarminWorkerForward
from missingmcp.config import load_config


def _config(tmp_path, **over):
    env = {"GATEWAY_SECRET": "s" * 40, "DATA_DIR": str(tmp_path), "PUBLIC_URL": "https://x"}
    env.update({k.upper(): str(v) for k, v in over.items()})
    return load_config(env)


async def test_ensure_spawns_and_reuses(tmp_path, fake_worker):
    spawned = []

    class FakeProc:
        def __init__(self): self._alive = True
        def poll(self): return None if self._alive else 0
        def terminate(self): self._alive = False

    def spawn(key, port, token_dir):
        spawned.append((key, port, token_dir))
        return FakeProc()

    cfg = _config(tmp_path, worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=spawn)
    port1 = await mgr.ensure_worker("me@x.cz", '{"t":1}')
    assert port1 == fake_worker.port
    port2 = await mgr.ensure_worker("me@x.cz", '{"t":1}')
    assert port2 == fake_worker.port
    assert len(spawned) == 1                      # reused, not respawned
    # tokens were materialized
    assert (tmp_path / "users").exists()
    mgr.shutdown()


async def test_ensure_raises_when_never_healthy(tmp_path):
    class DeadProc:
        def poll(self): return 1                  # already exited
        def terminate(self): pass

    cfg = _config(tmp_path, worker_startup_timeout=1, worker_port_start=59999, worker_port_end=59999)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: DeadProc())
    with pytest.raises(workers.WorkerStartError):
        await mgr.ensure_worker("me@x.cz", "{}")


async def test_self_exit_during_startup_is_credentials_rejected(tmp_path):
    # A worker that quits by itself has judged the account unserviceable — for
    # garmin_mcp, stale tokens ("OAuth tokens not found ... Exiting.", rc 0). That's
    # the user's re-sign-in, not an operator's incident, so it must be its own
    # exception type and must not wait out the whole startup timeout.
    class SelfExitedProc:
        def poll(self): return 0                  # clean exit, exactly what garmin_mcp does
        def terminate(self): pass

    cfg = _config(tmp_path, worker_startup_timeout=30, worker_port_start=59995, worker_port_end=59995)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: SelfExitedProc())
    t0 = time.monotonic()
    with pytest.raises(workers.WorkerCredentialsRejected):
        await mgr.ensure_worker("me@x.cz", "{}")
    assert time.monotonic() - t0 < 5              # gave up on exit, didn't sit out the 30s


@pytest.mark.parametrize("rc", [1, 137, -11])
async def test_crashed_worker_is_not_filed_as_stale_credentials(tmp_path, rc):
    # A worker that dies non-zero has crashed — rc 1 on a traceback, 137 on an OOM
    # kill, negative on a signal. Filing those as stale credentials would silence
    # the ops alert on a real outage (e.g. a bad GARMIN_MCP_REF bump crashing every
    # worker), which is the opposite of what the event split is for.
    class CrashedProc:
        def poll(self): return rc
        def terminate(self): pass

    cfg = _config(tmp_path, worker_startup_timeout=30, worker_port_start=59993, worker_port_end=59993)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: CrashedProc())
    with pytest.raises(workers.WorkerStartError) as exc:
        await mgr.ensure_worker("me@x.cz", "{}")
    assert not isinstance(exc.value, workers.WorkerCredentialsRejected)
    assert str(rc) in str(exc.value)              # the code is in the message, for triage


async def test_hanging_worker_is_a_plain_start_error(tmp_path):
    # Alive but silent on /healthz is the other failure: something is genuinely
    # wrong with the worker, and it must NOT be filed as stale credentials.
    class AliveProc:
        def poll(self): return None               # still running, never answers
        def terminate(self): pass

    cfg = _config(tmp_path, worker_startup_timeout=1, worker_port_start=59994, worker_port_end=59994)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: AliveProc())
    with pytest.raises(workers.WorkerStartError) as exc:
        await mgr.ensure_worker("me@x.cz", "{}")
    assert not isinstance(exc.value, workers.WorkerCredentialsRejected)


async def test_reap_idle_terminates(tmp_path, fake_worker):
    clock = [1000.0]

    class FakeProc:
        def __init__(self): self.alive = True
        def poll(self): return None if self.alive else 0
        def terminate(self): self.alive = False

    proc = FakeProc()
    cfg = _config(tmp_path, worker_idle_ttl=10,
                  worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: proc, clock=lambda: clock[0])
    await mgr.ensure_worker("me@x.cz", "{}")
    clock[0] = 1100.0                              # advance past idle ttl
    await mgr.reap_idle()
    assert proc.alive is False


async def test_reap_idle_spares_busy_worker(tmp_path, fake_worker):
    clock = [1000.0]

    class FakeProc:
        def __init__(self): self.alive = True
        def poll(self): return None if self.alive else 0
        def terminate(self): self.alive = False

    proc = FakeProc()
    cfg = _config(tmp_path, worker_idle_ttl=10,
                  worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: proc, clock=lambda: clock[0])
    await mgr.ensure_worker("me@x.cz", "{}")
    mgr.request_started("me@x.cz")                 # a request is streaming
    clock[0] = 1100.0                              # past idle ttl
    await mgr.reap_idle()
    assert proc.alive is True                      # not reaped while busy
    mgr.request_finished("me@x.cz")                # refreshes last_active
    clock[0] = 1200.0                              # idle again past ttl
    await mgr.reap_idle()
    assert proc.alive is False                     # reaped once idle


def test_enforce_cap_spares_busy_worker(tmp_path):
    cfg = _config(tmp_path, max_workers=1)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: None)

    class P:
        def __init__(self): self.killed = False
        def poll(self): return None
        def terminate(self): self.killed = True

    busy = P()
    mgr._workers["a@x.cz"] = workers.WorkerHandle("a@x.cz", 9000, busy, 1.0, inflight=1)
    mgr._enforce_cap()                             # at cap, but A is mid-request
    assert "a@x.cz" in mgr._workers and busy.killed is False
    mgr._workers["a@x.cz"].inflight = 0
    mgr._enforce_cap()                             # now idle -> evictable
    assert "a@x.cz" not in mgr._workers and busy.killed is True


async def test_busy_worker_not_replaced_on_healthz_miss(tmp_path):
    # A worker mid-stream (inflight>0) whose /healthz momentarily misses (2s
    # timeout on a slow, busy worker) must NOT be terminated/replaced — that
    # would abort the live request it's serving. Keep serving it instead.
    spawned = []

    class FakeProc:
        def __init__(self): self.alive = True
        def poll(self): return None if self.alive else 0
        def terminate(self): self.alive = False

    busy = FakeProc()
    dead_port = 59998                              # nothing listening -> /healthz fails fast
    cfg = _config(tmp_path, worker_port_start=dead_port, worker_port_end=dead_port)

    def spawn(key, port, token_dir):
        spawned.append(port)
        return FakeProc()

    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=spawn)
    mgr._workers["me@x.cz"] = workers.WorkerHandle("me@x.cz", dead_port, busy, 1.0, inflight=1)
    port = await mgr.ensure_worker("me@x.cz", "{}")
    assert port == dead_port                       # reused the busy worker
    assert busy.alive is True                      # NOT terminated
    assert spawned == []                           # NOT respawned


async def test_idle_worker_replaced_on_healthz_miss(tmp_path):
    # Counterpart: an *idle* worker (inflight==0) that fails /healthz is a genuinely
    # broken worker and must be replaced.
    spawned = []

    class FakeProc:
        def __init__(self): self.alive = True
        def poll(self): return None if self.alive else 0
        def terminate(self): self.alive = False

    stale = FakeProc()
    dead_port = 59998
    cfg = _config(tmp_path, worker_port_start=dead_port, worker_port_end=dead_port,
                  worker_startup_timeout=1)

    def spawn(key, port, token_dir):
        spawned.append(port)
        return FakeProc()                          # new proc, also never healthy on dead_port

    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=spawn)
    mgr._workers["me@x.cz"] = workers.WorkerHandle("me@x.cz", dead_port, stale, 1.0, inflight=0)
    with pytest.raises(workers.WorkerStartError):
        await mgr.ensure_worker("me@x.cz", "{}")
    assert stale.alive is False                    # the broken idle worker was terminated
    assert spawned == [dead_port]                  # a replacement was attempted


async def test_worker_not_reaped_during_health_check(tmp_path):
    # TOCTOU: while ensure_worker is validating an existing worker (awaiting
    # /healthz), a concurrent reap_idle must not pop it out from under the caller
    # even though it is past its idle TTL.
    clock = [1000.0]

    class FakeProc:
        def __init__(self): self.alive = True
        def poll(self): return None if self.alive else 0
        def terminate(self): self.alive = False

    proc = FakeProc()
    cfg = _config(tmp_path, worker_idle_ttl=10,
                  worker_port_start=59997, worker_port_end=59997)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg),
                                spawn=lambda *a: proc, clock=lambda: clock[0])
    mgr._workers["me@x.cz"] = workers.WorkerHandle("me@x.cz", 59997, proc, 1000.0, inflight=0)
    clock[0] = 2000.0                              # far past the idle TTL

    observed = {}

    async def healthy_that_triggers_reap(port):
        # Fire the reaper during the validation await, then report survival.
        await mgr.reap_idle()
        observed["survived"] = "me@x.cz" in mgr._workers
        return True

    mgr._healthy = healthy_that_triggers_reap
    port = await mgr.ensure_worker("me@x.cz", "{}")
    assert observed["survived"] is True            # not reaped mid-validation
    assert proc.alive is True
    assert port == 59997
    assert mgr._workers["me@x.cz"].inflight == 0   # temp hold released -> no leak


def test_enforce_cap_counts_reserved_spawns(tmp_path):
    # An in-flight spawn holds a reserved port not yet registered in _workers; it
    # must count toward MAX_WORKERS so concurrent distinct-key spawns don't
    # overshoot the cap.
    cfg = _config(tmp_path, max_workers=2)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: None)

    class P:
        def __init__(self): self.killed = False
        def poll(self): return None
        def terminate(self): self.killed = True

    idle = P()
    mgr._workers["a@x.cz"] = workers.WorkerHandle("a@x.cz", 9000, idle, 1.0, inflight=0)
    mgr._reserved.add(9001)                        # a distinct-key spawn in flight
    mgr._enforce_cap()                             # 1 worker + 1 reserved == cap(2) -> free a slot
    assert "a@x.cz" not in mgr._workers and idle.killed is True


def test_alloc_port_excludes_reserved(tmp_path):
    cfg = _config(tmp_path, worker_port_start=9000, worker_port_end=9001)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: None)
    mgr._reserved.add(9000)
    assert mgr._alloc_port() == 9001               # 9000 reserved -> next free

    class P:
        def poll(self): return None

    mgr._workers["a"] = workers.WorkerHandle("a", 9001, P(), 1.0)
    with pytest.raises(workers.WorkerStartError):
        mgr._alloc_port()                          # 9000 reserved + 9001 used -> none free


async def test_materialize_tokens_sets_secure_perms(tmp_path):
    cfg = _config(tmp_path)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: None)
    token_dir = mgr._materialize("Me@X.cz", '{"t":1}')
    tok_file = os.path.join(token_dir, "garmin_tokens.json")
    assert stat.S_IMODE(os.stat(tok_file).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(token_dir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(os.path.dirname(token_dir)).st_mode) == 0o700


def _token_file(tmp_path, key="me@x.cz"):
    return tmp_path / "users" / key / "tokens" / "garmin_tokens.json"


async def test_persist_rotated_captures_worker_rotation(tmp_path, fake_worker):
    # Garmin rotates the refresh token; the worker (garth) writes the rotation
    # to its token file. The manager must persist that back to the store —
    # otherwise the next materialize replays a spent token (the ticket-02 bug).
    persisted = []

    class FakeProc:
        def poll(self): return None
        def terminate(self): pass

    cfg = _config(tmp_path, worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: FakeProc(),
                                persist=lambda k, b: persisted.append((k, b)))
    await mgr.ensure_worker("me@x.cz", '{"v": 1}')
    await mgr.persist_rotated()
    assert persisted == []                                # untouched file — nothing rotated
    _token_file(tmp_path).write_text('{"v": 2}')          # the worker rotated its tokens
    await mgr.persist_rotated()
    assert persisted == [("me@x.cz", '{"v": 2}')]
    await mgr.persist_rotated()
    assert persisted == [("me@x.cz", '{"v": 2}')]         # unchanged since — no re-persist
    mgr.shutdown()


async def test_persist_rotated_skips_torn_file_until_it_parses(tmp_path, fake_worker):
    # garth may not write atomically; a half-written file must never reach the
    # store. The next tick picks the rotation up once the file parses again.
    persisted = []

    class FakeProc:
        def poll(self): return None
        def terminate(self): pass

    cfg = _config(tmp_path, worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: FakeProc(),
                                persist=lambda k, b: persisted.append((k, b)))
    await mgr.ensure_worker("me@x.cz", '{"v": 1}')
    _token_file(tmp_path).write_text('{"v": 2')           # torn mid-write
    await mgr.persist_rotated()
    assert persisted == []
    _token_file(tmp_path).write_text('{"v": 2}')          # write completed
    await mgr.persist_rotated()
    assert persisted == [("me@x.cz", '{"v": 2}')]
    mgr.shutdown()


async def test_reap_idle_captures_last_rotation(tmp_path, fake_worker):
    # A rotation written after the last periodic tick must be captured when the
    # worker is reaped — once it leaves the registry no tick will see it again.
    persisted = []
    clock = [1000.0]

    class FakeProc:
        def __init__(self): self.alive = True
        def poll(self): return None if self.alive else 0
        def terminate(self): self.alive = False

    cfg = _config(tmp_path, worker_idle_ttl=10,
                  worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: FakeProc(),
                                clock=lambda: clock[0],
                                persist=lambda k, b: persisted.append((k, b)))
    await mgr.ensure_worker("me@x.cz", '{"v": 1}')
    _token_file(tmp_path).write_text('{"v": 2}')
    clock[0] = 1100.0                                     # past the idle TTL
    await mgr.reap_idle()
    assert "me@x.cz" not in [h.key for h in mgr._workers.values()]
    assert persisted == [("me@x.cz", '{"v": 2}')]


async def test_respawn_recovers_rotation_from_dead_worker(tmp_path, fake_worker):
    # The worker rotated its tokens and then died. The caller still holds the
    # blob it read from the store BEFORE the rotation was captured — replaying
    # that spent blob is exactly the ticket-02 bug. The respawn must persist
    # the rotation and materialize IT, not the stale argument.
    persisted = []
    procs = []

    class FakeProc:
        def __init__(self): self.rc = None
        def poll(self): return self.rc
        def terminate(self): pass

    def spawn(key, port, token_dir):
        procs.append(FakeProc())
        return procs[-1]

    cfg = _config(tmp_path, worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=spawn,
                                persist=lambda k, b: persisted.append((k, b)))
    await mgr.ensure_worker("me@x.cz", '{"v": 1}')
    _token_file(tmp_path).write_text('{"v": 2}')          # worker rotated...
    procs[0].rc = 0                                       # ...and died
    await mgr.ensure_worker("me@x.cz", '{"v": 1}')        # caller's blob is pre-rotation
    assert persisted == [("me@x.cz", '{"v": 2}')]
    assert _token_file(tmp_path).read_text() == '{"v": 2}'
    mgr.shutdown()


async def test_fresh_manager_trusts_store_over_disk(tmp_path, fake_worker):
    # After a process restart the manager has no baseline: a differing file may
    # be an old generation, not a rotation — e.g. the user re-signed in while
    # the process was down. The store must win; repairing pre-fix drift is the
    # explicit backfill's job (reliability ticket 05), never this path's.
    persisted = []

    class FakeProc:
        def poll(self): return None
        def terminate(self): pass

    cfg = _config(tmp_path, worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    _token_file(tmp_path).parent.mkdir(parents=True)
    _token_file(tmp_path).write_text('{"stale-generation": 1}')
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: FakeProc(),
                                persist=lambda k, b: persisted.append((k, b)))
    await mgr.ensure_worker("me@x.cz", '{"fresh-login": 1}')
    assert persisted == []
    assert _token_file(tmp_path).read_text() == '{"fresh-login": 1}'
    mgr.shutdown()


async def test_shutdown_captures_rotations(tmp_path, fake_worker):
    # Deploys are frequent: a rotation written since the last tick must survive
    # the restart, or the next boot materializes a spent token from the store.
    persisted = []

    class FakeProc:
        def poll(self): return None
        def terminate(self): pass

    cfg = _config(tmp_path, worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: FakeProc(),
                                persist=lambda k, b: persisted.append((k, b)))
    await mgr.ensure_worker("me@x.cz", '{"v": 1}')
    _token_file(tmp_path).write_text('{"v": 2}')
    mgr.shutdown()
    assert persisted == [("me@x.cz", '{"v": 2}')]


async def test_evicted_worker_rotation_is_captured(tmp_path, fake_worker):
    # An eviction (cap pressure) forgets the worker just like a reap does — its
    # last rotation must be captured on the way out.
    persisted = []

    class FakeProc:
        def poll(self): return None
        def terminate(self): pass

    cfg = _config(tmp_path, max_workers=1,
                  worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: FakeProc(),
                                persist=lambda k, b: persisted.append((k, b)))
    await mgr.ensure_worker("me@x.cz", '{"v": 1}')
    _token_file(tmp_path).write_text('{"v": 2}')
    mgr._reserved.add(fake_worker.port + 1)               # a distinct-key spawn in flight
    mgr._enforce_cap()                                    # cap reached -> evict me@x.cz
    assert "me@x.cz" not in mgr._workers
    assert persisted == [("me@x.cz", '{"v": 2}')]


async def test_read_back_error_does_not_break_the_batch(tmp_path):
    # The contract doesn't promise read_back never raises, and the capture
    # points are batch contexts (periodic tick, eviction inside another
    # account's spawn, shutdown) — one account's disk problem must be logged
    # and skipped, never propagated into the batch.
    persisted = []

    class FakeProc:
        def poll(self): return None
        def terminate(self): pass

    class ExplodingReadBack(GarminWorkerForward):
        def read_back(self, workdir):
            if "a@x.cz" in workdir:
                raise RuntimeError("disk went away")
            return super().read_back(workdir)

    cfg = _config(tmp_path, worker_port_start=59900, worker_port_end=59901)
    mgr = workers.WorkerManager(cfg, ExplodingReadBack(cfg), spawn=lambda *a: FakeProc(),
                                persist=lambda k, b: persisted.append((k, b)))

    async def always_healthy(port):
        return True

    mgr._healthy = always_healthy
    await mgr.ensure_worker("a@x.cz", '{"v": 1}')
    await mgr.ensure_worker("b@x.cz", '{"v": 1}')
    _token_file(tmp_path, "a@x.cz").write_text('{"v": 2}')
    _token_file(tmp_path, "b@x.cz").write_text('{"v": 2}')
    await mgr.persist_rotated()                           # must not raise
    assert persisted == [("b@x.cz", '{"v": 2}')]          # A skipped, B still captured
    mgr.shutdown()                                        # must not raise either


def test_read_back_returns_current_token_file(tmp_path):
    # The worker (garth) rewrites garmin_tokens.json when Garmin rotates the
    # refresh token; read_back is how the gateway learns the current content.
    cfg = _config(tmp_path)
    fwd = GarminWorkerForward(cfg)
    assert fwd.read_back(str(tmp_path)) is None            # no file yet
    fwd.materialize('{"t": 1}', str(tmp_path))
    assert fwd.read_back(str(tmp_path)) == '{"t": 1}'
    # garth may not write atomically — a torn (unparseable) file must never
    # be persisted; report "nothing to read" and let the next tick retry.
    (tmp_path / "garmin_tokens.json").write_text('{"t": 1')
    assert fwd.read_back(str(tmp_path)) is None


async def test_manager_delegates_to_forward(tmp_path, fake_worker):
    calls = []

    class FakeForward:
        def command(self):
            return ["fake-worker"]
        def env(self, port, workdir):
            calls.append(("env", port, workdir))
            return {"FAKE": "1"}
        def materialize(self, blob, workdir):
            calls.append(("materialize", blob, workdir))

    class FakeProc:
        def poll(self): return None
        def terminate(self): pass

    cfg = _config(tmp_path, worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(cfg, FakeForward(), spawn=lambda *a: FakeProc())
    await mgr.ensure_worker("me@x.cz", '{"blob":1}')
    assert ("materialize", '{"blob":1}', calls[0][2]) == calls[0]   # forward wrote the credentials
    assert calls[0][2].endswith("/tokens")                          # into the manager-owned workdir
    mgr.shutdown()


def test_pump_demotes_routine_stream_teardown_line(capsys):
    # The worker's uvicorn prints "ASGI callable returned without completing
    # response" on every routine MCP session teardown (the client hung up its
    # listen stream) — a real fault it is not, so it must stay info despite
    # matching the deliberately-loose _WORKER_ERROR filter (reliability 10).
    import json as jsonlib
    workers._pump_worker_output(iter([
        "ERROR:    ASGI callable returned without completing response.\n",
        "ERROR: something actually broke\n",
    ]), "me@x.cz")
    events = [jsonlib.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    levels = {e["line"][:22]: e["level"] for e in events if e["event"] == "worker-log"}
    assert levels["ERROR:    ASGI callabl"] == "info"
    assert levels["ERROR: something actua"] == "error"


# --- port hygiene: never hand a freed port to the next spawn while its ------
# --- previous owner may still be dying (reliability tickets 12/14) ----------

class LingeringProc:
    """SIGTERM was sent but the process is still shutting down — the state in
    which a real worker's uvicorn keeps answering /healthz for a moment."""
    def __init__(self):
        self.terminated = False
        self.killed = False
        self.dead = False
    def poll(self): return 0 if self.dead else None
    def terminate(self): self.terminated = True
    def kill(self): self.killed = True


def test_alloc_port_round_robins(tmp_path):
    # Lowest-free-first hands the next spawn exactly the port the eviction it
    # just triggered freed up; a rotating cursor spaces reuses out instead.
    cfg = _config(tmp_path, worker_port_start=9000, worker_port_end=9002)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: None)
    assert mgr._alloc_port() == 9000
    assert mgr._alloc_port() == 9001               # advanced, though 9000 is free
    assert mgr._alloc_port() == 9002
    assert mgr._alloc_port() == 9000               # wraps


def test_alloc_port_skips_port_of_dying_worker(tmp_path):
    cfg = _config(tmp_path, worker_port_start=9000, worker_port_end=9002)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: None)
    proc = LingeringProc()
    h = workers.WorkerHandle("a@x.cz", 9000, proc, 1.0)
    mgr._workers["a@x.cz"] = h
    mgr._terminate(h)                              # what evict/reap/replace do
    mgr._workers.pop("a@x.cz")
    assert mgr._alloc_port() == 9001               # 9000 cools until its owner dies


def test_cooling_port_frees_when_process_dies(tmp_path):
    cfg = _config(tmp_path, worker_port_start=9000, worker_port_end=9000)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: None)
    proc = LingeringProc()
    h = workers.WorkerHandle("a@x.cz", 9000, proc, 1.0)
    mgr._workers["a@x.cz"] = h
    mgr._terminate(h)
    mgr._workers.pop("a@x.cz")
    with pytest.raises(workers.WorkerStartError):
        mgr._alloc_port()                          # sole port still cooling
    proc.dead = True
    assert mgr._alloc_port() == 9000               # owner observed dead -> usable


def test_cooling_escalates_to_kill_then_expires(tmp_path):
    # A worker that ignores SIGTERM must not shrink the port pool forever:
    # escalate to SIGKILL after a grace period, and hard-expire the hold.
    clock = [1000.0]
    cfg = _config(tmp_path, worker_port_start=9000, worker_port_end=9000)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg),
                                spawn=lambda *a: None, clock=lambda: clock[0])
    proc = LingeringProc()
    h = workers.WorkerHandle("a@x.cz", 9000, proc, 1000.0)
    mgr._workers["a@x.cz"] = h
    mgr._terminate(h)
    mgr._workers.pop("a@x.cz")
    with pytest.raises(workers.WorkerStartError):
        mgr._alloc_port()
    assert proc.killed is False
    clock[0] = 1000.0 + workers._COOLING_KILL_S + 1
    with pytest.raises(workers.WorkerStartError):
        mgr._alloc_port()                          # still held, but escalated
    assert proc.killed is True
    clock[0] = 1000.0 + workers._COOLING_MAX_S + 1
    assert mgr._alloc_port() == 9000               # hard expiry frees the port


async def test_spawn_not_validated_against_dying_predecessors_listener(tmp_path, fake_worker):
    # THE ticket-12 regression: account A's evicted worker still answers
    # /healthz on its port while dying. A spawn for account B must not be
    # handed that port — the old code validated B's half-booted worker against
    # A's dying listener ("worker-started ms=6") and the forward then hit a
    # dead port (ConnectError -> 502).
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading

    cfg = _config(tmp_path, worker_port_start=fake_worker.port,
                  worker_port_end=fake_worker.port + 1, worker_startup_timeout=5)

    class Healthz(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")

    servers = []

    def spawn(key, port, token_dir):
        httpd = HTTPServer(("127.0.0.1", port), Healthz)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        proc = LingeringProc()
        return proc

    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=spawn)
    dying = LingeringProc()
    h = workers.WorkerHandle("a@x.cz", fake_worker.port, dying, 1.0)
    mgr._workers["a@x.cz"] = h
    mgr._terminate(h)                              # evicted; fake_worker keeps listening
    mgr._workers.pop("a@x.cz")
    try:
        port = await mgr.ensure_worker("b@x.cz", "{}")
        assert port != fake_worker.port            # not the dying predecessor's port
    finally:
        for s in servers:
            s.shutdown()
