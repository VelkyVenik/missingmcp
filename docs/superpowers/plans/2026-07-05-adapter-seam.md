# Adapter Seam Extraction — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract an adapter seam from the Garmin-specific code so a second upstream (Rohlik, step 4 of the spec) can be added without touching the OAuth/worker/proxy core — step 1 of `docs/superpowers/specs/2026-07-05-multi-adapter-gateway-design.md`.

**Architecture:** Introduce `adapters/base.py` (contract types + protocols) and `adapters/garmin/` (the moved `garmin_login.py` plus a `GarminAdapter` that owns form-field names, login/MFA/verify mechanics, error copy, and the worker command/env). `WorkerManager` is parameterized by a `WorkerForward` strategy object; `oauth.py` drives the flow through the `Adapter` protocol instead of calling `garmin_login` directly. **Pure refactor**: no schema change, no route change, no log-schema change, no behavior change.

**Tech Stack:** Python 3.12, Starlette, pytest (`uv run --extra dev pytest`), sqlite3, garminconnect (mocked in tests).

## Global Constraints

- **Pure refactor.** After every task the observable behavior is identical: same routes, same DB schema (`garmin_accounts` etc. — schema generalization is spec step 2), same HTML, same HTTP status codes, same **structured-log event names and fields** (`scripts/health.py` parses `login-start`, `login-start-result` with `status` in `{ok, needs_mfa}`, `login-start-failed`, `mfa-resume-failed`, `mfa-verify-failed`, `login-verify-failed`, `worker-spawn`, `mcp-request`, …).
- **Never modify or import `garmin_mcp` internals** — only its CLI + env contract (unchanged by this plan).
- Test command: `uv run --extra dev pytest -q` (the `--extra dev` is required; plain `uv run pytest` fails). Baseline before Task 1: **71 passed**.
- Run the full suite at the end of every task; it must be green before the commit step.
- Python 3.12; all source under `src/garmin_gateway/`, all tests under `tests/`.
- Commit messages end with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` (this repo commits as `vaclav@slajs.eu`, already configured).
- The **Garmin password is never persisted or logged**; it must remain a local that is `del`-ed immediately after the login call (it moves into `GarminAdapter.start_login`, same discipline).
- `account_key` (today `garmin_user_key`) = normalized **lowercased login email**. Normalization moves INTO the adapter (`LoginOk.account_key`) — `oauth._finish` stops normalizing. Exactly one place normalizes.
- **Verify-then-persist:** `adapter.verify(blob)` gates `_finish` on every authorize path, called from `oauth.py` flow code (do NOT move it inside `_finish` or inside the adapter's login methods).

## File Structure (end state of this plan)

```
src/garmin_gateway/
  adapters/
    __init__.py          — build_adapters(config) registry + re-exports of base types
    base.py              — LoginOk, SecondFactorNeeded, LoginError, SecondFactorError,
                           WorkerForward protocol, Adapter protocol
    garmin/
      __init__.py        — GarminAdapter + GarminWorkerForward + _login_error_message
      login.py           — moved verbatim from src/garmin_gateway/garmin_login.py
  workers.py             — WorkerManager(config, forward, …); garmin specifics removed
  oauth.py               — flow driven via Adapter protocol; no garmin imports left
  proxy.py               — handle_mcp gains `adapter` param (log field only, for now)
  app.py                 — builds the adapter registry, wires adapter into handlers
tests/
  test_adapters.py       — NEW: contract types + GarminWorkerForward + GarminAdapter
  test_garmin_login.py   — import path updated only
  test_oauth.py          — drives oauth via a GarminAdapter instance
  test_workers.py        — WorkerManager constructed with a forward
  test_proxy.py          — handle_mcp called with adapter
```

---

### Task 1: Move `garmin_login.py` → `adapters/garmin/login.py` (pure move)

**Files:**
- Create: `src/garmin_gateway/adapters/__init__.py` (empty)
- Create: `src/garmin_gateway/adapters/garmin/__init__.py` (empty)
- Move: `src/garmin_gateway/garmin_login.py` → `src/garmin_gateway/adapters/garmin/login.py` (content unchanged)
- Modify: `src/garmin_gateway/oauth.py:9` (import)
- Modify: `tests/test_garmin_login.py:5` (import)
- Modify: `tests/test_oauth.py:10` (import)

**Interfaces:**
- Consumes: nothing new.
- Produces: module `garmin_gateway.adapters.garmin.login` exposing `start_login`, `resume_login`, `verify_tokens`, `LoginResult`, `GarminLoginError` — exactly today's `garmin_login` API. Later tasks import it as `from . import login` (inside the adapter) and `from garmin_gateway.adapters.garmin import login as garmin_login` (tests).

- [ ] **Step 1: Move the file and create packages**

```bash
cd /Users/vaclav.slajs/dev/garmin-mcp-gateway
mkdir -p src/garmin_gateway/adapters/garmin
touch src/garmin_gateway/adapters/__init__.py src/garmin_gateway/adapters/garmin/__init__.py
git mv src/garmin_gateway/garmin_login.py src/garmin_gateway/adapters/garmin/login.py
```

- [ ] **Step 2: Update the three imports**

In `src/garmin_gateway/oauth.py` line 9, replace:
```python
from . import store, security, garmin_login
```
with:
```python
from . import store, security
from .adapters.garmin import login as garmin_login
```

In `tests/test_garmin_login.py` line 5, replace:
```python
from garmin_gateway import garmin_login
```
with:
```python
from garmin_gateway.adapters.garmin import login as garmin_login
```

In `tests/test_oauth.py` line 10, replace:
```python
from garmin_gateway import store, oauth, security, garmin_login
```
with:
```python
from garmin_gateway import store, oauth, security
from garmin_gateway.adapters.garmin import login as garmin_login
```

(All `patch.object(garmin_login, …)` calls keep working — they patch the same module object `oauth.py` now references.)

- [ ] **Step 3: Verify nothing references the old path**

Run: `grep -rn "garmin_gateway import garmin_login\|from \. import.*garmin_login\|garmin_gateway\.garmin_login" src tests scripts --include="*.py"`
Expected: no output.

- [ ] **Step 4: Run the full suite**

Run: `uv run --extra dev pytest -q`
Expected: `71 passed`

- [ ] **Step 5: Commit**

```bash
git add -A src tests
git commit -m "refactor(adapters): move garmin_login into adapters/garmin/login

Pure move — first cut of the adapter seam (spec 2026-07-05, step 1).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Contract types in `adapters/base.py`

**Files:**
- Create: `src/garmin_gateway/adapters/base.py`
- Test: `tests/test_adapters.py` (new file)

**Interfaces:**
- Consumes: nothing.
- Produces (used by every later task):
  - `LoginOk(account_key: str, blob: str)` — frozen dataclass. `blob` is the adapter-defined **serialized** credentials string (for Garmin: the `garmin_tokens.json` content; the store encrypts it at rest — note this realizes the spec's "adapter-defined JSON blob" as `str`, matching today's `store.upsert_account(conn, key, tokens_json, secret)`).
  - `SecondFactorNeeded(state: object)` — frozen dataclass; `state` is opaque adapter state held in `AuthState` (TTL 300 s).
  - `LoginError(message, reason="unknown")` — exception; `str(e)` is the user-facing form error, `e.reason` feeds the structured log (`{auth, blocked, unknown}` today).
  - `SecondFactorError(message, state)` — exception; the flow re-stashes `e.state` and re-prompts the second-factor form with `str(e)`.
  - `WorkerForward` protocol: `command() -> list[str]`, `env(port: int, workdir: str) -> dict[str, str]`, `materialize(blob: str, workdir: str) -> None`.
  - `Adapter` protocol: attrs `name`, `display_name`, `authorize_template`, `second_factor_template`, `forward`; methods `login_hint(form) -> str`, `start_login(form) -> LoginOk | SecondFactorNeeded`, `resume_second_factor(state, form) -> LoginOk`, `verify(blob) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_adapters.py`:

```python
import dataclasses
import pytest
from garmin_gateway.adapters import base


def test_login_ok_is_frozen():
    r = base.LoginOk(account_key="me@x.cz", blob='{"t":1}')
    assert r.account_key == "me@x.cz" and r.blob == '{"t":1}'
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.account_key = "other"


def test_login_error_carries_reason():
    e = base.LoginError("try later", reason="blocked")
    assert str(e) == "try later" and e.reason == "blocked"
    assert base.LoginError("x").reason == "unknown"      # default


def test_second_factor_error_carries_state():
    state = ("pending", "me@x.cz")
    e = base.SecondFactorError("wrong code", state=state)
    assert str(e) == "wrong code" and e.state is state
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev pytest tests/test_adapters.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'garmin_gateway.adapters.base'` (collection error).

- [ ] **Step 3: Implement `src/garmin_gateway/adapters/base.py`**

```python
"""Adapter contract — the seam between the gateway core and upstream services.

The core (oauth.py flow, WorkerManager, proxy) owns: form rendering, CSRF,
rate limits, OAuth params, second-factor state TTL, encryption of the blob,
code mint + redirect. An adapter owns: what the credential fields are, how
login works, what the blob contains, and how to reach the upstream.

Spec: docs/superpowers/specs/2026-07-05-multi-adapter-gateway-design.md.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Mapping, Protocol


class LoginError(Exception):
    """Login/verify failed. str(e) is user-facing (re-rendered on the form);
    .reason feeds structured logs: "auth" | "blocked" | "unknown"."""

    def __init__(self, message: str, reason: str = "unknown"):
        super().__init__(message)
        self.reason = reason


class SecondFactorError(Exception):
    """Second-factor code rejected. Carries the (possibly refreshed) opaque
    state so the flow can re-stash it and re-prompt with str(e)."""

    def __init__(self, message: str, state: object):
        super().__init__(message)
        self.state = state


@dataclass(frozen=True)
class LoginOk:
    account_key: str   # normalized (lowercased) login identity — cross-table join key
    blob: str          # adapter-defined serialized credentials; store encrypts at rest


@dataclass(frozen=True)
class SecondFactorNeeded:
    state: object      # opaque adapter state; held in AuthState under its TTL


class WorkerForward(Protocol):
    """Forward strategy B: per-account spawned HTTP worker.
    (Strategy A, RemoteForward, arrives with the rohlik adapter — spec step 4.)"""

    def command(self) -> list[str]: ...
    def env(self, port: int, workdir: str) -> dict[str, str]: ...
    def materialize(self, blob: str, workdir: str) -> None:
        """Write credential files into workdir (0600; the manager owns 0700 dirs)."""
        ...


class Adapter(Protocol):
    name: str                    # registry key, log field; path prefix from spec step 3
    display_name: str            # user-facing service name in error copy
    authorize_template: str      # template filename for the credential form
    second_factor_template: str  # template filename for the second-factor form
    forward: WorkerForward

    def login_hint(self, form: Mapping[str, str]) -> str:
        """The login identity as typed (for the login-start log line)."""
        ...

    def start_login(self, form: Mapping[str, str]) -> LoginOk | SecondFactorNeeded:
        """Attempt login from the adapter's own form fields. Raises LoginError.
        Must not retain secrets beyond the call."""
        ...

    def resume_second_factor(self, state: object, form: Mapping[str, str]) -> LoginOk:
        """Complete a SecondFactorNeeded login. Raises SecondFactorError (retryable)
        or LoginError (start over)."""
        ...

    def verify(self, blob: str) -> str:
        """Confirm the blob authenticates against the upstream; return a display
        name for logging. Raises LoginError. Gates persistence on every path."""
        ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/test_adapters.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/garmin_gateway/adapters/base.py tests/test_adapters.py
git commit -m "feat(adapters): contract types — Adapter/WorkerForward protocols, results, errors

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: `GarminWorkerForward` — command/env/materialize

**Files:**
- Modify: `src/garmin_gateway/adapters/garmin/__init__.py` (was empty)
- Test: `tests/test_adapters.py` (append)

**Interfaces:**
- Consumes: `Config.garmin_mcp_cmd` (existing, from `config.py`).
- Produces: `garmin_gateway.adapters.garmin.GarminWorkerForward(config)` implementing the `WorkerForward` protocol:
  - `command() -> list[str]` — returns `config.garmin_mcp_cmd` as-is.
  - `env(port, workdir) -> dict` — exactly the four `GARMIN_MCP_*`/`GARMINTOKENS` vars `workers._default_spawn` sets today (`workers.py:186-192`).
  - `materialize(blob, workdir)` — writes `workdir/garmin_tokens.json` with mode `0600` (today's `workers._materialize_tokens` file-writing tail, `workers.py:172-175`).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_adapters.py`)

```python
import os
import stat
from garmin_gateway.config import load_config
from garmin_gateway.adapters.garmin import GarminWorkerForward

CFG = load_config({"GATEWAY_SECRET": "z" * 40, "PUBLIC_URL": "https://x",
                   "GARMIN_MCP_CMD": "uvx garmin-mcp"})


def test_garmin_forward_command_comes_from_config():
    assert GarminWorkerForward(CFG).command() == ["uvx", "garmin-mcp"]


def test_garmin_forward_env_is_the_documented_contract():
    env = GarminWorkerForward(CFG).env(9007, "/data/users/me/tokens")
    assert env == {
        "GARMIN_MCP_TRANSPORT": "streamable-http",
        "GARMIN_MCP_HOST": "127.0.0.1",
        "GARMIN_MCP_PORT": "9007",
        "GARMINTOKENS": "/data/users/me/tokens",
    }


def test_garmin_forward_materialize_writes_0600_tokens_file(tmp_path):
    GarminWorkerForward(CFG).materialize('{"t":1}', str(tmp_path))
    path = tmp_path / "garmin_tokens.json"
    assert path.read_text() == '{"t":1}'
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev pytest tests/test_adapters.py -v`
Expected: FAIL — `ImportError: cannot import name 'GarminWorkerForward'`.

- [ ] **Step 3: Implement in `src/garmin_gateway/adapters/garmin/__init__.py`**

```python
from __future__ import annotations
import os


class GarminWorkerForward:
    """WorkerForward strategy for the unmodified garmin-mcp worker: its documented
    CLI + env contract (GARMIN_MCP_* / GARMINTOKENS) and token-file materialization."""

    def __init__(self, config):
        self._cfg = config

    def command(self) -> list[str]:
        return self._cfg.garmin_mcp_cmd

    def env(self, port: int, workdir: str) -> dict[str, str]:
        return {
            "GARMIN_MCP_TRANSPORT": "streamable-http",
            "GARMIN_MCP_HOST": "127.0.0.1",
            "GARMIN_MCP_PORT": str(port),
            "GARMINTOKENS": workdir,
        }

    def materialize(self, blob: str, workdir: str) -> None:
        path = os.path.join(workdir, "garmin_tokens.json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(blob)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/test_adapters.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/garmin_gateway/adapters/garmin/__init__.py tests/test_adapters.py
git commit -m "feat(adapters): GarminWorkerForward — worker command/env/token materialization

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Parameterize `WorkerManager` by a forward

**Files:**
- Modify: `src/garmin_gateway/workers.py` (constructor, `_materialize_tokens` → `_materialize`, `_default_spawn`, two log lines)
- Modify: `src/garmin_gateway/app.py:28` (constructor call)
- Modify: `tests/test_workers.py` (all 8 constructor calls + one method rename + one new seam test)
- Modify: `tests/test_proxy.py:29,41` (constructor calls)

**Interfaces:**
- Consumes: `WorkerForward` protocol (Task 2), `GarminWorkerForward` (Task 3).
- Produces: `WorkerManager(config, forward, spawn=None, clock=time.monotonic)` — new required 2nd positional arg. `ensure_worker(key: str, blob: str) -> int` (rename of the `tokens_json` param only; callers pass positionally). Private `_materialize(key, blob) -> workdir` replaces `_materialize_tokens`. Everything else (locks, ports, reaper, cap, snapshot) unchanged.

- [ ] **Step 1: Write the failing seam test** (append to `tests/test_workers.py`)

```python
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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run --extra dev pytest tests/test_workers.py::test_manager_delegates_to_forward -v`
Expected: FAIL — `TypeError` (FakeForward passed as `spawn`… constructor doesn't accept a forward yet).

- [ ] **Step 3: Modify `src/garmin_gateway/workers.py`**

Constructor (`workers.py:30-36`) — add `forward`:
```python
    def __init__(self, config, forward, spawn=None, clock=time.monotonic):
        self._cfg = config
        self._forward = forward
        self._clock = clock
        self._spawn_fn = spawn or self._default_spawn
        self._workers: dict[str, WorkerHandle] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._reserved: set[int] = set()   # ports being spawned but not yet registered
```

`ensure_worker` (`workers.py:40`): rename the parameter and the two garmin-specific lines:
```python
    async def ensure_worker(self, key: str, blob: str) -> int:
```
…line 50 becomes:
```python
            token_dir = self._materialize(key, blob)
```
…and the `worker-spawn` log (line 58) plus the `worker-spawn-failed` log (line 63-64) swap `self._cfg.garmin_mcp_cmd` for the forward:
```python
                log("worker-spawn", port=port, cmd=" ".join(self._forward.command()),
                    token_dir=token_dir)
```
```python
                    log_exc("worker-spawn-failed", e, error=str(e),
                            cmd=" ".join(self._forward.command()))
```

Replace `_materialize_tokens` (`workers.py:165-176`) with — dirs stay manager-owned, the file write delegates:
```python
    def _materialize(self, key: str, blob: str) -> str:
        safe = _SAFE.sub("_", key)
        user_dir = os.path.join(self._cfg.data_dir, "users", safe)
        workdir = os.path.join(user_dir, "tokens")
        os.makedirs(workdir, exist_ok=True)
        os.chmod(user_dir, 0o700)
        os.chmod(workdir, 0o700)
        self._forward.materialize(blob, workdir)
        return workdir
```

Replace `_default_spawn` (`workers.py:185-202`) — env comes from the forward:
```python
    def _default_spawn(self, key: str, port: int, workdir: str):
        env = dict(os.environ)
        env.update(self._forward.env(port, workdir))
        # Redirect the worker's own (chatty, unstructured) stdout/stderr to a
        # per-user file so it doesn't interleave with the gateway's structured
        # log. The child inherits the fd; we close our copy after spawning.
        log_path = os.path.join(os.path.dirname(workdir), "worker.log")
        logf = open(log_path, "a", encoding="utf-8")
        try:
            return subprocess.Popen(self._forward.command(), env=env,
                                    stdout=logf, stderr=subprocess.STDOUT)
        finally:
            logf.close()
```

- [ ] **Step 4: Update the callers**

`src/garmin_gateway/app.py` — line 12 and line 28:
```python
from .workers import WorkerManager
from .adapters.garmin import GarminWorkerForward
```
```python
    manager = WorkerManager(config, GarminWorkerForward(config))
```
(Temporary direct import — Task 6 replaces it with the adapter instance's `.forward`.)

`tests/test_workers.py` — add at the top:
```python
from garmin_gateway.adapters.garmin import GarminWorkerForward
```
and update every constructor call (lines 28, 45, 61, 79, 93, 111, 125):
```python
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=spawn)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: DeadProc())
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: proc, clock=lambda: clock[0])
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: proc, clock=lambda: clock[0])
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: None)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: None)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: None)
```
and in `test_materialize_tokens_sets_secure_perms` (line 126) rename the call:
```python
    token_dir = mgr._materialize("Me@X.cz", '{"t":1}')
```

`tests/test_proxy.py` — add at the top:
```python
from garmin_gateway.adapters.garmin import GarminWorkerForward
```
and update lines 29 and 41:
```python
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: FakeProc())
```

- [ ] **Step 5: Run the full suite**

Run: `uv run --extra dev pytest -q`
Expected: `78 passed` (71 baseline + 3 from Task 2 + 3 from Task 3 + 1 new here). If the count differs, investigate — nothing may be skipped or failing.

- [ ] **Step 6: Commit**

```bash
git add src/garmin_gateway/workers.py src/garmin_gateway/app.py tests/test_workers.py tests/test_proxy.py
git commit -m "refactor(workers): parameterize WorkerManager by a WorkerForward strategy

Manager keeps dirs/ports/locks/reaping; the forward owns command, env and
credential materialization. Garmin specifics now live only in the adapter.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: `GarminAdapter` — login/second-factor/verify behind the protocol

**Files:**
- Modify: `src/garmin_gateway/adapters/garmin/__init__.py` (append `_login_error_message` + `GarminAdapter`)
- Test: `tests/test_adapters.py` (append)

**Interfaces:**
- Consumes: `adapters.base` types (Task 2), `adapters.garmin.login` module (Task 1), `GarminWorkerForward` (Task 3).
- Produces: `GarminAdapter(config)` with:
  - attrs: `name="garmin"`, `display_name="Garmin"`, `authorize_template="authorize.html"`, `second_factor_template="mfa.html"`, `forward` (a `GarminWorkerForward`).
  - `login_hint(form) -> str` — `form.get("garmin_email", "")`.
  - `start_login(form)` — reads `garmin_email`/`garmin_password`, calls `login.start_login`; `GarminLoginError` → `LoginError(mapped_message, reason=e.reason)`; `needs_mfa` → `SecondFactorNeeded(state=(result.pending, email))`; ok → `LoginOk(account_key=email.strip().lower(), blob=result.tokens_json)`. Password `del`-ed in a `finally`.
  - `resume_second_factor(state, form)` — unpacks `(pending, email)`, calls `login.resume_login(pending, form.get("mfa_code",""))`; any exception → `SecondFactorError("Incorrect or expired code, try again", state=state)` (same retryable behavior as today's `oauth.py:182-188`); ok → `LoginOk`.
  - `verify(blob) -> str` — `login.verify_tokens`; `GarminLoginError` → `LoginError("Garmin sign-in could not be verified")` (today's literal copy from `oauth.py:194/230`).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_adapters.py`)

```python
from unittest.mock import patch
from garmin_gateway.adapters import base
from garmin_gateway.adapters.garmin import GarminAdapter, login


def _adapter():
    return GarminAdapter(CFG)


def test_adapter_attrs():
    a = _adapter()
    assert a.name == "garmin" and a.display_name == "Garmin"
    assert a.authorize_template == "authorize.html"
    assert a.second_factor_template == "mfa.html"
    assert a.forward.command() == ["uvx", "garmin-mcp"]
    assert a.login_hint({"garmin_email": "Me@X.cz"}) == "Me@X.cz"


def test_start_login_ok_normalizes_account_key():
    with patch.object(login, "start_login",
                      return_value=login.LoginResult(status="ok", tokens_json='{"t":1}')):
        r = _adapter().start_login({"garmin_email": " Me@X.cz ", "garmin_password": "pw"})
    assert isinstance(r, base.LoginOk)
    assert r.account_key == "me@x.cz" and r.blob == '{"t":1}'


def test_start_login_mfa_state_carries_email():
    with patch.object(login, "start_login",
                      return_value=login.LoginResult(status="needs_mfa", pending=("P", "S"))):
        r = _adapter().start_login({"garmin_email": "me@x.cz", "garmin_password": "pw"})
    assert isinstance(r, base.SecondFactorNeeded)
    assert r.state == (("P", "S"), "me@x.cz")


def test_start_login_blocked_maps_message_and_reason():
    with patch.object(login, "start_login",
                      side_effect=login.GarminLoginError("429", reason="blocked")):
        with pytest.raises(base.LoginError) as ei:
            _adapter().start_login({"garmin_email": "me@x.cz", "garmin_password": "pw"})
    assert ei.value.reason == "blocked"
    assert "rate-limiting" in str(ei.value) and "not your password" in str(ei.value)


def test_start_login_auth_error_maps_message():
    with patch.object(login, "start_login",
                      side_effect=login.GarminLoginError("bad", reason="auth")):
        with pytest.raises(base.LoginError) as ei:
            _adapter().start_login({"garmin_email": "me@x.cz", "garmin_password": "pw"})
    assert ei.value.reason == "auth" and "check your Garmin email" in str(ei.value)


def test_resume_ok_returns_login_ok():
    with patch.object(login, "resume_login", return_value='{"t":9}'):
        r = _adapter().resume_second_factor((("P", "S"), "Me@X.cz"), {"mfa_code": "123456"})
    assert r == base.LoginOk(account_key="me@x.cz", blob='{"t":9}')


def test_resume_failure_is_retryable_with_same_state():
    state = (("P", "S"), "me@x.cz")
    with patch.object(login, "resume_login", side_effect=Exception("wrong code")):
        with pytest.raises(base.SecondFactorError) as ei:
            _adapter().resume_second_factor(state, {"mfa_code": "000000"})
    assert ei.value.state is state
    assert "Incorrect or expired code" in str(ei.value)


def test_verify_ok_and_failure():
    with patch.object(login, "verify_tokens", return_value="Vaclav S"):
        assert _adapter().verify('{"t":1}') == "Vaclav S"
    with patch.object(login, "verify_tokens", side_effect=login.GarminLoginError("bad")):
        with pytest.raises(base.LoginError) as ei:
            _adapter().verify('{"t":1}')
    assert "could not be verified" in str(ei.value)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev pytest tests/test_adapters.py -v`
Expected: FAIL — `ImportError: cannot import name 'GarminAdapter'`.

- [ ] **Step 3: Implement** (append to `src/garmin_gateway/adapters/garmin/__init__.py`)

Add imports at the top of the file:
```python
from typing import Mapping
from ..base import LoginError, LoginOk, SecondFactorError, SecondFactorNeeded
from . import login
```

Append (the `_login_error_message` body moves **verbatim** from `oauth.py:98-107`; it is deleted there in Task 6):
```python
def _login_error_message(reason: str) -> str:
    if reason == "blocked":
        # Garmin (via Cloudflare) rate-limits fresh logins on the mobile SSO
        # endpoint — per-account, not per-IP (garth#217, garminconnect#344) — and
        # the widget/portal fallback can flake. Not the user's fault; a retry usually works.
        return ("Garmin is temporarily rate-limiting new sign-ins (a limit on "
                "Garmin's side, not your password). Please wait a couple of minutes and try again.")
    if reason == "auth":
        return "Garmin sign-in failed — check your Garmin email and password."
    return "Garmin sign-in failed, please try again."


class GarminAdapter:
    name = "garmin"
    display_name = "Garmin"
    authorize_template = "authorize.html"
    second_factor_template = "mfa.html"

    def __init__(self, config):
        self.forward = GarminWorkerForward(config)

    def login_hint(self, form: Mapping[str, str]) -> str:
        return form.get("garmin_email", "")

    def start_login(self, form: Mapping[str, str]) -> LoginOk | SecondFactorNeeded:
        email = form.get("garmin_email", "")
        password = form.get("garmin_password", "")
        try:
            result = login.start_login(email, password)
        except login.GarminLoginError as e:
            reason = getattr(e, "reason", "unknown")
            raise LoginError(_login_error_message(reason), reason=reason) from e
        finally:
            del password  # never retained beyond the login call
        if result.status == "needs_mfa":
            return SecondFactorNeeded(state=(result.pending, email))
        return LoginOk(account_key=email.strip().lower(), blob=result.tokens_json)

    def resume_second_factor(self, state: object, form: Mapping[str, str]) -> LoginOk:
        pending, email = state
        try:
            tokens = login.resume_login(pending, form.get("mfa_code", ""))
        except Exception as e:  # noqa: BLE001 - wrong/expired code: caller re-prompts
            raise SecondFactorError("Incorrect or expired code, try again", state=state) from e
        return LoginOk(account_key=email.strip().lower(), blob=tokens)

    def verify(self, blob: str) -> str:
        try:
            return login.verify_tokens(blob)
        except login.GarminLoginError as e:
            raise LoginError("Garmin sign-in could not be verified") from e
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/test_adapters.py -v`
Expected: 14 passed.

- [ ] **Step 5: Commit**

```bash
git add src/garmin_gateway/adapters/garmin/__init__.py tests/test_adapters.py
git commit -m "feat(adapters): GarminAdapter — login/MFA/verify + error copy behind the protocol

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Re-thread `oauth.py` onto the Adapter protocol

**Files:**
- Modify: `src/garmin_gateway/oauth.py` (imports, `render_authorize`, `_finish`, `authorize_get`, `authorize_post`; delete `_login_error_message`)
- Modify: `src/garmin_gateway/app.py` (construct one `GarminAdapter`, pass it to the authorize handlers, reuse its `.forward` for the manager)
- Modify: `tests/test_oauth.py` (drive handlers with a `GarminAdapter`; second-factor state shape)

**Interfaces:**
- Consumes: `Adapter` protocol (Task 2), `GarminAdapter` (Task 5).
- Produces (Task 7 and the app depend on these exact signatures):
  - `oauth.authorize_get(request, adapter, state, conn, config)` — the old unused `_templates` slot becomes `adapter`.
  - `oauth.authorize_post(request, adapter, state, conn, config)`.
  - `oauth.render_authorize(params, csrf_token, config, adapter, error="")`.
  - `oauth._finish(conn, config, params, blob, account_key)` — **no longer normalizes**; trusts `LoginOk.account_key`.
  - `AuthState` unchanged (it now stores the adapter's opaque state where it stored `pending`).
  - Log schema preserved: `login-start` (with `email=`), `login-start-result` (`status` ∈ {`ok`, `needs_mfa`}), `login-start-failed` (with `reason=`), `mfa-resume-start/-ok/-failed`, `mfa-verify-ok/-failed`, `login-verify-ok/-failed`, `authorize-finish`.

- [ ] **Step 1: Update the failing tests first** (`tests/test_oauth.py`)

Update the import block (line 10-11 after Task 1) to:
```python
from garmin_gateway import store, oauth, security
from garmin_gateway.adapters.garmin import GarminAdapter, login as garmin_login
```

Add one line after `CONFIG = …` (line 13):
```python
ADAPTER = GarminAdapter(CONFIG)
```

Replace `_authz_app` (lines 67-77) so handlers receive the adapter:
```python
def _authz_app(conn):
    state = oauth.AuthState(security.CsrfStore())
    async def aget(request):
        return await oauth.authorize_get(request, ADAPTER, state, conn, CONFIG)
    async def apost(request):
        return await oauth.authorize_post(request, ADAPTER, state, conn, CONFIG)
    app = Starlette(routes=[
        Route("/oauth/authorize", aget, methods=["GET"]),
        Route("/oauth/authorize", apost, methods=["POST"]),
    ])
    return TestClient(app, follow_redirects=False), state
```

In the three tests that stash MFA state directly, the state becomes the adapter tuple `(pending, email)` and `_email` disappears from params:

`test_authorize_post_mfa_rejects_tampered_redirect` (lines 191-193):
```python
    params = {"client_id": cid, "redirect_uri": "https://evil.com/cb", "state": "s",
              "code_challenge": "abc", "code_challenge_method": "S256"}
    lid = state.put_mfa((("P", "S"), "me@x.cz"), params)
```

`test_mfa_wrong_code_reprompts` (lines 267-269):
```python
    params = {"client_id": cid, "redirect_uri": "https://claude.ai/cb", "state": "s",
              "code_challenge": "abc", "code_challenge_method": "S256"}
    lid = state.put_mfa((("P", "S"), "me@x.cz"), params)
```

`test_mfa_verify_failure_restarts` (lines 280-282): same two-line change as `test_mfa_wrong_code_reprompts`.

Everything else in `test_oauth.py` stays byte-identical — the `patch.object(garmin_login, …)` mocks keep working because `GarminAdapter` calls the same module object.

- [ ] **Step 2: Run the oauth tests to verify they fail**

Run: `uv run --extra dev pytest tests/test_oauth.py -v`
Expected: FAIL — `TypeError`/assertion failures (handlers still expect the old flow; `_email` no longer provided).

- [ ] **Step 3: Rewrite the flow in `src/garmin_gateway/oauth.py`**

Imports (top of file) — drop the `garmin_login` import from Task 1, add base types:
```python
from . import store, security
from .adapters.base import LoginError, SecondFactorError, SecondFactorNeeded
```

Delete `_login_error_message` (lines 98-107) — it lives in the adapter now.

`render_authorize` (lines 110-120) — template comes from the adapter:
```python
def render_authorize(params: dict, csrf_token: str, config, adapter, error: str = "") -> HTMLResponse:
    body = _fill(_tpl(adapter.authorize_template), {
        "CSRF": csrf_token,
        "CLIENT_ID": params.get("client_id", ""),
        "REDIRECT_URI": params.get("redirect_uri", ""),
        "STATE": params.get("state", ""),
        "CODE_CHALLENGE": params.get("code_challenge", ""),
        "METHOD": params.get("code_challenge_method", ""),
        **_operator_fields(config),
    }, error)
    return HTMLResponse(body)
```

`authorize_get` (lines 133-142) — rename the placeholder param and pass the adapter through:
```python
async def authorize_get(request, adapter, state, conn, config) -> HTMLResponse:
    params = _oauth_params_from(request.query_params)
    client = store.get_client(conn, params["client_id"])
    if client is None:
        return HTMLResponse("unknown client_id", status_code=400)
    if not security.validate_redirect_uri(params["redirect_uri"], client["redirect_uris"]):
        return HTMLResponse("invalid redirect_uri", status_code=400)
    if params["code_challenge_method"] != "S256" or not params["code_challenge"]:
        return HTMLResponse("PKCE S256 required", status_code=400)
    return render_authorize(params, state.csrf.issue(), config, adapter)
```

`_finish` (lines 145-156) — normalization moved to the adapter; only the two first lines change:
```python
def _finish(conn, config, params: dict, blob: str, account_key: str) -> RedirectResponse:
    # blob already verified by the caller (adapter.verify) before we persist
    store.upsert_account(conn, account_key, blob, config.gateway_secret)
    code = security.new_secret(32)
    store.create_code(
        conn, store.hash_token(code), params["client_id"], params["redirect_uri"],
        params["code_challenge"], params["code_challenge_method"], account_key,
    )
    sep = "&" if "?" in params["redirect_uri"] else "?"
    location = params["redirect_uri"] + sep + urlencode({"code": code, "state": params["state"]})
    return RedirectResponse(location, status_code=302)
```

`authorize_post` (lines 159-232) — full replacement:
```python
async def authorize_post(request, adapter, state, conn, config) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    has_login_id = bool(form.get("login_id"))
    log("authorize-post", step="mfa" if has_login_id else "login",
        has_csrf=bool(form.get("csrf")), client_id=form.get("client_id", ""))
    if not state.csrf.consume(form.get("csrf", "")):
        log_error("authorize-csrf-invalid", step="mfa" if has_login_id else "login")
        return HTMLResponse("invalid or expired CSRF token", status_code=400)

    # second-factor step (Garmin: MFA)
    if has_login_id:
        popped = state.pop_mfa(form["login_id"])
        if popped is None:
            log_error("mfa-session-missing", login_id=form.get("login_id", "")[:6])
            return HTMLResponse("MFA session expired, please start over", status_code=400)
        pending, params = popped
        client = store.get_client(conn, params["client_id"])
        if client is None or not security.validate_redirect_uri(params["redirect_uri"], client["redirect_uris"]):
            return HTMLResponse("invalid client/redirect_uri", status_code=400)
        try:
            log("mfa-resume-start", mfa_len=len(form.get("mfa_code", "")))
            result = adapter.resume_second_factor(pending, form)
            log("mfa-resume-ok", tokens_len=len(result.blob or ""))
        except SecondFactorError as e:  # wrong/expired code: re-prompt
            log_exc("mfa-resume-failed", e, error_type=type(e).__name__, error=str(e))
            lid = state.put_mfa(e.state, params)
            body = _fill(_tpl(adapter.second_factor_template),
                         {"CSRF": state.csrf.issue(), "LOGIN_ID": lid, **_operator_fields(config)},
                         str(e))
            return HTMLResponse(body, status_code=400)
        try:
            name = adapter.verify(result.blob)
            log("mfa-verify-ok", name=name)
        except LoginError as e:  # blob didn't authenticate: start over
            log_exc("mfa-verify-failed", e, error=str(e))
            return render_authorize(params, state.csrf.issue(), config, adapter, str(e))
        log("authorize-finish", step="mfa")
        return _finish(conn, config, params, result.blob, result.account_key)

    # login step
    params = _oauth_params_from(form)
    client = store.get_client(conn, params["client_id"])
    if client is None or not security.validate_redirect_uri(params["redirect_uri"], client["redirect_uris"]):
        return HTMLResponse("invalid client/redirect_uri", status_code=400)
    try:
        log("login-start", email=adapter.login_hint(form))
        result = adapter.start_login(form)
        log("login-start-result",
            status="needs_mfa" if isinstance(result, SecondFactorNeeded) else "ok")
    except LoginError as e:
        log_exc("login-start-failed", e, reason=e.reason, error=str(e))
        return render_authorize(params, state.csrf.issue(), config, adapter, str(e))
    except Exception as e:  # noqa: BLE001 - unexpected failure
        log_exc("login-start-failed", e, reason="unknown", error_type=type(e).__name__, error=str(e))
        return render_authorize(params, state.csrf.issue(), config, adapter,
                                f"{adapter.display_name} sign-in failed, please try again.")
    if isinstance(result, SecondFactorNeeded):
        lid = state.put_mfa(result.state, params)
        body = _fill(_tpl(adapter.second_factor_template),
                     {"CSRF": state.csrf.issue(), "LOGIN_ID": lid, **_operator_fields(config)}, "")
        return HTMLResponse(body)
    try:
        name = adapter.verify(result.blob)
        log("login-verify-ok", name=name)
    except LoginError as e:
        log_exc("login-verify-failed", e, error=str(e))
        return render_authorize(params, state.csrf.issue(), config, adapter, str(e))
    log("authorize-finish", step="login")
    return _finish(conn, config, params, result.blob, result.account_key)
```

Behavior notes (why this is still a pure refactor):
- The password now lives and dies inside `adapter.start_login` — `oauth.py` never touches it (the old `del password` lines disappear *with* the variable).
- `log("login-start-result", status=…)` keeps emitting `needs_mfa`, not a new value — `scripts/health.py:73-74` buckets on it.
- Verify-failure messages: `str(LoginError("Garmin sign-in could not be verified"))` renders the exact string the old code hard-coded.
- `AuthState.put_mfa/pop_mfa` are untouched; they now carry `(pending, email)` instead of `pending`, and `params` no longer smuggles `_email`.

- [ ] **Step 4: Update `src/garmin_gateway/app.py`**

Replace the Task-4 import and construction (and pass the adapter to the two authorize handlers):
```python
from .adapters.garmin import GarminAdapter
```
```python
    garmin = GarminAdapter(config)
    manager = WorkerManager(config, garmin.forward)
```
```python
    async def authz_get(request):
        return await oauth.authorize_get(request, garmin, auth_state, conn, config)

    async def authz_post(request):
        if not rate.check(f"login:{request.client.host}", 5, 60):
            return HTMLResponse("Too many attempts, wait a minute.", status_code=429)
        return await oauth.authorize_post(request, garmin, auth_state, conn, config)
```

- [ ] **Step 5: Run the full suite**

Run: `uv run --extra dev pytest -q`
Expected: `86 passed` (78 after Task 4 + 8 adapter tests from Task 5)

- [ ] **Step 6: Commit**

```bash
git add src/garmin_gateway/oauth.py src/garmin_gateway/app.py tests/test_oauth.py
git commit -m "refactor(oauth): drive the authorize flow through the Adapter protocol

oauth.py no longer knows Garmin: field names, error copy, MFA state shape and
key normalization live in GarminAdapter. Log schema unchanged (health.py).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Adapter registry + thread adapter through `proxy.py`

**Files:**
- Modify: `src/garmin_gateway/adapters/__init__.py` (registry + re-exports)
- Modify: `src/garmin_gateway/proxy.py:48-49` (signature + log field)
- Modify: `src/garmin_gateway/app.py` (use the registry; pass adapter to the mcp handler)
- Modify: `tests/test_proxy.py:14-18` (call site)
- Test: `tests/test_adapters.py` (append registry test)

**Interfaces:**
- Consumes: `GarminAdapter` (Task 5).
- Produces:
  - `garmin_gateway.adapters.build_adapters(config) -> dict[str, Adapter]` — today returns `{"garmin": GarminAdapter(config)}`; spec step 4 adds `"rohlik"` here and step 3 mounts routes per key.
  - `proxy.handle_mcp(request, method, adapter, conn, manager, config, secret, rate)` — `adapter` inserted as the 3rd positional parameter; used only for `log("mcp-request", adapter=adapter.name, …)` until the forward-strategy branch arrives (spec step 4).

- [ ] **Step 1: Write the failing registry test** (append to `tests/test_adapters.py`)

```python
from garmin_gateway.adapters import build_adapters


def test_registry_builds_garmin():
    adapters = build_adapters(CFG)
    assert set(adapters) == {"garmin"}
    assert adapters["garmin"].name == "garmin"
    assert adapters["garmin"].forward.command() == ["uvx", "garmin-mcp"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run --extra dev pytest tests/test_adapters.py::test_registry_builds_garmin -v`
Expected: FAIL — `ImportError: cannot import name 'build_adapters'`.

- [ ] **Step 3: Implement the registry** — `src/garmin_gateway/adapters/__init__.py` (was empty):

```python
from .base import (  # noqa: F401 - re-exported as the adapter API surface
    Adapter, LoginError, LoginOk, SecondFactorError, SecondFactorNeeded, WorkerForward,
)


def build_adapters(config) -> dict:
    from .garmin import GarminAdapter
    return {"garmin": GarminAdapter(config)}
```

- [ ] **Step 4: Thread the adapter through `proxy.py`**

`src/garmin_gateway/proxy.py` lines 48-49 — new signature + log field (nothing else changes):
```python
async def handle_mcp(request, method, adapter, conn, manager, config, secret, rate) -> Response:
    log("mcp-request", adapter=adapter.name, method=method,
        has_session=bool(request.headers.get("mcp-session-id")))
```

`src/garmin_gateway/app.py` — switch to the registry and pass the adapter:
```python
from .adapters import build_adapters
```
(replacing `from .adapters.garmin import GarminAdapter`), and in `build_app`:
```python
    adapters = build_adapters(config)
    garmin = adapters["garmin"]
    manager = WorkerManager(config, garmin.forward)
```
```python
    def mcp(method):
        async def handler(request):
            return await proxy.handle_mcp(request, method, garmin, conn, manager,
                                          config, config.gateway_secret, rate)
        return handler
```

`tests/test_proxy.py` — update `_app` (line 17) and **replace** the Task-4 import line (`from garmin_gateway.adapters.garmin import GarminWorkerForward`) with:
```python
from garmin_gateway.adapters.garmin import GarminAdapter, GarminWorkerForward
```
(`GarminWorkerForward` is still used by the two `WorkerManager(...)` constructions.)
```python
def _app(conn, mgr, cfg):
    rate = security.RateLimiter()
    adapter = GarminAdapter(cfg)
    async def mcp_post(request):
        return await proxy.handle_mcp(request, "POST", adapter, conn, mgr, cfg,
                                      cfg.gateway_secret, rate)
    return TestClient(Starlette(routes=[Route("/mcp", mcp_post, methods=["POST"])]))
```

- [ ] **Step 5: Run the full suite**

Run: `uv run --extra dev pytest -q`
Expected: `87 passed` (86 after Task 6 + the registry test)

- [ ] **Step 6: Commit**

```bash
git add src/garmin_gateway/adapters/__init__.py src/garmin_gateway/proxy.py src/garmin_gateway/app.py tests/test_proxy.py tests/test_adapters.py
git commit -m "feat(adapters): registry + adapter threaded through proxy and app wiring

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Documentation — CLAUDE.md reflects the seam

**Files:**
- Modify: `CLAUDE.md` (module map + one invariant)
- Modify: `docs/superpowers/specs/2026-07-05-multi-adapter-gateway-design.md` (tick step 1 in "Implementation order" — change `1. **Extract the adapter seam**` to `1. ~~Extract the adapter seam~~ ✅ done (plan 2026-07-05-adapter-seam.md)`)

**Interfaces:** none (docs only).

- [ ] **Step 1: Update the module list in CLAUDE.md**

In the `## Architecture` module list, replace the `garmin_login.py` bullet with:

```markdown
- **`adapters/base.py`** — the adapter contract: `Adapter`/`WorkerForward` protocols, `LoginOk`/`SecondFactorNeeded` results, `LoginError`/`SecondFactorError`. The seam between the core and upstream services (spec 2026-07-05).
- **`adapters/garmin/`** — `login.py` is the thin `garminconnect` wrapper (`start_login` MFA-aware with transient-block retry, `resume_login`, `verify_tokens`); `GarminAdapter` owns form-field names, error copy, account-key normalization and the second-factor state; `GarminWorkerForward` owns the worker CLI/env contract + token materialization. Registry: `adapters.build_adapters(config)`.
```

And update the `workers.py` bullet's first line to mention the forward:

```markdown
- **`workers.py`** — `WorkerManager(config, forward)`: per-account `asyncio.Lock` (no double-spawn), lazy spawn, `/healthz` poll, idle reaper, LRU cap; dirs `0700` are manager-owned, credential files come from `forward.materialize` (`0600`). `spawn` is injectable for tests.
```

- [ ] **Step 2: Add the invariant to "Cross-cutting invariants"**

Append this bullet:

```markdown
- **The adapter owns identity normalization:** `LoginOk.account_key` is already normalized (lowercased email); `oauth._finish` persists it as-is. Log event names and fields are a stable schema (`scripts/health.py` parses them) — refactors must not rename events or the `status`/`reason` values.
```

- [ ] **Step 3: Run the full suite one last time**

Run: `uv run --extra dev pytest -q`
Expected: `87 passed`

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-07-05-multi-adapter-gateway-design.md
git commit -m "docs: adapter seam in CLAUDE.md module map + invariants; spec step 1 done

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Out of scope (later spec steps — do NOT do these here)

- Schema generalization (`accounts` table, `adapter` columns) — spec step 2.
- Path-scoped routes/`.well-known` (`/garmin/mcp`) — spec step 3.
- `RemoteForward` + Rohlik adapter + forward-strategy branch in `proxy.py` — spec step 4.
- Any rename of `garmin_user_key` in `store.py`/DB — step 2.
- Moving templates under `adapters/garmin/templates/` — only worth it when a second adapter brings its own templates (step 4).

## Verification at the end

1. `uv run --extra dev pytest -q` → 87 passed.
2. Local smoke (OAuth surface only, no Garmin):
   `GATEWAY_SECRET="$(openssl rand -base64 48)" PUBLIC_URL=http://localhost:8088 PORT=8088 DATA_DIR=./.localdata uv run garmin-gateway`
   then `curl -s http://localhost:8088/.well-known/oauth-authorization-server | jq .issuer` → `"http://localhost:8088"`, and `curl -s http://localhost:8088/healthz` → `ok`.
3. `git log --oneline` shows 8 commits, each with a green suite behind it.
