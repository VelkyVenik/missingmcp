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
# Same cache-buster trick for the link-preview card: scrapers (Facebook, LinkedIn,
# Slack) cache og:image per URL and hold a stale copy for days, so a regenerated
# card needs a new URL to be picked up.
_OG_IMAGE = "og.png"
_OG_IMAGE_VER = hashlib.sha256(
    (Path(__file__).parent / "static" / _OG_IMAGE).read_bytes()
).hexdigest()[:8]
_OG_IMAGE_W, _OG_IMAGE_H = 1200, 630     # keep in sync with scripts/gen_og_image.py
_OG_IMAGE_ALT = ("MissingMCP — Claude & ChatGPT answer from your own Garmin "
                 "data.")
_DEFAULT_DESC = ("The connectors your AI is missing — sign in once, add a URL, "
                 "start asking.")
# Social previews cut around 125 characters, while a meta description can usefully
# run to ~155 for search. They are therefore not the same string.
_OG_DESC_MAX = 120


def tpl(name: str) -> str:
    return (_TPL_DIR / name).read_text()


def og_desc(desc: str, override: str | None = None) -> str:
    """The preview line: an explicit short one when given, else the first sentence
    of the search description. Truncating mid-sentence is what makes a shared link
    look broken, so fall back to a boundary the copy already has (— or . or ?),
    and only ellipsize on a word if the first sentence is itself too long."""
    if override:
        return override
    if len(desc) <= _OG_DESC_MAX:
        return desc
    for sep in (" — ", "? ", ". ", "! "):
        head = desc.split(sep, 1)[0]
        if head != desc and len(head) <= _OG_DESC_MAX:
            return head + ("?" if sep.startswith("?") else "")
    cut = desc[:_OG_DESC_MAX].rsplit(" ", 1)[0]
    return cut + "…"


def _head_meta(title: str, desc: str, public_url: str, path: str,
               noindex: bool, extra_head: str, og_description: str) -> str:
    """SEO head block: sign-in/MFA pages get noindex, indexable pages get a
    canonical URL (the 404 catch-all serves the home fragment, so every stray
    URL canonicalizes to /) plus Open Graph tags for link previews."""
    lines = []
    if noindex:
        lines.append('<meta name="robots" content="noindex">')
    elif public_url:
        url = public_url + path
        t = html.escape(title, quote=True)
        d = html.escape(og_description, quote=True)
        img = f"{public_url}/static/{_OG_IMAGE}?v={_OG_IMAGE_VER}"
        lines += [
            f'<link rel="canonical" href="{url}">',
            '<meta property="og:type" content="website">',
            '<meta property="og:site_name" content="MissingMCP">',
            f'<meta property="og:title" content="{t}">',
            f'<meta property="og:description" content="{d}">',
            f'<meta property="og:url" content="{url}">',
            f'<meta property="og:image" content="{img}">',
            # Dimensions inline so a scraper can lay the card out on first parse
            # instead of fetching the image to find out it is 1.91:1.
            f'<meta property="og:image:width" content="{_OG_IMAGE_W}">',
            f'<meta property="og:image:height" content="{_OG_IMAGE_H}">',
            f'<meta property="og:image:alt" content="{html.escape(_OG_IMAGE_ALT, quote=True)}">',
            # summary_large_image, not summary: with a 1.91:1 card, `summary`
            # renders it as a cropped thumbnail.
            '<meta name="twitter:card" content="summary_large_image">',
        ]
    if extra_head:
        lines.append(extra_head)
    return "\n".join(f"  {line}" for line in lines)


def render_page(fragment: str, title: str, desc: str | None = None, *,
                public_url: str = "", path: str = "", noindex: bool = False,
                extra_head: str = "", social_desc: str | None = None) -> str:
    """Wrap a template fragment in the shared site layout. Placeholders inside
    the fragment ({PUBLIC_URL}, {ERROR}, {OAUTH_FIELDS}, ...) survive for the
    caller to fill afterwards. `social_desc` overrides the link-preview line when
    the search description is too long to survive a preview intact."""
    desc = desc or _DEFAULT_DESC
    return (tpl("_layout.html")
            .replace("{TITLE}", title)
            .replace("{DESC}", desc)
            .replace("{SITE_JS_VER}", _SITE_JS_VER)
            .replace("{HEAD_META}", _head_meta(title, desc, public_url, path,
                                               noindex, extra_head,
                                               og_desc(desc, social_desc)))
            .replace("{CONTENT}", tpl(fragment)))


def operator_html(config) -> str:
    """The {OPERATOR} placeholder value: the operator's name, linked to
    OPERATOR_URL when configured. Values are escaped here, so the result is
    trusted HTML — replace it into a page *after* any escaping fill pass."""
    name = html.escape(config.operator_name)
    if config.operator_url:
        return f'<a href="{html.escape(config.operator_url, quote=True)}">{name}</a>'
    return name
