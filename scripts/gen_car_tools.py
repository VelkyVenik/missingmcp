#!/usr/bin/env python3
"""Regenerate the "All tools" section of templates/car.html from the in-tree
tool table (missingmcp.adapters.car.mcp.TOOLS) so the page never drifts from
the code. Run after any TOOLS change:

  python scripts/gen_car_tools.py
"""
from __future__ import annotations
import html
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
TEMPLATE = ROOT / "src" / "missingmcp" / "templates" / "car.html"
BEGIN = "<!-- GENERATED:TOOLS:BEGIN"
END = "<!-- GENERATED:TOOLS:END -->"


def _group(title: str, tools: list) -> list[str]:
    lines = ["        <details open>",
             f'          <summary>{title} <span class="count">&middot; {len(tools)} tools</span></summary>',
             "          <dl>"]
    for name, description, _schema, _op, _ro in tools:
        lines.append(f"            <dt><code>{html.escape(name)}</code></dt>"
                     f"<dd>{html.escape(description)}</dd>")
    return lines + ["          </dl>", "        </details>"]


def render() -> str:
    from missingmcp.adapters.car.mcp import TOOLS
    reads = [t for t in TOOLS if t[4]]
    actions = [t for t in TOOLS if not t[4]]
    lines = [
        f"{BEGIN} — do not edit by hand; regenerate with scripts/gen_car_tools.py -->",
        f'      <p class="lede">All <strong>{len(TOOLS)} tools</strong> this connector exposes. '
        "Actions show up only when your car supports them.</p>",
        '      <div class="tools">',
        *_group("Read", reads),
        *_group("Actions", actions),
        "      </div>",
    ]
    return "\n".join(lines)


def main() -> None:
    text = TEMPLATE.read_text()
    pre, rest = text.split(BEGIN, 1)
    _, post = rest.split(END, 1)
    TEMPLATE.write_text(pre + render() + "\n" + END + post)
    print(f"wrote {TEMPLATE}")


if __name__ == "__main__":
    main()
