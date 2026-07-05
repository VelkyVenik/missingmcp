# MissingMCP Homepage Landing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Garmin-only landing at `/` with the approved MissingMCP
benefit-first landing (spec `2026-07-05-homepage-messaging-design.md`) and move
the Garmin connect instructions to `/garmin`.

**Architecture:** Two server-rendered static templates. `landing.html` is
renamed to `garmin.html` (content verbatim) and served at `/garmin`; a new
`home.html` carries the approved homepage copy and is served at `/` and by the
404 catch-all. `app.py` renders both once at startup with the existing
`.replace()` placeholder chain. No new dependencies, no JS.

**Tech Stack:** Python 3.12, Starlette, plain HTML + inline CSS templates,
pytest via `uv run --extra dev pytest`.

## Global Constraints

- **Copy is approved verbatim** in `docs/superpowers/specs/2026-07-05-homepage-messaging-design.md` — do not reword; typographic entities (`&rarr;`, curly quotes) are fine.
- **`/garmin` page content stays verbatim** — the current `landing.html` already contains the required "built on garmin_mcp" credit; no copy changes there.
- **CSP is `default-src 'self'; style-src 'self' 'unsafe-inline'`** (`security.py`): no external fonts/images/scripts, no inline JS. Inline `<style>` only.
- **Placeholders:** templates may use `{PUBLIC_URL}`, `{OPERATOR_NAME}`, `{OPERATOR_EMAIL}` — replaced once in `build_app` (existing pattern).
- **Open question resolved (both repos verified public):** `Suggest a connector` → `https://github.com/VelkyVenik/missingmcp/issues/new`; footer `Source on GitHub` → `https://github.com/VelkyVenik/missingmcp`.
- **MCP server URL stays `{PUBLIC_URL}/mcp`** on the Garmin page — path-scoped routing (`/garmin/mcp`) is a separate work item (spec step 3), not this plan.
- Tests: `uv run --extra dev pytest -q` must be green at the end of every task.

---

### Task 1: Serve the Garmin instructions at `/garmin`

**Files:**
- Rename: `src/garmin_gateway/templates/landing.html` → `src/garmin_gateway/templates/garmin.html` (content unchanged)
- Modify: `src/garmin_gateway/app.py:35-48` (render helper + `/garmin` route)
- Test: `tests/test_app.py`

**Interfaces:**
- Produces: `_render(name: str) -> str` helper inside `build_app` (used by Task 2), template file `garmin.html`, route `GET /garmin`. `/` and the 404 catch-all still serve the Garmin page after this task (Task 2 repoints them).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_app.py`:

```python
def test_garmin_page(tmp_path):
    c = _client(tmp_path)
    r = c.get("/garmin")
    assert r.status_code == 200
    assert "How to connect" in r.text
    assert "https://gw.example.com/mcp" in r.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev pytest tests/test_app.py::test_garmin_page -v`
Expected: FAIL — `/garmin` currently hits the catch-all, which returns status 404.

- [ ] **Step 3: Rename the template and add the route**

```bash
git mv src/garmin_gateway/templates/landing.html src/garmin_gateway/templates/garmin.html
```

In `src/garmin_gateway/app.py`, replace the `landing = ...` block (lines 35–39)
and the two handlers below it with:

```python
    def _render(name: str) -> str:
        return (_TPL / name).read_text().replace(
            "{PUBLIC_URL}", config.public_url
        ).replace("{OPERATOR_NAME}", config.operator_name).replace(
            "{OPERATOR_EMAIL}", f" ({config.operator_email})" if config.operator_email else ""
        )

    garmin_page = _render("garmin.html")

    async def home(request):
        return HTMLResponse(garmin_page)

    async def garmin_landing(request):
        return HTMLResponse(garmin_page)

    async def notfound(request):
        # Catch-all for unknown GET paths: show the instructional landing page
        # (humans see how to connect) but with a 404 so API/discovery clients
        # still read it as "not here".
        return HTMLResponse(garmin_page, status_code=404)
```

In the `routes` list, add directly under the `Route("/", home, ...)` line:

```python
        Route("/garmin", garmin_landing, methods=["GET"]),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/test_app.py -v`
Expected: all PASS (including the pre-existing `test_landing_page` — `/` still serves the Garmin page).

- [ ] **Step 5: Run the full suite**

Run: `uv run --extra dev pytest -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add -A src/garmin_gateway/templates src/garmin_gateway/app.py tests/test_app.py
git commit -m "feat(pages): serve the Garmin connect instructions at /garmin"
```

---

### Task 2: MissingMCP landing at `/` (and as the 404 page)

**Files:**
- Create: `src/garmin_gateway/templates/home.html`
- Modify: `src/garmin_gateway/app.py` (point `home` + `notfound` at the new page)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `_render(name)` helper and `garmin_page` from Task 1.
- Produces: `home_page` rendered template; `/` and 404 catch-all serve it. `test_landing_page` is replaced by `test_home_page`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_app.py`, **replace** `test_landing_page` with:

```python
def test_home_page(tmp_path):
    c = _client(tmp_path)
    r = c.get("/")
    assert r.status_code == 200
    assert "Your apps," in r.text                 # hero H1
    assert 'href="/garmin"' in r.text             # Garmin card links to the subpage
    assert "Coming soon" in r.text                # Rohlík card
    assert "never stored" in r.text               # security section
    assert r.headers["x-frame-options"] == "DENY"


def test_unknown_path_serves_home_as_404(tmp_path):
    c = _client(tmp_path)
    r = c.get("/definitely-not-a-page")
    assert r.status_code == 404
    assert "Your apps," in r.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra dev pytest tests/test_app.py -v`
Expected: `test_home_page` and `test_unknown_path_serves_home_as_404` FAIL (the Garmin page contains none of the hero copy); the rest PASS.

- [ ] **Step 3: Create `src/garmin_gateway/templates/home.html`**

Full file content (copy is verbatim from the messaging spec):

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>MissingMCP — Your apps, in Claude</title>
  <meta name="description" content="Connect Garmin and more to Claude in two minutes. Sign in once, add a URL, start asking. Free and open source.">
  <link rel="icon" href="/favicon.svg" type="image/svg+xml">
  <style>
    :root {
      --bg: #ffffff; --bg-card: #f7f8fa; --border: #e5e7ee;
      --text: #191c23; --muted: #5a6172; --dim: #99a0b0;
      --accent: #4f46e5; --accent-soft: rgba(79, 70, 229, .09);
      --live: #059669; --live-soft: rgba(5, 150, 105, .1);
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #0e1013; --bg-card: #16191f; --border: #262b35;
        --text: #edeff3; --muted: #a2a9b8; --dim: #6b7280;
        --accent: #818cf8; --accent-soft: rgba(129, 140, 248, .12);
        --live: #34d399; --live-soft: rgba(52, 211, 153, .12);
      }
    }
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
           background: var(--bg); color: var(--text); line-height: 1.6; }
    .wrap { max-width: 960px; margin: 0 auto; padding: 0 1.25rem; }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }

    header { padding: 1.1rem 0; }
    header .wrap { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: .5rem; }
    .logo { font-weight: 700; letter-spacing: -.02em; color: var(--text); }
    nav a { color: var(--muted); font-size: .9rem; margin-left: 1.1rem; }

    .hero { padding: 4rem 0 3rem; }
    .badge { display: inline-block; font-size: .72rem; font-weight: 600; letter-spacing: .18em;
             text-transform: uppercase; color: var(--accent); background: var(--accent-soft);
             padding: .3rem .75rem; border-radius: 999px; margin-bottom: 1.4rem; }
    h1 { font-size: clamp(2.6rem, 7vw, 4.5rem); line-height: 1.02; letter-spacing: -.03em; max-width: 14ch; }
    .hl { color: var(--accent); }
    .hero p { margin-top: 1.5rem; max-width: 52ch; font-size: 1.15rem; color: var(--muted); }
    .cta-row { margin-top: 2.1rem; display: flex; flex-wrap: wrap; gap: 1.1rem; align-items: center; }
    .btn { display: inline-block; background: var(--accent); color: #fff; font-weight: 600;
           padding: .7rem 1.3rem; border-radius: 10px; }
    .btn:hover { text-decoration: none; opacity: .92; }
    .quiet { color: var(--muted); font-size: .95rem; }

    section { padding: 2.4rem 0; }
    .sec-h { display: flex; align-items: center; gap: .9rem; font-size: .75rem; font-weight: 600;
             letter-spacing: .2em; text-transform: uppercase; color: var(--dim); margin-bottom: 1.3rem; }
    .sec-h::after { content: ""; height: 1px; flex: 1; background: var(--border); }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: .9rem; }
    .card { border: 1px solid var(--border); background: var(--bg-card); border-radius: 16px; padding: 1.3rem; }
    .card h3 { font-size: 1.05rem; letter-spacing: -.01em; margin: .35rem 0 .4rem; }
    .card p { font-size: .92rem; color: var(--muted); }
    .card .go { display: inline-block; margin-top: .8rem; font-weight: 600; font-size: .92rem; }
    .pill { display: inline-block; font-size: .66rem; font-weight: 700; letter-spacing: .08em;
            text-transform: uppercase; padding: .18rem .6rem; border-radius: 999px; }
    .pill.live { color: var(--live); background: var(--live-soft); }
    .pill.soon { color: var(--dim); background: var(--border); }
    .step-n { font-size: .72rem; font-weight: 600; letter-spacing: .06em; color: var(--dim); text-transform: uppercase; }

    .lede { max-width: 52ch; color: var(--muted); margin-bottom: 1.3rem; margin-top: -.4rem; }
    .trust { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: .9rem; }
    .trust div { border-left: 3px solid var(--accent-soft); padding: .2rem 0 .2rem 1rem; }
    .trust h3 { font-size: .98rem; margin-bottom: .25rem; }
    .trust p { font-size: .9rem; color: var(--muted); }

    .final { padding: 4rem 0; }
    .final .inner { border: 1px solid var(--border); background: var(--bg-card); border-radius: 24px;
                    padding: 3rem 1.5rem; text-align: center; }
    .final h2 { font-size: clamp(1.9rem, 5vw, 3rem); letter-spacing: -.02em; line-height: 1.05; }
    .final p { margin: 1rem auto 1.8rem; max-width: 44ch; color: var(--muted); }
    .beer { margin-top: 1.4rem; font-size: .9rem; color: var(--muted); }

    footer { border-top: 1px solid var(--border); padding: 1.6rem 0 2.2rem; font-size: .85rem; color: var(--dim); }
    footer .wrap > div + div { margin-top: .3rem; }
  </style>
</head>
<body>
  <header>
    <div class="wrap">
      <span class="logo">MissingMCP</span>
      <nav>
        <a href="#connectors">Connectors</a>
        <a href="#how">How it works</a>
        <a href="#security">Security</a>
        <a href="https://github.com/VelkyVenik/missingmcp">GitHub</a>
      </nav>
    </div>
  </header>

  <div class="hero">
    <div class="wrap">
      <span class="badge">The missing connectors</span>
      <h1>Your apps, <span class="hl">in Claude</span>.</h1>
      <p>Ask about last night&rsquo;s sleep. Reorder this week&rsquo;s groceries. MissingMCP hosts the connectors your favorite services are missing &mdash; sign in once, add a URL, and start asking.</p>
      <div class="cta-row">
        <a class="btn" href="#connectors">Browse connectors</a>
        <a class="quiet" href="#how">How it works &darr;</a>
      </div>
    </div>
  </div>

  <section id="connectors">
    <div class="wrap">
      <h2 class="sec-h">Connectors</h2>
      <div class="cards">
        <div class="card">
          <span class="pill live">Live</span>
          <h3>Garmin</h3>
          <p>Your training, sleep, and health data. Ask how you slept, review yesterday&rsquo;s ride, or have Claude plan next week&rsquo;s workouts.</p>
          <a class="go" href="/garmin">Connect Garmin &rarr;</a>
        </div>
        <div class="card">
          <span class="pill soon">Coming soon</span>
          <h3>Rohl&iacute;k</h3>
          <p>Groceries from Claude. Reorder your usuals, turn a recipe into a cart, check delivery slots.</p>
        </div>
        <div class="card">
          <h3>Missing something?</h3>
          <p>Every connector here started as &ldquo;I wish Claude could&hellip;&rdquo;. Tell me which app should be next.</p>
          <a class="go" href="https://github.com/VelkyVenik/missingmcp/issues/new">Suggest a connector &rarr;</a>
        </div>
      </div>
    </div>
  </section>

  <section id="how">
    <div class="wrap">
      <h2 class="sec-h">How it works</h2>
      <div class="cards">
        <div class="card">
          <span class="step-n">Step 1</span>
          <h3>Pick a connector</h3>
          <p>Choose an app from the list and copy its server URL.</p>
        </div>
        <div class="card">
          <span class="step-n">Step 2</span>
          <h3>Add it to Claude</h3>
          <p>Settings &rarr; Connectors &rarr; Add custom connector. Works on phone, desktop, and web.</p>
        </div>
        <div class="card">
          <span class="step-n">Step 3</span>
          <h3>Sign in once</h3>
          <p>Claude opens a sign-in page &mdash; log in with your own account. Your password is used once and never stored.</p>
        </div>
        <div class="card">
          <span class="step-n">Step 4</span>
          <h3>Start asking</h3>
          <p>Claude picks up the tools automatically. &ldquo;How did I sleep this week?&rdquo; just works.</p>
        </div>
      </div>
    </div>
  </section>

  <section id="security">
    <div class="wrap">
      <h2 class="sec-h">Security &amp; trust</h2>
      <p class="lede">You&rsquo;re signing in with real credentials &mdash; here&rsquo;s exactly how they&rsquo;re handled.</p>
      <div class="trust">
        <div>
          <h3>Password: used once, never stored</h3>
          <p>Your password signs you in to the service and is immediately discarded. Only the resulting session tokens are kept &mdash; encrypted with AES-256-GCM.</p>
        </div>
        <div>
          <h3>Standard OAuth 2.1</h3>
          <p>Claude never sees your credentials. It gets its own revocable token, over the same OAuth + PKCE flow that banks and APIs use.</p>
        </div>
        <div>
          <h3>Your data passes through, nothing sticks</h3>
          <p>Your health and shopping data flows from the service to Claude on demand. The gateway stores none of it.</p>
        </div>
        <div>
          <h3>Open source, run by a person</h3>
          <p>This instance is run by {OPERATOR_NAME}. The full source is <a href="https://github.com/VelkyVenik/missingmcp">on GitHub</a> &mdash; audit it, or run your own.</p>
        </div>
      </div>
    </div>
  </section>

  <div class="final">
    <div class="wrap">
      <div class="inner">
        <h2>Give Claude the <span class="hl">missing</span> piece.</h2>
        <p>Your first connector is two minutes away &mdash; free, open source, and no server of your own.</p>
        <a class="btn" href="#connectors">Browse connectors</a>
        <p class="beer">Like MissingMCP and want to support it? <a href="https://buymeacoffee.com/venik" target="_blank" rel="noopener noreferrer">&#127866; Buy me a beer</a></p>
      </div>
    </div>
  </div>

  <footer>
    <div class="wrap">
      <div>MissingMCP &middot; The connectors Claude is missing.</div>
      <div><a href="https://github.com/VelkyVenik/missingmcp">Source on GitHub</a> &middot; Run by {OPERATOR_NAME}</div>
    </div>
  </footer>
</body>
</html>
```

- [ ] **Step 4: Point `/` and the 404 catch-all at the new page**

In `src/garmin_gateway/app.py`, directly under the `garmin_page = _render("garmin.html")`
line add:

```python
    home_page = _render("home.html")
```

and change the `home` and `notfound` handlers to:

```python
    async def home(request):
        return HTMLResponse(home_page)

    async def notfound(request):
        # Catch-all for unknown GET paths: humans get the MissingMCP home
        # (with links to every connector) but with a 404 status so
        # API/discovery clients still read it as "not here".
        return HTMLResponse(home_page, status_code=404)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run --extra dev pytest tests/test_app.py -v`
Expected: all PASS.

- [ ] **Step 6: Run the full suite**

Run: `uv run --extra dev pytest -q`
Expected: all PASS.

- [ ] **Step 7: Eyeball the page locally**

```bash
GATEWAY_SECRET="$(openssl rand -base64 48)" PUBLIC_URL=http://localhost:8088 PORT=8088 \
  DATA_DIR=./.localdata uv run garmin-gateway
```

Open `http://localhost:8088/` — check hero, the three connector cards, the four
steps, the security section (operator name filled in), final CTA, footer; check
`http://localhost:8088/garmin` still shows the connect instructions; check an
unknown path (e.g. `/xyz`) renders the home with a 404. Try light and dark
system themes.

- [ ] **Step 8: Commit**

```bash
git add src/garmin_gateway/templates/home.html src/garmin_gateway/app.py tests/test_app.py
git commit -m "feat(pages): MissingMCP benefit-first landing at / (spec 2026-07-05)"
```
