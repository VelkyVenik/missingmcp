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
