"""One site, one chrome: every HTML page (home, connector landings, OAuth
sign-in forms) is a content fragment wrapped in templates/_layout.html, so the
header, nav, footer and stylesheet exist exactly once."""
from __future__ import annotations
from pathlib import Path

_TPL_DIR = Path(__file__).parent / "templates"
_DEFAULT_DESC = ("The connectors Claude is missing — sign in once, add a URL, "
                 "start asking.")


def tpl(name: str) -> str:
    return (_TPL_DIR / name).read_text()


def render_page(fragment: str, title: str, desc: str | None = None) -> str:
    """Wrap a template fragment in the shared site layout. Placeholders inside
    the fragment ({PUBLIC_URL}, {ERROR}, {OAUTH_FIELDS}, ...) survive for the
    caller to fill afterwards."""
    return (tpl("_layout.html")
            .replace("{TITLE}", title)
            .replace("{DESC}", desc or _DEFAULT_DESC)
            .replace("{CONTENT}", tpl(fragment)))
