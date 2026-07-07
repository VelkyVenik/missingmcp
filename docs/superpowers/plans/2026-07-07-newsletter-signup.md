# Newsletter Signup & Connector Suggestions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a home-page visitor (a) opt in to be emailed when a new connector goes live, and (b) suggest a missing connector — both via modal dialogs, storing the data locally in SQLite. No email is sent yet (a provider is chosen later); this is capture-only.

**Architecture:** Two new anonymous public `POST` endpoints (`/subscribe`, `/suggest`) on the existing Starlette app, backed by two new SQLite tables (`subscribers`, `suggestions`) added to the existing `store.py` schema. The UI is two `<dialog>` modals in `home.html` driven by vanilla JS appended to the existing `static/site.js` (CSP-safe: `default-src 'self'` allows the same-origin `fetch`). The current GitHub-issues link in the "Missing something?" card is replaced by two buttons opening these modals. No new dependencies, no email sending, no unsubscribe flow.

**Tech Stack:** Python 3.12, Starlette, SQLite (existing `store`), vanilla JS + `<dialog>`. **No new dependencies.**

## Global Constraints

- Tests: run from the repo root. The canonical `uv run --extra dev pytest -q` from CLAUDE.md **currently fails in this environment** with `Failed to spawn: pytest` (the `pytest` console script isn't on the venv PATH); use **`uv run --extra dev python -m pytest -q`** instead — the module runs fine. **Baseline: 203 passed.** Full suite green at the end of every task.
- **No email is sent by this feature.** Storage only. No double opt-in, no confirmation email, no unsubscribe link. Sending/provider is explicitly out of scope (decided: Resend's free tier is one-domain and that domain is already used elsewhere; provider TBD later).
- **No new dependencies.** Modal behavior is vanilla JS in the one existing `static/site.js`; the protocol layer precedent for "hand-rolled, no SDK" is already established in this repo.
- **CSP must NOT change.** `security.security_headers()` stays `default-src 'self'; style-src 'self' 'unsafe-inline'`. No inline `<script>`, no `form-action` directive. All JS lives in `static/site.js` (loaded with `defer` by `_layout.html`).
- **Anonymous endpoints, no CSRF token.** These are unauthenticated public opt-in forms with no session/state to protect — CSRF has no meaningful target here (the attacker would only be able to subscribe an email they already control). Protection is: per-IP rate limit (reuse the shared `security.RateLimiter`), server-side email-format validation, a honeypot field, and silent-dedup so the endpoint can't be used to probe membership.
- **Never log the submitted email or suggestion text.** Marketing PII must not enter logs. Log only counts/flags (e.g. `log("subscribe")`, `log("suggest", wants_updates=<bool>)`). This matches the repo's "logs carry at most an 8-char hash / account key, never secrets" norm.
- **Email normalization:** `.strip().lower()` before storing, so dedup on the `subscribers.email` PRIMARY KEY is meaningful.
- Templates are fragments wrapped by `pages.render_page` (`_layout.html`); `home.html` is rendered **once at startup** into a static string and served verbatim (also by the 404 catch-all) — so the modal markup lives in `home.html`, and success/error state is handled client-side, never by re-rendering the page.
- Log event names/fields are a stable schema; the new events are exactly `subscribe` and `suggest`. Don't rename existing events.

---

### Task 1: Store — `subscribers` and `suggestions` tables + CRUD

**Files:**
- Modify: `src/missingmcp/store.py` (add two tables to `_SCHEMA`; add CRUD after the usage-metrics section)
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: existing `store.init_db(path) -> sqlite3.Connection`.
- Produces (used by Tasks 2 & 4):
  - `add_subscriber(conn, email: str) -> None` — idempotent insert into `subscribers`.
  - `add_suggestion(conn, email: str, description: str, wants_updates: bool) -> None` — insert into `suggestions`; when `wants_updates`, also `add_subscriber`.
  - `list_subscribers(conn) -> list[dict]` — rows `{email, created_at}` ordered by `created_at`.
  - `list_suggestions(conn) -> list[dict]` — rows `{email, description, wants_updates, created_at}` ordered by `created_at`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_store.py`:

```python
def test_add_subscriber_is_idempotent(conn):
    store.add_subscriber(conn, "fan@example.com")
    store.add_subscriber(conn, "fan@example.com")          # duplicate — silent no-op
    subs = store.list_subscribers(conn)
    assert [s["email"] for s in subs] == ["fan@example.com"]
    assert subs[0]["created_at"]                           # timestamp filled


def test_add_suggestion_without_updates_does_not_subscribe(conn):
    store.add_suggestion(conn, "a@example.com", "Strava please", wants_updates=False)
    sugg = store.list_suggestions(conn)
    assert len(sugg) == 1
    assert sugg[0]["email"] == "a@example.com"
    assert sugg[0]["description"] == "Strava please"
    assert sugg[0]["wants_updates"] == 0
    assert store.list_subscribers(conn) == []              # not added to newsletter


def test_add_suggestion_with_updates_also_subscribes(conn):
    store.add_suggestion(conn, "b@example.com", "Oura", wants_updates=True)
    assert [s["email"] for s in store.list_subscribers(conn)] == ["b@example.com"]
    assert store.list_suggestions(conn)[0]["wants_updates"] == 1


def test_suggestion_allows_repeat_email(conn):
    # suggestions are a log, not a set — the same person may suggest twice
    store.add_suggestion(conn, "c@example.com", "Fitbit", wants_updates=False)
    store.add_suggestion(conn, "c@example.com", "Withings", wants_updates=False)
    assert len(store.list_suggestions(conn)) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev python -m pytest tests/test_store.py -k "subscriber or suggestion" -v`
Expected: FAIL — `AttributeError: module 'missingmcp.store' has no attribute 'add_subscriber'`.

- [ ] **Step 3: Add the tables to the schema**

In `src/missingmcp/store.py`, inside the `_SCHEMA` string, append these two `CREATE TABLE` statements just before the closing `"""` (after the `tool_usage` table):

```sql
CREATE TABLE IF NOT EXISTS subscribers (
    email      TEXT PRIMARY KEY,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS suggestions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    wants_updates INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT DEFAULT (datetime('now'))
);
```

(`_SCHEMA` is run via `executescript` on every `init_db`, and every statement is `CREATE TABLE IF NOT EXISTS`, so existing production DBs pick these up on the next startup with no `PRAGMA user_version` migration needed.)

- [ ] **Step 4: Add the CRUD functions**

At the end of `src/missingmcp/store.py` (after `record_usage`), add:

```python
# --- newsletter subscribers & connector suggestions -----------------------
# Marketing opt-in captured on the home page. Stored locally only — no email is
# sent from here (a provider is chosen later). Never log the address itself.

def add_subscriber(conn, email: str) -> None:
    """Record a newsletter opt-in. Idempotent: a repeat email is a silent no-op
    (INSERT OR IGNORE), so the endpoint can't be used to probe who's subscribed."""
    conn.execute("INSERT OR IGNORE INTO subscribers (email) VALUES (?)", (email,))
    conn.commit()


def add_suggestion(conn, email: str, description: str, wants_updates: bool) -> None:
    """Record a 'which connector next?' suggestion (a log — repeats allowed). When
    wants_updates, the email is also added to the newsletter list."""
    conn.execute(
        "INSERT INTO suggestions (email, description, wants_updates) VALUES (?, ?, ?)",
        (email, description, 1 if wants_updates else 0),
    )
    if wants_updates:
        conn.execute("INSERT OR IGNORE INTO subscribers (email) VALUES (?)", (email,))
    conn.commit()


def list_subscribers(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT email, created_at FROM subscribers ORDER BY created_at"
    ).fetchall()
    return [dict(r) for r in rows]


def list_suggestions(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT email, description, wants_updates, created_at "
        "FROM suggestions ORDER BY created_at"
    ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run --extra dev python -m pytest tests/test_store.py -k "subscriber or suggestion" -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Full suite still green**

Run: `uv run --extra dev python -m pytest -q`
Expected: 207 passed (203 baseline + 4).

- [ ] **Step 7: Commit**

```bash
git add src/missingmcp/store.py tests/test_store.py
git commit -m "feat(store): subscribers + suggestions tables and CRUD"
```

---

### Task 2: `valid_email` + `/subscribe` and `/suggest` endpoints

**Files:**
- Modify: `src/missingmcp/security.py` (add `valid_email`)
- Modify: `src/missingmcp/app.py` (two handlers inside `build_app`, two routes in the top-level `routes` list)
- Test: `tests/test_security.py`, `tests/test_app.py`

**Interfaces:**
- Consumes: `store.add_subscriber`, `store.add_suggestion` (Task 1); `security.RateLimiter.check`; the shared `rate` and `conn` singletons already built in `build_app`.
- Produces (used by Task 3's JS): `POST /subscribe` and `POST /suggest`, both `application/x-www-form-urlencoded`, both returning JSON. Success `{"ok": true}` (200). Bad email `{"ok": false, "error": "invalid_email"}` (400). Rate-limited `{"ok": false, "error": "rate_limited"}` (429). Honeypot filled → silent `{"ok": true}` (200), no DB write.
  - `/subscribe` fields: `email`, `website` (honeypot).
  - `/suggest` fields: `email`, `description`, `wants_updates` (checkbox → `"1"` when checked, absent otherwise), `website` (honeypot).
- Produces: `security.valid_email(email: str) -> bool`.

- [ ] **Step 1: Write the failing `valid_email` test**

Append to `tests/test_security.py`:

```python
def test_valid_email():
    assert security.valid_email("a@b.co")
    assert security.valid_email("First.Last+tag@sub.example.com")
    assert not security.valid_email("")
    assert not security.valid_email("no-at-sign")
    assert not security.valid_email("no@domain")          # no TLD dot
    assert not security.valid_email("spaces in@email.com")
    assert not security.valid_email("a@" + "x" * 300 + ".com")   # too long
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run --extra dev python -m pytest tests/test_security.py::test_valid_email -v`
Expected: FAIL — `AttributeError: module 'missingmcp.security' has no attribute 'valid_email'`.

- [ ] **Step 3: Implement `valid_email`**

In `src/missingmcp/security.py`, next to the existing `_SESSION_RE` regex near the top, add:

```python
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
```

and add this function (near `validate_session_id`):

```python
def valid_email(email: str) -> bool:
    """Cheap syntactic check for opt-in forms — no deliverability probe, no DNS.
    Rejects empty, over-long (>254, the RFC 5321 max), and anything without a
    plausible local@domain.tld shape."""
    return bool(email) and len(email) <= 254 and bool(_EMAIL_RE.match(email))
```

(`re` is already imported in `security.py`.)

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run --extra dev python -m pytest tests/test_security.py::test_valid_email -v`
Expected: PASS.

- [ ] **Step 5: Write the failing endpoint tests**

Append to `tests/test_app.py`:

```python
import sqlite3


def _client_and_db(tmp_path):
    db = str(tmp_path / "t.db")
    cfg = load_config({"GATEWAY_SECRET": "s" * 40, "PUBLIC_URL": "https://gw.example.com",
                       "DATA_DIR": str(tmp_path), "DB_PATH": db})
    return TestClient(build_app(cfg)), db


def _rows(db, sql):
    c = sqlite3.connect(db)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def test_subscribe_stores_email(tmp_path):
    c, db = _client_and_db(tmp_path)
    r = c.post("/subscribe", data={"email": "Fan@Example.com"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    # normalized lowercase, stored
    assert _rows(db, "SELECT email FROM subscribers") == [("fan@example.com",)]


def test_subscribe_duplicate_is_silent_ok(tmp_path):
    c, db = _client_and_db(tmp_path)
    c.post("/subscribe", data={"email": "fan@example.com"})
    r = c.post("/subscribe", data={"email": "fan@example.com"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert len(_rows(db, "SELECT email FROM subscribers")) == 1


def test_subscribe_rejects_bad_email(tmp_path):
    c, db = _client_and_db(tmp_path)
    r = c.post("/subscribe", data={"email": "not-an-email"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_email"
    assert _rows(db, "SELECT email FROM subscribers") == []


def test_subscribe_honeypot_is_silent_noop(tmp_path):
    c, db = _client_and_db(tmp_path)
    r = c.post("/subscribe", data={"email": "bot@example.com", "website": "http://spam"})
    assert r.status_code == 200 and r.json() == {"ok": True}   # looks fine to the bot
    assert _rows(db, "SELECT email FROM subscribers") == []    # but nothing stored


def test_subscribe_rate_limited(tmp_path):
    c, _ = _client_and_db(tmp_path)
    codes = [c.post("/subscribe", data={"email": f"u{i}@example.com"}).status_code
             for i in range(7)]
    assert 429 in codes                                        # 5/60s window exceeded


def test_suggest_stores_suggestion_without_subscribing(tmp_path):
    c, db = _client_and_db(tmp_path)
    r = c.post("/suggest", data={"email": "a@example.com", "description": "Strava"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert _rows(db, "SELECT email, description, wants_updates FROM suggestions") == \
        [("a@example.com", "Strava", 0)]
    assert _rows(db, "SELECT email FROM subscribers") == []


def test_suggest_with_updates_also_subscribes(tmp_path):
    c, db = _client_and_db(tmp_path)
    r = c.post("/suggest", data={"email": "b@example.com", "description": "Oura",
                                 "wants_updates": "1"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert _rows(db, "SELECT email FROM subscribers") == [("b@example.com",)]


def test_suggest_honeypot_is_silent_noop(tmp_path):
    c, db = _client_and_db(tmp_path)
    r = c.post("/suggest", data={"email": "bot@example.com", "description": "x",
                                 "website": "spam"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert _rows(db, "SELECT email FROM suggestions") == []
```

- [ ] **Step 6: Run them to verify they fail**

Run: `uv run --extra dev python -m pytest tests/test_app.py -k "subscribe or suggest" -v`
Expected: FAIL — POSTs hit the GET-only catch-all and return 405 / 404, not the JSON shapes above.

- [ ] **Step 7: Add the handlers and routes**

In `src/missingmcp/app.py`, inside `build_app`, add the two handlers right after the `privacy` handler (before `notfound`):

```python
    async def subscribe(request):
        if not rate.check(f"subscribe:{request.client.host}", 5, 60):
            return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)
        form = await request.form()
        if (form.get("website") or "").strip():        # honeypot: bots fill it
            return JSONResponse({"ok": True})           # look successful, store nothing
        email = (form.get("email") or "").strip().lower()
        if not security.valid_email(email):
            return JSONResponse({"ok": False, "error": "invalid_email"}, status_code=400)
        store.add_subscriber(conn, email)
        log("subscribe")                                # never log the address itself
        return JSONResponse({"ok": True})

    async def suggest(request):
        if not rate.check(f"suggest:{request.client.host}", 5, 60):
            return JSONResponse({"ok": False, "error": "rate_limited"}, status_code=429)
        form = await request.form()
        if (form.get("website") or "").strip():
            return JSONResponse({"ok": True})
        email = (form.get("email") or "").strip().lower()
        if not security.valid_email(email):
            return JSONResponse({"ok": False, "error": "invalid_email"}, status_code=400)
        description = (form.get("description") or "").strip()[:2000]
        wants = (form.get("wants_updates") or "").lower() in ("1", "true", "on", "yes")
        store.add_suggestion(conn, email, description, wants)
        log("suggest", wants_updates=wants)
        return JSONResponse({"ok": True})
```

Then in the top-level `routes = [ ... ]` list, add these two lines right after the `Route("/privacy", privacy, ...)` line:

```python
        Route("/subscribe", subscribe, methods=["POST"]),
        Route("/suggest", suggest, methods=["POST"]),
```

(`JSONResponse`, `store`, `security`, and `log` are already imported in `app.py`; `rate` and `conn` are already in scope inside `build_app`.)

- [ ] **Step 8: Run the endpoint tests to verify they pass**

Run: `uv run --extra dev python -m pytest tests/test_app.py -k "subscribe or suggest" -v`
Expected: PASS (8 tests).

- [ ] **Step 9: Full suite still green**

Run: `uv run --extra dev python -m pytest -q`
Expected: 216 passed (207 + 1 security + 8 app).

- [ ] **Step 10: Commit**

```bash
git add src/missingmcp/security.py src/missingmcp/app.py tests/test_security.py tests/test_app.py
git commit -m "feat(app): /subscribe and /suggest opt-in endpoints (rate-limit + honeypot + validation)"
```

---

### Task 3: Frontend — modals, buttons, styles, JS behavior

**Files:**
- Modify: `src/missingmcp/templates/home.html` (replace the GitHub link in the "Missing something?" card with two modal buttons; add two `<dialog>` blocks)
- Modify: `src/missingmcp/templates/_layout.html` (CSS for `.linklike`, `dialog.modal`, `.modal-card`, honeypot `.hp`)
- Modify: `src/missingmcp/static/site.js` (open/close/submit behavior)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `POST /subscribe`, `POST /suggest` (Task 2). Forms carry `data-endpoint="/subscribe"` / `"/suggest"`; buttons carry `data-modal="subscribe"` / `"suggest"`; each dialog id is `modal-<name>`.

- [ ] **Step 1: Write the failing markup tests**

Append to `tests/test_app.py`:

```python
def test_home_has_signup_modals_not_github_link(tmp_path):
    r = _client(tmp_path).get("/").text
    # the old GitHub-issues link in the card is gone (GitHub stays only in footer/security)
    assert "github.com/VelkyVenik/missingmcp/issues/new" not in r
    # two buttons open the two modals
    assert 'data-modal="suggest"' in r
    assert 'data-modal="subscribe"' in r
    # the two dialogs exist and post to the right endpoints
    assert 'id="modal-subscribe"' in r and 'data-endpoint="/subscribe"' in r
    assert 'id="modal-suggest"' in r and 'data-endpoint="/suggest"' in r
    # honeypot present in each form, and the opt-in checkbox on the suggest form
    assert r.count('name="website"') == 2
    assert 'name="wants_updates"' in r


def test_github_still_reachable_in_footer(tmp_path):
    # removing the card link must not remove GitHub from the site entirely
    r = _client(tmp_path).get("/").text
    assert 'href="https://github.com/VelkyVenik/missingmcp"' in r


def test_site_js_has_modal_behavior(tmp_path):
    r = _client(tmp_path).get("/static/site.js").text
    assert "data-modal" in r and "showModal" in r
    assert "data-endpoint" in r and "fetch(" in r
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --extra dev python -m pytest tests/test_app.py -k "modal or github_still or site_js_has" -v`
Expected: FAIL — the issues link is still present; `data-modal` / `showModal` not found.

- [ ] **Step 3: Replace the card link with two modal buttons**

In `src/missingmcp/templates/home.html`, replace this block:

```html
        <div class="card">
          <h3>Missing something?</h3>
          <p>Every connector here started as &ldquo;I wish Claude could&hellip;&rdquo;. Tell me which app should be next.</p>
          <a class="go" href="https://github.com/VelkyVenik/missingmcp/issues/new">Suggest a connector &rarr;</a>
        </div>
```

with:

```html
        <div class="card">
          <h3>Missing something?</h3>
          <p>Every connector here started as &ldquo;I wish Claude could&hellip;&rdquo;. Tell me which app should be next &mdash; or just get a ping when a new one lands.</p>
          <div class="card-actions">
            <button class="linklike" type="button" data-modal="suggest">Suggest a connector &rarr;</button>
            <button class="linklike" type="button" data-modal="subscribe">Notify me about new connectors &rarr;</button>
          </div>
        </div>
```

- [ ] **Step 4: Add the two dialogs**

In `src/missingmcp/templates/home.html`, at the very end of the file (after the closing `</div>` of the `.final` block), append:

```html
  <dialog id="modal-subscribe" class="modal">
    <form class="modal-card" data-endpoint="/subscribe">
      <button class="modal-x" type="button" data-close aria-label="Close">&times;</button>
      <h3>Get notified about new connectors</h3>
      <p class="modal-sub">Leave your email and I&rsquo;ll let you know when a new connector goes live. No spam.</p>
      <input class="hp" type="text" name="website" tabindex="-1" autocomplete="off" aria-hidden="true">
      <label>Email<input name="email" type="email" required placeholder="you@example.com"></label>
      <button class="btn" type="submit">Notify me</button>
      <p class="modal-msg" data-msg></p>
    </form>
  </dialog>

  <dialog id="modal-suggest" class="modal">
    <form class="modal-card" data-endpoint="/suggest">
      <button class="modal-x" type="button" data-close aria-label="Close">&times;</button>
      <h3>Suggest a connector</h3>
      <p class="modal-sub">Which app should Claude speak to next? Tell me what you&rsquo;re missing.</p>
      <input class="hp" type="text" name="website" tabindex="-1" autocomplete="off" aria-hidden="true">
      <label>Email<input name="email" type="email" required placeholder="you@example.com"></label>
      <label>What&rsquo;s missing?<textarea name="description" rows="3" placeholder="e.g. Strava, Oura, Fitbit&hellip;"></textarea></label>
      <label class="check"><input type="checkbox" name="wants_updates" value="1"> Also notify me when new connectors go live</label>
      <button class="btn" type="submit">Send suggestion</button>
      <p class="modal-msg" data-msg></p>
    </form>
  </dialog>
```

- [ ] **Step 5: Add the CSS**

In `src/missingmcp/templates/_layout.html`, inside the `<style>` block, add after the `.auth .after { ... }` rule (end of the auth-form group):

```css
    .card-actions { display: flex; flex-direction: column; align-items: flex-start; gap: .5rem; margin-top: .8rem; }
    .linklike { background: none; border: 0; padding: 0; font: inherit; font-weight: 600;
                font-size: .92rem; color: var(--accent); cursor: pointer; }
    .linklike:hover { text-decoration: underline; }

    dialog.modal { border: 1px solid var(--border); border-radius: 16px; background: var(--bg-card);
                   color: var(--text); padding: 0; max-width: 27rem; width: calc(100% - 2rem); }
    dialog.modal::backdrop { background: rgba(0, 0, 0, .5); }
    .modal-card { position: relative; padding: 1.6rem 1.5rem; }
    .modal-card h3 { font-size: 1.25rem; margin-bottom: .4rem; }
    .modal-sub { font-size: .9rem; color: var(--muted); margin-bottom: 1rem; }
    .modal-card label { display: block; font-size: .85rem; font-weight: 600; color: var(--muted); margin-top: .8rem; }
    .modal-card input:not([type="checkbox"]):not(.hp), .modal-card textarea {
                 display: block; width: 100%; margin-top: .35rem; padding: .6rem .8rem;
                 border: 1px solid var(--border); border-radius: 10px; background: var(--bg);
                 color: var(--text); font: inherit; }
    .modal-card textarea { resize: vertical; }
    .modal-card label.check { display: flex; align-items: center; gap: .5rem; font-weight: 500; margin-top: 1rem; }
    .modal-card label.check input { margin: 0; }
    .modal-card .btn { display: block; width: 100%; margin-top: 1.2rem; border: 0; cursor: pointer; font: inherit; font-weight: 600; }
    .modal-x { position: absolute; top: .6rem; right: .8rem; background: none; border: 0;
               font-size: 1.5rem; line-height: 1; color: var(--dim); cursor: pointer; }
    .modal-msg { margin-top: .8rem; font-size: .9rem; min-height: 1.1em; }
    .modal-msg.ok { color: var(--live); }
    .modal-msg.err { color: var(--err); }
    .hp { position: absolute !important; left: -9999px; width: 1px; height: 1px; opacity: 0; }
```

- [ ] **Step 6: Add the JS behavior**

Append to `src/missingmcp/static/site.js`:

```javascript

// Signup / suggestion modals. CSP (default-src 'self') allows the same-origin
// fetch below; no inline scripts, so all behavior lives in this one file.
document.addEventListener("click", function (e) {
  var opener = e.target.closest("[data-modal]");
  if (opener) {
    var dlg = document.getElementById("modal-" + opener.dataset.modal);
    if (dlg && dlg.showModal) { dlg.showModal(); }
    return;
  }
  if (e.target.closest("[data-close]")) {
    var card = e.target.closest("dialog");
    if (card) { card.close(); }
    return;
  }
  // Click on the backdrop (the <dialog> element itself, outside the card) closes it.
  if (e.target.tagName === "DIALOG") { e.target.close(); }
});

document.addEventListener("submit", function (e) {
  var form = e.target.closest("form[data-endpoint]");
  if (!form) { return; }
  e.preventDefault();
  var msg = form.querySelector("[data-msg]");
  var btn = form.querySelector("button[type=submit]");
  if (btn) { btn.disabled = true; }
  fetch(form.dataset.endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(new FormData(form)),
  }).then(function (r) {
    return r.json().catch(function () { return { ok: r.ok }; });
  }).then(function (data) {
    if (data.ok) {
      // hide the inputs and show a thank-you; the visitor can just close the modal
      form.querySelectorAll("label, button[type=submit]").forEach(function (el) {
        el.style.display = "none";
      });
      if (msg) {
        msg.className = "modal-msg ok";
        msg.textContent = "Thanks! I’ll be in touch when there’s something new.";
      }
    } else {
      if (btn) { btn.disabled = false; }
      if (msg) {
        msg.className = "modal-msg err";
        msg.textContent = data.error === "invalid_email"
          ? "That email doesn’t look right — please check it."
          : data.error === "rate_limited"
          ? "Too many tries — wait a minute and try again."
          : "Something went wrong — please try again.";
      }
    }
  }).catch(function () {
    if (btn) { btn.disabled = false; }
    if (msg) {
      msg.className = "modal-msg err";
      msg.textContent = "Network error — please try again.";
    }
  });
});
```

(An unchecked checkbox is omitted from `FormData` by spec, so `wants_updates` arrives only when ticked — matching the server's `in ("1", ...)` check. The honeypot text input is always sent empty by real users.)

- [ ] **Step 7: Run the markup/JS tests to verify they pass**

Run: `uv run --extra dev python -m pytest tests/test_app.py -k "modal or github_still or site_js_has" -v`
Expected: PASS (3 tests).

- [ ] **Step 8: Manual smoke check (JS can't be exercised by TestClient)**

Run the gateway locally and click through both modals:

```bash
GATEWAY_SECRET="$(openssl rand -base64 48)" PUBLIC_URL=http://localhost:8088 PORT=8088 \
  DATA_DIR=./.localdata uv run missingmcp
```

Verify at `http://localhost:8088`: both buttons open their modal; submitting a valid email shows the green thanks; an invalid email shows the red error; backdrop/× closes; then confirm rows landed: `uv run --extra dev python -c "import sqlite3; print(sqlite3.connect('./.localdata/gateway.db').execute('select * from subscribers').fetchall())"`.

- [ ] **Step 9: Full suite still green**

Run: `uv run --extra dev python -m pytest -q`
Expected: 219 passed (216 + 3).

- [ ] **Step 10: Commit**

```bash
git add src/missingmcp/templates/home.html src/missingmcp/templates/_layout.html src/missingmcp/static/site.js tests/test_app.py
git commit -m "feat(site): signup + suggestion modals on the home page"
```

---

### Task 4: `scripts/subscribers.py` — list/export

**Files:**
- Create: `scripts/subscribers.py`
- Test: `tests/test_scripts.py`

**Interfaces:**
- Consumes: the `subscribers` / `suggestions` tables (Task 1). Opens the DB read-only (`mode=ro`), like `scripts/status.py`. `main()` is driven with a patched `argv` in tests.
- Produces: default table view (counts + rows) to stdout; `--emails` prints one subscriber email per line for pasting into an email tool.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_scripts.py`. First extend the `seeded_db` fixture to add signup data — add these lines just before `conn.close()` in the fixture:

```python
    store.add_subscriber(conn, "sub@example.com")
    store.add_suggestion(conn, "wanter@example.com", "Strava please", wants_updates=True)
    store.add_suggestion(conn, "nomail@example.com", "Oura", wants_updates=False)
```

Then add the tests:

```python
# --- subscribers.py --------------------------------------------------------

def test_subscribers_lists_subscribers_and_suggestions(seeded_db, capsys, monkeypatch):
    out = run_script("subscribers", ["--db", seeded_db], capsys, monkeypatch)
    assert "sub@example.com" in out
    assert "wanter@example.com" in out          # opted in via suggestion checkbox
    assert "Strava please" in out               # suggestion description shown
    assert "Newsletter subscribers: 2" in out   # sub@ + wanter@ (nomail@ opted out)
    assert "Connector suggestions: 2" in out


def test_subscribers_emails_only_mode(seeded_db, capsys, monkeypatch):
    out = run_script("subscribers", ["--db", seeded_db, "--emails"], capsys, monkeypatch)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert set(lines) == {"sub@example.com", "wanter@example.com"}
    assert "Strava" not in out                  # emails-only: no descriptions/headers
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --extra dev python -m pytest tests/test_scripts.py -k subscribers -v`
Expected: FAIL — `scripts/subscribers.py` doesn't exist (import/spec error).

- [ ] **Step 3: Create the script**

Create `scripts/subscribers.py`:

```python
#!/usr/bin/env python3
"""List newsletter subscribers and connector suggestions (reads the DB read-only).

No email is sent from here — this is capture-only until a sending provider is
wired up. Use --emails to get a plain list to paste into an email tool.

Usage:
  python scripts/subscribers.py                 # table view (counts + rows)
  python scripts/subscribers.py --emails        # subscriber emails, one per line
  python scripts/subscribers.py --db /data/gateway.db
  railway ssh --service gateway "python3 /app/scripts/subscribers.py --emails"
"""
from __future__ import annotations
import argparse
import os
import sqlite3
import sys


def resolve_db() -> str:
    if os.environ.get("DB_PATH"):
        return os.environ["DB_PATH"]
    if os.environ.get("DATA_DIR"):
        return os.path.join(os.environ["DATA_DIR"], "gateway.db")
    for cand in ("/data/gateway.db", "./.localdata/gateway.db"):
        if os.path.exists(cand):
            return cand
    return "/data/gateway.db"


def _rows(db, sql):
    # Tolerate a DB the gateway hasn't opened since this feature shipped.
    try:
        return db.execute(sql).fetchall()
    except sqlite3.OperationalError:
        return []


def main():
    p = argparse.ArgumentParser(description="MissingMCP subscribers & suggestions.")
    p.add_argument("--db", default=None,
                   help="SQLite DB path (default: $DB_PATH, $DATA_DIR/gateway.db, "
                        "/data/gateway.db, or ./.localdata/gateway.db)")
    p.add_argument("--emails", action="store_true",
                   help="print only subscriber emails, one per line")
    args = p.parse_args()
    db_path = args.db or resolve_db()
    if not os.path.exists(db_path):
        sys.exit(f"DB not found: {db_path}\nSet --db, DB_PATH or DATA_DIR.")

    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    subs = _rows(db, "SELECT email, created_at FROM subscribers ORDER BY created_at")
    if args.emails:
        for s in subs:
            print(s["email"])
        return

    print(f"\nMissingMCP — subscribers & suggestions  ({db_path})\n")
    print(f"Newsletter subscribers: {len(subs)}")
    for s in subs:
        print(f"  {s['email']:<40} since {s['created_at']}")

    sugg = _rows(db, "SELECT email, description, wants_updates, created_at "
                     "FROM suggestions ORDER BY created_at")
    print(f"\nConnector suggestions: {len(sugg)}")
    for s in sugg:
        flag = " (+updates)" if s["wants_updates"] else ""
        print(f"  {s['created_at']}  {s['email']}{flag}")
        desc = (s["description"] or "").replace("\n", " ").strip()
        if desc:
            print(f"      {desc}")
    print()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --extra dev python -m pytest tests/test_scripts.py -k subscribers -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Full suite still green**

Run: `uv run --extra dev python -m pytest -q`
Expected: 221 passed (219 + 2).

- [ ] **Step 6: Commit**

```bash
git add scripts/subscribers.py tests/test_scripts.py
git commit -m "feat(scripts): subscribers.py — list/export newsletter signups & suggestions"
```

---

### Task 5: Privacy copy + docs

**Files:**
- Modify: `src/missingmcp/templates/privacy.html`
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Test: `tests/test_app.py`

No code cycle beyond the one privacy assertion — the rest is reviewed text; weave minimally into existing structure.

- [ ] **Step 1: Write the failing privacy test**

Append to `tests/test_app.py`:

```python
def test_privacy_mentions_signup_storage_and_deletion(tmp_path):
    r = _client(tmp_path).get("/privacy").text
    assert "newsletter" in r.lower() or "notify" in r.lower()   # opt-in disclosed
    assert "email the operator" in r.lower()                    # deletion path
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run --extra dev python -m pytest tests/test_app.py::test_privacy_mentions_signup_storage_and_deletion -v`
Expected: FAIL (unless "newsletter"/"notify" already present — if it passes by accident, still do Step 3 for the wording).

- [ ] **Step 3: Update `privacy.html`**

In `src/missingmcp/templates/privacy.html`, in the "What we store" `<ul>` (the `#stored` section), add this `<li>` after the tool-usage-counters bullet:

```html
          <li>If you opt in on the home page: your <strong>email address</strong> (so we can tell you when a new connector launches) and any connector suggestion you send. Only if you choose to &mdash; and never used for anything else.</li>
```

Then in the `#revoke` section, update the "Delete everything" card's `<p>` to name the signup explicitly:

Replace:
```html
          <p>Email the operator{OPERATOR_EMAIL} to have every stored record of your account deleted.</p>
```
with:
```html
          <p>Email the operator{OPERATOR_EMAIL} to have every stored record &mdash; including any newsletter signup &mdash; deleted.</p>
```

- [ ] **Step 4: Run the privacy test to verify it passes**

Run: `uv run --extra dev python -m pytest tests/test_app.py::test_privacy_mentions_signup_storage_and_deletion -v`
Expected: PASS.

- [ ] **Step 5: Update `README.md` — Monitoring section**

In the Monitoring section's script block, add a line after the `usage.py` entries:

```bash
python scripts/subscribers.py                         # newsletter signups + suggestions
python scripts/subscribers.py --emails                # subscriber emails, one per line
```

- [ ] **Step 6: Update `CLAUDE.md`**

Two minimal edits:
- In the **`store.py`** module bullet, add `subscribers` and `suggestions` to the table list: "... `tool_usage` (per-account metrics), `subscribers` (newsletter opt-in email, PK email) and `suggestions` (connector-request log)."
- In the **`app.py`** module bullet (or the routing invariants), note the two anonymous opt-in endpoints: "plus two unauthenticated public opt-in endpoints — `POST /subscribe` and `POST /suggest` — capturing home-page signups/suggestions (rate-limited + honeypot + email-format check; storage only, no email sent)."
- Optionally, in the Commands/testing note, add: "If `uv run --extra dev pytest` reports `Failed to spawn: pytest`, use `uv run --extra dev python -m pytest` — the module runs even when the console script isn't on the venv PATH."

- [ ] **Step 7: Full suite still green**

Run: `uv run --extra dev python -m pytest -q`
Expected: 222 passed (221 + 1).

- [ ] **Step 8: Commit**

```bash
git add src/missingmcp/templates/privacy.html README.md CLAUDE.md tests/test_app.py
git commit -m "docs: disclose signup storage in privacy policy; document subscribers.py"
```

---

## Self-review notes

- **Spec coverage:** local-only storage (Tasks 1, 4 — no sending anywhere); `subscribers` + `suggestions` tables with `wants_updates` cross-link (Task 1); modal UX with vanilla JS, CSP-safe fetch, inline success (Task 3); rate-limit + email validation + honeypot + silent dedup (Task 2); two buttons / two modals / two endpoints per the confirmed layout (Tasks 2, 3); GitHub link removed from the card but kept in the footer (Task 3 + `test_github_still_reachable_in_footer`); `scripts/subscribers.py` export (Task 4); privacy copy + deletion path, no unsubscribe link, no extra consent checkbox beyond the suggest-modal opt-in (Task 5).
- **Type/name consistency:** `add_subscriber(conn, email)`, `add_suggestion(conn, email, description, wants_updates: bool)`, `list_subscribers`, `list_suggestions` (Tasks 1/4); `security.valid_email(email) -> bool` (Task 2); form field names `email` / `description` / `wants_updates` / `website` and dialog ids `modal-subscribe` / `modal-suggest` / `data-endpoint` values match across Tasks 2 and 3.
- **Test-count anchors** (baseline 203) are approximate — if `main` moved the baseline, the requirement is "everything green," not the literal number.
- **Out of scope (confirmed):** email sending, provider integration (Resend/other), double opt-in, unsubscribe link, per-suggestion notification to the operator. Revisit once signup volume justifies wiring a provider.
