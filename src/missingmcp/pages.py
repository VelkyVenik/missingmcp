"""One site, one chrome: every HTML page (home, connector landings, OAuth
sign-in forms) is a content fragment wrapped in templates/_layout.html, so the
header, nav, footer and stylesheet exist exactly once."""
from __future__ import annotations
import html
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


def operator_html(config) -> str:
    """The {OPERATOR} placeholder value: the operator's name, linked to
    OPERATOR_URL when configured. Values are escaped here, so the result is
    trusted HTML — replace it into a page *after* any escaping fill pass."""
    name = html.escape(config.operator_name)
    if config.operator_url:
        return f'<a href="{html.escape(config.operator_url, quote=True)}">{name}</a>'
    return name
