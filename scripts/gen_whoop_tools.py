#!/usr/bin/env python3
"""Regenerate the "All tools" section of templates/whoop.html from the in-tree
tool table (missingmcp.adapters.whoop.mcp.TOOLS) so the page never drifts from
the code. Run after any TOOLS change:

  python scripts/gen_whoop_tools.py
"""
from __future__ import annotations
import html
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
TEMPLATE = ROOT / "src" / "missingmcp" / "templates" / "whoop.html"
BEGIN = "<!-- GENERATED:TOOLS:BEGIN"
END = "<!-- GENERATED:TOOLS:END -->"


def render() -> str:
    from missingmcp.adapters.whoop.mcp import TOOLS
    lines = [
        f"{BEGIN} — do not edit by hand; regenerate with scripts/gen_whoop_tools.py -->",
        f'      <p class="lede">All <strong>{len(TOOLS)} tools</strong> this connector exposes '
        "&mdash; read-only, straight from WHOOP&rsquo;s official v2 API.</p>",
        '      <div class="tools">',
        "        <details open>",
        f'          <summary>WHOOP data <span class="count">&middot; {len(TOOLS)} tools</span></summary>',
        "          <dl>",
    ]
    for name, description, _schema, _resolve in TOOLS:
        lines.append(f"            <dt><code>{html.escape(name)}</code></dt>"
                     f"<dd>{html.escape(description)}</dd>")
    lines += ["          </dl>", "        </details>", "      </div>"]
    return "\n".join(lines)


def main() -> None:
    text = TEMPLATE.read_text()
    pre, rest = text.split(BEGIN, 1)
    _, post = rest.split(END, 1)
    TEMPLATE.write_text(pre + render() + "\n" + END + post)
    print(f"wrote {TEMPLATE}")


if __name__ == "__main__":
    main()
