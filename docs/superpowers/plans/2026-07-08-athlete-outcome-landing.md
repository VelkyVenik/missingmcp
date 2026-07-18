# Athlete-Focused, Outcome-First Landing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite the landing copy so the front door leads with the outcome
("your data, in Claude" + a cross-source "just ask" demo) instead of MCP jargon,
while keeping the MissingMCP brand, domain, and SEO keywords intact.

**Architecture:** Pure template/copy change. The site is server-rendered HTML
fragments (`templates/*.html`) wrapped by `pages.render_page` in
`templates/_layout.html`; page `<title>`/`<meta description>` for `/` are set in
`app.py` (not the template). Tests drive the real app with Starlette's
`TestClient` and assert on returned HTML substrings — so TDD here is: assert the
new copy is present, watch it fail, edit the template/`app.py`, watch it pass.

**Tech Stack:** Python 3.12, Starlette, `uv`, pytest. No new dependencies.

## Global Constraints

- **Python 3.12**; source under `src/missingmcp/`, tests under `tests/`.
- **Run tests with the dev extra:** `uv run --extra dev pytest -q` (pytest lives
  in `[project.optional-dependencies].dev`; plain `uv run pytest` fails).
- **Keep the H1 as-is:** `Your data, <span class="hl">in Claude</span>.` — do not touch.
- **`<title>` MUST retain the exact substring `Garmin MCP Server`** — it is the
  SEO query target and is asserted by `tests/test_app.py::test_seo_head_meta`.
  Lead with the human promise, keep the keyword as a tail.
- **"MCP" is under-the-hood + brand + SEO only** — never in the visible
  hero/body as the entry point. Do not delete it from `<title>`, footer brand
  line, the `/garmin` "under the hood" mention, or the "No longer missing" section.
- **Copy is English.** Match the existing typographic-entity style in templates
  (`&rsquo;`, `&mdash;`, `&ldquo;`/`&rdquo;`, `&nbsp;`), not raw Unicode punctuation.
- **Do not touch** security copy, the Connectors section, the connector/adapter
  system, OAuth, or the layout framework. Scope is landing copy + one new
  section + its CSS + the footer brand line.

## File Structure

- `src/missingmcp/templates/home.html` — hero badge + subhead rewrite; insert the
  new `#just-ask` section between the hero and `#connectors`.
- `src/missingmcp/templates/_layout.html` — footer brand line; add CSS for the
  chat bubbles + chips used by `#just-ask`.
- `src/missingmcp/templates/garmin.html` — reorder two blocks outcome-first,
  keeping a one-line "under the hood" MCP mention.
- `src/missingmcp/app.py` — home page `<title>` + `<meta description>` (lines ~62–68).
- `tests/test_app.py` — new assertions for the hero/just-ask/footer copy; update
  the footer assertion in `test_subpages_share_site_chrome`.

---

### Task 1: Hero subhead, badge, and home `<title>`/meta

**Files:**
- Modify: `src/missingmcp/templates/home.html:3` (badge), `:5` (subhead)
- Modify: `src/missingmcp/app.py:62-68` (home `<title>` + description)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: no code symbols; downstream tasks only rely on the page rendering.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_app.py`:

```python
def test_hero_leads_with_outcome(tmp_path):
    home = _client(tmp_path).get("/").text
    # badge names the niche (CSS uppercases it)
    assert "Built for athletes" in home
    # subhead leads with the outcome, not "connectors" / "MCP server"
    assert "except the numbers your apps keep locked away" in home
    assert "an answer that actually knows" in home
    # <title> leads with the promise AND keeps the SEO keyword tail
    assert "Your data, in Claude" in home          # from <title>/og:title
    assert "Garmin MCP Server" in home             # SEO keyword retained
    # the old jargon-first subhead phrasing is gone
    assert "hosts the connectors your favorite services are missing" not in home
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev pytest tests/test_app.py::test_hero_leads_with_outcome -v`
Expected: FAIL — `"except the numbers your apps keep locked away"` not found.

- [ ] **Step 3: Rewrite the badge and subhead in `home.html`**

Replace line 3:

```html
      <span class="badge">The missing connectors</span>
```

with:

```html
      <span class="badge">Built for athletes</span>
```

Replace line 5:

```html
      <p>Ask about last night&rsquo;s sleep. Review yesterday&rsquo;s ride. MissingMCP hosts the connectors your favorite services are missing &mdash; starting with a hosted <strong>Garmin MCP server</strong>. Sign in once, add a URL, and start asking.</p>
```

with:

```html
      <p>Claude can reason about almost anything &mdash; except the numbers your apps keep locked away. MissingMCP hands it your real data, starting with Garmin, so you can just ask: <em>&ldquo;Did I eat enough for today&rsquo;s ride?&rdquo;</em> &mdash; and get an answer that actually knows.</p>
```

- [ ] **Step 4: Rewrite the home `<title>` and meta description in `app.py`**

Replace lines 62–68:

```python
    home_page = _render(
        "home.html", "MissingMCP — Garmin MCP Server & the Connectors Claude Is Missing",
        "Connect Garmin to Claude in two minutes with a hosted Garmin MCP server "
        "— sleep, training, and health data by asking. Sign in once, add a URL. "
        "Free, open source, more connectors on the way.",
        extra_head=_json_ld({"@type": "WebSite", "name": "MissingMCP",
                             "url": config.public_url}))
```

with:

```python
    home_page = _render(
        "home.html", "MissingMCP — Your data, in Claude · Garmin MCP Server",
        "Give Claude your Garmin and health data, then just ask — did I eat "
        "enough for today's ride, how did I sleep this week? A hosted Garmin "
        "MCP server: connect in two minutes. Free and open source.",
        extra_head=_json_ld({"@type": "WebSite", "name": "MissingMCP",
                             "url": config.public_url}))
```

(The meta description carries no double-quote characters, so it stays valid
inside the layout's `content="{DESC}"` attribute.)

- [ ] **Step 5: Run the new test plus the SEO + home guards**

Run: `uv run --extra dev pytest tests/test_app.py::test_hero_leads_with_outcome tests/test_app.py::test_seo_head_meta tests/test_app.py::test_home_page -v`
Expected: PASS (3 passed). Confirms the new copy landed and no SEO/home invariant broke.

- [ ] **Step 6: Commit**

```bash
git add tests/test_app.py src/missingmcp/templates/home.html src/missingmcp/app.py
git commit -m "feat(site): hero + title lead with the outcome, keep MCP SEO tail"
```

---

### Task 2: The "Just ask" demonstration section

**Files:**
- Modify: `src/missingmcp/templates/home.html` (insert a `<section id="just-ask">` between the hero `</div>` and `<section id="connectors">`)
- Modify: `src/missingmcp/templates/_layout.html` (add chat/chip CSS inside `<style>`)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: existing layout CSS tokens (`--accent`, `--accent-soft`, `--bg-card`, `--border`, `--muted`, `--dim`).
- Produces: a section anchored `#just-ask`; classes `.chat`, `.bubble`, `.bubble.you`, `.bubble.claude`, `.bubble .who`, `.chat-cap`, `.chips`, `.chip`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_app.py`:

```python
def test_just_ask_section(tmp_path):
    home = _client(tmp_path).get("/").text
    assert "Just ask" in home                                   # section heading
    assert "Am I on track for today" in home                    # the "You" bubble
    assert "running a deficit for the load ahead" in home        # the "Claude" bubble
    assert "No single app does that." in home                    # the caption
    assert "How did I sleep this week?" in home                  # first (easy) chip
    assert "Compare my last three long runs." in home
    assert "Why was my recovery low today?" in home
    # the demo sits above the connector list
    assert home.index('id="just-ask"') < home.index('id="connectors"')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev pytest tests/test_app.py::test_just_ask_section -v`
Expected: FAIL — `"No single app does that."` not found.

- [ ] **Step 3: Insert the section in `home.html`**

In `src/missingmcp/templates/home.html`, immediately after the hero block's
closing `</div>` (the one right before `<section id="connectors">`) and before
`<section id="connectors">`, insert:

```html
  <section id="just-ask">
    <div class="wrap">
      <h2 class="sec-h">Just ask</h2>
      <p class="lede">No dashboards, no exports. A real question, a real answer &mdash; from your own data.</p>
      <div class="chat">
        <div class="bubble you"><span class="who">You</span>I had chicken and rice for lunch. Am I on track for today&rsquo;s long run?</div>
        <div class="bubble claude"><span class="who">Claude</span>Garmin has you at ~2,850&nbsp;kcal burned so far today, with a 90-minute run still to come (~950&nbsp;kcal). Lunch was ~620&nbsp;kcal &mdash; you&rsquo;re running a deficit for the load ahead. Grab a carb-heavy snack before you head out.</div>
      </div>
      <p class="chat-cap">Two sources &mdash; your Garmin and what you told Claude &mdash; connected in one answer. No single app does that.</p>
      <div class="chips">
        <span class="chip">&ldquo;How did I sleep this week?&rdquo;</span>
        <span class="chip">&ldquo;Compare my last three long runs.&rdquo;</span>
        <span class="chip">&ldquo;Why was my recovery low today?&rdquo;</span>
      </div>
    </div>
  </section>
```

- [ ] **Step 4: Add the CSS in `_layout.html`**

In `src/missingmcp/templates/_layout.html`, inside the `<style>` block, add these
rules immediately after the `.step-n { ... }` line (around line 113):

```css
    .chat { display: flex; flex-direction: column; gap: .7rem; max-width: 60ch; }
    .bubble { border: 1px solid var(--border); background: var(--bg-card);
              border-radius: 16px; border-bottom-left-radius: 4px;
              padding: .9rem 1.1rem; font-size: .95rem; }
    .bubble .who { display: block; font-size: .72rem; font-weight: 700;
                   letter-spacing: .06em; text-transform: uppercase;
                   color: var(--dim); margin-bottom: .3rem; }
    .bubble.claude { background: var(--accent-soft); border-color: var(--accent); }
    .chat-cap { max-width: 60ch; margin-top: 1rem; font-size: .9rem; color: var(--muted); }
    .chips { display: flex; flex-wrap: wrap; gap: .6rem; margin-top: 1.2rem; }
    .chip { border: 1px solid var(--border); background: var(--bg-card);
            border-radius: 999px; padding: .45rem .9rem; font-size: .88rem; color: var(--muted); }
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run --extra dev pytest tests/test_app.py::test_just_ask_section -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/test_app.py src/missingmcp/templates/home.html src/missingmcp/templates/_layout.html
git commit -m "feat(site): add the 'Just ask' cross-source demo section"
```

---

### Task 3: Jargon cleanup on `/garmin`

**Files:**
- Modify: `src/missingmcp/templates/garmin.html:9` (page-hero subhead), `:21` ("What is this?" opener)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: no code symbols.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_app.py`:

```python
def test_garmin_page_leads_with_outcome(tmp_path):
    g = _client(tmp_path).get("/garmin").text
    # page-hero subhead now opens on the outcome, not "A hosted Garmin MCP server:"
    assert "everything your watch knows" in g.lower()
    # the old jargon-first opener is gone
    assert "A hosted <strong>Garmin MCP server</strong>: everything" not in g
    # MCP is kept, but demoted to an under-the-hood aside
    assert "under the hood" in g.lower()
    assert "hosted MCP server" in g
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev pytest tests/test_app.py::test_garmin_page_leads_with_outcome -v`
Expected: FAIL — `"under the hood"` not found.

- [ ] **Step 3: Reorder the page-hero subhead (`garmin.html:9`)**

Replace:

```html
      <p>A hosted <strong>Garmin MCP server</strong>: everything your watch knows &mdash; training, sleep, recovery, health &mdash; available in a conversation. Ask questions, spot trends, and let Claude plan workouts that land right back on your wrist.</p>
```

with:

```html
      <p>Everything your watch knows &mdash; training, sleep, recovery, health &mdash; in a conversation. Ask questions, spot trends, and let Claude plan workouts that land right back on your wrist. <span class="quiet">(A hosted MCP server, under the hood.)</span></p>
```

- [ ] **Step 4: Reorder the "What is this?" opener (`garmin.html:21`)**

Replace:

```html
        <p>Garmin Connect collects an enormous amount of data about you &mdash; but exploring it means tapping through app screens, one metric at a time. This connector is a hosted <strong>MCP (Model Context Protocol) server for Garmin</strong>: it plugs your Garmin account into Claude, so you can just ask: <em>&ldquo;How did I sleep this week?&rdquo;</em>, <em>&ldquo;Compare my last three long runs&rdquo;</em>, <em>&ldquo;Why is my training readiness low today?&rdquo;</em></p>
```

with:

```html
        <p>Garmin Connect collects an enormous amount of data about you &mdash; but exploring it means tapping through app screens, one metric at a time. This plugs your Garmin account straight into Claude, so you can just ask: <em>&ldquo;How did I sleep this week?&rdquo;</em>, <em>&ldquo;Compare my last three long runs&rdquo;</em>, <em>&ldquo;Why is my training readiness low today?&rdquo;</em> Under the hood it&rsquo;s a hosted <strong>MCP (Model Context Protocol) server</strong> &mdash; but you never have to think about that.</p>
```

- [ ] **Step 5: Run the new test plus the existing garmin guard**

Run: `uv run --extra dev pytest tests/test_app.py::test_garmin_page_leads_with_outcome tests/test_app.py::test_garmin_page -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add tests/test_app.py src/missingmcp/templates/garmin.html
git commit -m "feat(site): /garmin leads with the outcome, MCP moved under the hood"
```

---

### Task 4: Footer brand line → the umbrella promise

**Files:**
- Modify: `src/missingmcp/templates/_layout.html:225` (footer brand line)
- Test: `tests/test_app.py:161` (inside `test_subpages_share_site_chrome`)

**Interfaces:**
- Consumes: nothing new.
- Produces: no code symbols.

- [ ] **Step 1: Update the shared-chrome test to the new tagline**

In `tests/test_app.py`, inside `test_subpages_share_site_chrome`, replace:

```python
        assert "The connectors Claude is missing." in r, path   # shared footer
```

with:

```python
        assert "Your data, in Claude." in r, path               # shared footer (umbrella promise)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev pytest tests/test_app.py::test_subpages_share_site_chrome -v`
Expected: FAIL — `"Your data, in Claude."` not found in the footer yet.

- [ ] **Step 3: Update the footer brand line in `_layout.html`**

Replace line 225:

```html
      <div><span>MissingMCP &middot; The connectors Claude is missing.</span> <span>Built by <a href="https://slajs.eu">Vaclav Slajs</a>.</span></div>
```

with:

```html
      <div><span>MissingMCP &middot; Your data, in Claude.</span> <span>Built by <a href="https://slajs.eu">Vaclav Slajs</a>.</span></div>
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run --extra dev pytest tests/test_app.py::test_subpages_share_site_chrome -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_app.py src/missingmcp/templates/_layout.html
git commit -m "feat(site): footer tagline is the umbrella promise, not connector jargon"
```

---

### Task 5: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Run the whole test suite**

Run: `uv run --extra dev pytest -q`
Expected: all tests pass (0 failed). If any legacy test asserted the old hero/footer/garmin copy and now fails, reconcile it against this plan's intended copy — do not revert the new copy.

- [ ] **Step 2: Eyeball the rendered page locally (optional but recommended)**

Run:
```bash
GATEWAY_SECRET="$(openssl rand -base64 48)" PUBLIC_URL=http://localhost:8088 PORT=8088 \
  DATA_DIR=./.localdata uv run missingmcp
```
Open `http://localhost:8088/` and confirm: badge "BUILT FOR ATHLETES", the new
hero subhead, the "Just ask" section with two bubbles + three chips rendering in
both light and dark mode, and the footer tagline. Stop the server (Ctrl-C).

## Self-Review notes (already reconciled)

- **`<title>` SEO:** kept `Garmin MCP Server` (Task 1) so `test_seo_head_meta`
  stays green while the promise leads.
- **Footer test:** `test_subpages_share_site_chrome` asserts the old tagline;
  updated in Task 4 to the new one (same task that changes it).
- **Ordering:** `test_just_ask_section` anchors on `id="just-ask"` vs
  `id="connectors"` (not the word "Connectors", which also appears in the nav).
- **`pages.py::_DEFAULT_DESC`** still reads "The connectors Claude is missing…";
  left as-is deliberately — no page renders without an explicit description, so
  it is never shown. Out of scope.
