"""One site, one chrome: every HTML page (home, connector landings, OAuth
sign-in forms) is a content fragment wrapped in templates/_layout.html, so the
header, nav, footer and stylesheet exist exactly once."""
from __future__ import annotations
import hashlib
import html
from pathlib import Path

_TPL_DIR = Path(__file__).parent / "templates"
# Content hash of static/site.js, computed once at import. Appended as a
# `?v=` cache-buster to the layout's <script> src so a new deploy of the JS
# forces returning browsers (and the Cloudflare edge) to fetch the fresh file
# instead of serving a stale cached copy under the same URL.
_SITE_JS_VER = hashlib.sha256(
    (Path(__file__).parent / "static" / "site.js").read_bytes()
).hexdigest()[:8]
_DEFAULT_DESC = ("The connectors Claude is missing — sign in once, add a URL, "
                 "start asking.")


def tpl(name: str) -> str:
    return (_TPL_DIR / name).read_text()


def _head_meta(title: str, desc: str, public_url: str, path: str,
               noindex: bool, extra_head: str) -> str:
    """SEO head block: sign-in/MFA pages get noindex, indexable pages get a
    canonical URL (the 404 catch-all serves the home fragment, so every stray
    URL canonicalizes to /) plus Open Graph tags for link previews."""
    lines = []
    if noindex:
        lines.append('<meta name="robots" content="noindex">')
    elif public_url:
        url = public_url + path
        t, d = html.escape(title, quote=True), html.escape(desc, quote=True)
        lines += [
            f'<link rel="canonical" href="{url}">',
            '<meta property="og:type" content="website">',
            '<meta property="og:site_name" content="MissingMCP">',
            f'<meta property="og:title" content="{t}">',
            f'<meta property="og:description" content="{d}">',
            f'<meta property="og:url" content="{url}">',
            f'<meta property="og:image" content="{public_url}/static/icon.png">',
            '<meta name="twitter:card" content="summary">',
        ]
    if extra_head:
        lines.append(extra_head)
    return "\n".join(f"  {line}" for line in lines)


def render_page(fragment: str, title: str, desc: str | None = None, *,
                public_url: str = "", path: str = "", noindex: bool = False,
                extra_head: str = "") -> str:
    """Wrap a template fragment in the shared site layout. Placeholders inside
    the fragment ({PUBLIC_URL}, {ERROR}, {OAUTH_FIELDS}, ...) survive for the
    caller to fill afterwards."""
    desc = desc or _DEFAULT_DESC
    return (tpl("_layout.html")
            .replace("{TITLE}", title)
            .replace("{DESC}", desc)
            .replace("{SITE_JS_VER}", _SITE_JS_VER)
            .replace("{HEAD_META}", _head_meta(title, desc, public_url, path,
                                               noindex, extra_head))
            .replace("{CONTENT}", tpl(fragment)))


def operator_html(config) -> str:
    """The {OPERATOR} placeholder value: the operator's name, linked to
    OPERATOR_URL when configured. Values are escaped here, so the result is
    trusted HTML — replace it into a page *after* any escaping fill pass."""
    name = html.escape(config.operator_name)
    if config.operator_url:
        return f'<a href="{html.escape(config.operator_url, quote=True)}">{name}</a>'
    return name
