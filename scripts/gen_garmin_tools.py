#!/usr/bin/env python3
"""Regenerate the "All tools" section of templates/garmin.html from garmin_mcp.

Reads the worker's source at the exact commit production runs (the
GARMIN_MCP_REF pin in the Dockerfile), extracts every ``@app.tool()`` function
with the first paragraph of its docstring, and rewrites the HTML between the
GENERATED:TOOLS markers — grouped per module, in the worker's registration
order. Run it after every GARMIN_MCP_REF bump so the page matches the deploy:

  python scripts/gen_garmin_tools.py                 # clone the pinned ref (needs network)
  python scripts/gen_garmin_tools.py --src ~/dev/garmin_mcp   # use an existing checkout
"""
from __future__ import annotations
import argparse
import ast
import html
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "src" / "missingmcp" / "templates" / "garmin.html"
REPO_URL = "https://github.com/Taxuspt/garmin_mcp"
BEGIN = "<!-- GENERATED:TOOLS:BEGIN"
END = "<!-- GENERATED:TOOLS:END -->"

# Reader-facing names for the worker's module files; anything unmapped falls
# back to the module name with underscores spaced out.
CATEGORY_NAMES = {
    "activity_management": "Activities",
    "activity_analysis": "Activity analysis",
    "health_wellness": "Health & wellness",
    "user_profile": "Profile & settings",
    "devices": "Devices",
    "gear_management": "Gear",
    "weight_management": "Weight & body composition",
    "challenges": "Challenges & badges",
    "training": "Training & performance",
    "workouts": "Workouts",
    "workout_builders": "Workout builders",
    "data_management": "Data management",
    "womens_health": "Women's health",
    "nutrition": "Nutrition",
    "courses": "Courses",
}


def pinned_ref() -> str:
    m = re.search(r"^ARG GARMIN_MCP_REF=(\S+)", (ROOT / "Dockerfile").read_text(), re.M)
    if not m:
        sys.exit("GARMIN_MCP_REF pin not found in Dockerfile")
    return m.group(1)


def clone_at(ref: str, dest: str) -> Path:
    subprocess.run(["git", "clone", "--quiet", REPO_URL, dest], check=True)
    subprocess.run(["git", "-C", dest, "checkout", "--quiet", ref], check=True)
    return Path(dest)


def module_order(pkg: Path) -> list[str]:
    """Module names in the order the worker registers them (its __init__.py)."""
    return re.findall(r"(\w+)\.register_tools\(app\)", (pkg / "__init__.py").read_text())


def first_paragraph(doc: str | None, limit: int = 220) -> str:
    if not doc:
        return ""
    para = " ".join(line.strip() for line in doc.strip().split("\n\n")[0].splitlines())
    if len(para) <= limit:
        return para
    cut = para[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(".,;") + "…"


def _is_tool_decorator(node: ast.expr) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "tool")


def extract_tools(module_path: Path) -> list[tuple[str, str]]:
    """(name, one-line description) for every @app.tool() inside register_tools,
    in source order — matching what the worker actually exposes."""
    tree = ast.parse(module_path.read_text())
    register = next(
        (n for n in tree.body
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "register_tools"),
        None)
    if register is None:
        return []
    tools = []
    for node in ast.walk(register):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                _is_tool_decorator(d) for d in node.decorator_list):
            tools.append((node.name, first_paragraph(ast.get_docstring(node))))
    return tools


def render(groups: list[tuple[str, list[tuple[str, str]]]], ref: str) -> str:
    total = sum(len(tools) for _, tools in groups)
    out = [
        f'      <p class="lede">All <strong>{total} tools</strong> this connector currently exposes, '
        f'grouped by area &mdash; straight from the <a href="{REPO_URL}">garmin_mcp</a> source '
        f'(<a href="{REPO_URL}/tree/{ref}"><code>{ref[:7]}</code></a>). Click a group to expand.</p>',
        '      <div class="tools">',
    ]
    for module, tools in groups:
        title = CATEGORY_NAMES.get(module, module.replace("_", " ").capitalize())
        out.append("        <details>")
        out.append(f'          <summary>{html.escape(title)} '
                   f'<span class="count">&middot; {len(tools)} tools</span></summary>')
        out.append("          <dl>")
        for name, desc in tools:
            out.append(f"            <dt><code>{html.escape(name)}</code></dt>"
                       f"<dd>{html.escape(desc)}</dd>")
        out.append("          </dl>")
        out.append("        </details>")
    out.append("      </div>")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", help="existing garmin_mcp checkout (skips the clone)")
    ap.add_argument("--ref", help="override the Dockerfile GARMIN_MCP_REF pin")
    args = ap.parse_args()

    ref = args.ref or pinned_ref()
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(args.src).expanduser() if args.src else clone_at(ref, tmp)
        pkg = src / "src" / "garmin_mcp"
        groups = [(m, extract_tools(pkg / f"{m}.py")) for m in module_order(pkg)]
        groups = [(m, tools) for m, tools in groups if tools]

    page = TEMPLATE.read_text()
    begin = page.index(BEGIN)
    begin = page.index("\n", begin) + 1          # keep the marker line itself
    end = page.index(END)
    TEMPLATE.write_text(page[:begin] + render(groups, ref) + "\n" + page[end:])
    total = sum(len(t) for _, t in groups)
    print(f"wrote {total} tools in {len(groups)} groups to {TEMPLATE.relative_to(ROOT)} (ref {ref[:7]})")


if __name__ == "__main__":
    main()
