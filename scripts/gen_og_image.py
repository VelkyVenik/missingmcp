#!/usr/bin/env python3
"""Draw src/missingmcp/static/og.png (1200x630) — the link-preview card.

Why a script and not a hand-made bitmap: the card carries copy and the site's
palette, both of which change. Same pattern as gen_garmin_tools.py — a generated
artifact checked in, with its generator next to it.

The card leads with the question a user actually types, because that is what the
product does; a claim about "health data insights" would say less in more words.
Serif for the human sentence, mono for the endpoint — the gateway's whole thesis
in two typefaces. Palette is the site's own dark theme, so a shared link and the
page it opens read as one product.

Drawn at 2x and downscaled (supersampling), which is what keeps the type crisp at
the 1.91:1 size every platform expects. 1200x630 exactly — a 256x256 icon is what
made previews letterbox before.

Pillow is not a project dependency; pull it in for the run:
  uv run --with pillow python scripts/gen_og_image.py
  uv run --with pillow python scripts/gen_og_image.py --open

Re-run after changing the copy or the palette, and commit the PNG.
"""
from __future__ import annotations
import argparse
import os
import subprocess
import sys

W, H, SS = 1200, 630, 2          # SS = supersampling factor
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "src", "missingmcp", "static", "og.png")

# --- palette: src/missingmcp/templates/_layout.html, dark theme ---------------
BG      = "#0e1013"
CARD    = "#16191f"
BORDER  = "#262b35"
TEXT    = "#edeff3"
MUTED   = "#a2a9b8"
ACCENT  = "#818cf8"
LIVE    = "#34d399"

# --- copy --------------------------------------------------------------------
EYEBROW  = "MissingMCP"
ASK      = ["How did I sleep", "this week?"]        # pre-wrapped: the break is a
                                                    # design decision, not luck
ANSWER   = [("Claude & ChatGPT answer from ", False), ("your own", True),
            (" Garmin data.", False)]               # (text, emphasized)
ENDPOINTS = [("missingmcp.com/", "garmin", "/mcp"),
             ("missingmcp.com/", "whoop", "/mcp")]

# --- fonts: system faces, variable axes set explicitly so weights are exact ---
NY   = "/System/Library/Fonts/NewYork.ttf"          # axes: Optical Size, Weight, GRAD
SANS = "/System/Library/Fonts/SFNS.ttf"             # axes: Width, Optical Size, GRAD, Weight
MONO = "/System/Library/Fonts/SFNSMono.ttf"         # axes: YAXS, Weight
FALLBACK_SERIF = "/System/Library/Fonts/Supplemental/Georgia Bold.ttf"
FALLBACK_SANS = "/System/Library/Fonts/Helvetica.ttc"
FALLBACK_MONO = "/System/Library/Fonts/Supplemental/Menlo.ttc"


def font(path: str, size: int, axes=None, fallback: str | None = None):
    """Load a font at 2x and pin its variable axes. Falls back to a static face
    when the variable one is unavailable (non-macOS), so the script still runs."""
    from PIL import ImageFont
    try:
        f = ImageFont.truetype(path, size * SS)
    except OSError:
        if not fallback:
            raise
        print(f"note: {path} unavailable, using {fallback}", file=sys.stderr)
        return ImageFont.truetype(fallback, size * SS)
    if axes:
        try:
            f.set_variation_by_axes(axes)
        except Exception as e:            # noqa: BLE001 - static build, keep going
            print(f"note: no variations on {path} ({type(e).__name__})",
                  file=sys.stderr)
    return f


def bloom(img):
    """One soft indigo bloom off the top-right corner — the site's accent-soft,
    scaled up. Enough that the black doesn't read as a void, quiet enough to stay
    behind the type.

    Built small and enlarged: a 128px radial gradient resized up is instant, where
    a per-pixel loop at 2x would crawl. The blur is not optional — stacked
    ellipses band visibly once enlarged, and a visible ring reads as a mistake."""
    from PIL import Image, ImageDraw, ImageFilter
    n, d = 128, 900 * SS
    mask = Image.new("L", (n, n), 0)
    md = ImageDraw.Draw(mask)
    for i in range(n // 2, 0, -1):                  # outside in, so alpha stacks
        t = 1 - i / (n / 2)                         # 0 at rim -> 1 at centre
        md.ellipse([n / 2 - i, n / 2 - i, n / 2 + i, n / 2 + i],
                   fill=int(58 * t ** 2.2))
    mask = mask.filter(ImageFilter.GaussianBlur(n / 22))
    mask = mask.resize((d, d), Image.BICUBIC)
    layer = Image.new("RGB", (d, d), ACCENT)
    # Centre it just off the top-right corner, so only the falloff is on canvas.
    img.paste(layer, (W * SS - 150 * SS - d // 2, -170 * SS - d // 2), mask)


def tracked(draw, x, y, text, f, fill, em_track):
    """Draw text with letter tracking (PIL has no letter-spacing). Returns width."""
    for ch in text:
        draw.text((x, y), ch, font=f, fill=fill)
        x += draw.textlength(ch, font=f) + em_track
    return x


def runs(draw, x, y, parts, plain, strong):
    """Draw a line assembled from (text, emphasized) runs, so one phrase can
    carry emphasis without becoming an image of a sentence."""
    for text, em in parts:
        f, fill = (strong, TEXT) if em else (plain, MUTED)
        draw.text((x, y), text, font=f, fill=fill)
        x += draw.textlength(text, font=f)
    return x


def chip(draw, x, y, head, mid, tail, f):
    """An endpoint pill: the connector segment in accent, the rest muted. Mono is
    literal here — this is a URL you paste, not a label."""
    pad_x, pad_y, r = 17 * SS, 11 * SS, 9 * SS
    w = sum(draw.textlength(s, font=f) for s in (head, mid, tail))
    h = f.size + 2 * pad_y
    draw.rounded_rectangle([x, y, x + w + 2 * pad_x, y + h], radius=r,
                           fill=CARD, outline=BORDER, width=SS)
    tx, ty = x + pad_x, y + pad_y - int(0.12 * f.size)
    for s, fill in ((head, MUTED), (mid, ACCENT), (tail, MUTED)):
        draw.text((tx, ty), s, font=f, fill=fill)
        tx += draw.textlength(s, font=f)
    return x + w + 2 * pad_x


def main():
    p = argparse.ArgumentParser(description="Draw the OG card to static/og.png")
    p.add_argument("--open", action="store_true", help="open the PNG when done")
    args = p.parse_args()
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        sys.exit("Pillow missing — run: uv run --with pillow python "
                 "scripts/gen_og_image.py")

    img = Image.new("RGB", (W * SS, H * SS), BG)
    bloom(img)
    d = ImageDraw.Draw(img)

    f_eyebrow = font(SANS, 21, [100, 28, 400, 640], FALLBACK_SANS)
    f_ask = font(NY, 82, [96, 600, 0], FALLBACK_SERIF)      # big optical size
    f_ans = font(SANS, 27, [100, 28, 400, 400], FALLBACK_SANS)
    f_ans_b = font(SANS, 27, [100, 28, 400, 640], FALLBACK_SANS)
    f_mono = font(MONO, 22, [294, 400], FALLBACK_MONO)

    pad_l, pad_t, pad_b = 80 * SS, 72 * SS, 72 * SS

    # eyebrow — a live dot, because the connectors are hosted and running
    dot_r = 5.5 * SS
    cy = pad_t + f_eyebrow.size * 0.42
    d.ellipse([pad_l - dot_r, cy - dot_r, pad_l + dot_r, cy + dot_r], fill=LIVE)
    tracked(d, pad_l + 23 * SS, pad_t, EYEBROW.upper(), f_eyebrow, MUTED,
            0.14 * f_eyebrow.size)

    # the question — the signature element, optically centred in the middle band
    line_h = int(88.5 * SS)
    block_h = line_h * len(ASK) + 30 * SS + int(f_ans.size * 1.45)
    # Optical, not arithmetic: a serif cap-height sits low in its line box, so the
    # geometric centre reads as sagging. Lift it by a fraction of the line.
    y = pad_t + f_eyebrow.size + ((H * SS - pad_t - pad_b - f_eyebrow.size
                                   - block_h) // 2) - int(0.16 * line_h)
    last_x = pad_l
    for i, line in enumerate(ASK):
        d.text((pad_l, y + i * line_h), line, font=f_ask, fill=TEXT)
        if i == len(ASK) - 1:
            last_x = pad_l + d.textlength(line, font=f_ask)
    # caret: marks the sentence as something being typed, not a slogan
    c_h, c_w = int(0.82 * f_ask.size), 5 * SS
    c_y = y + (len(ASK) - 1) * line_h + int(0.18 * f_ask.size)
    d.rounded_rectangle([last_x + 14 * SS, c_y, last_x + 14 * SS + c_w,
                         c_y + c_h], radius=2 * SS, fill=ACCENT)

    runs(d, pad_l, y + len(ASK) * line_h + 30 * SS, ANSWER, f_ans, f_ans_b)

    # endpoints
    chip_h = f_mono.size + 22 * SS
    cx, cy2 = pad_l, H * SS - pad_b - chip_h
    for head, mid, tail in ENDPOINTS:
        cx = chip(d, cx, cy2, head, mid, tail, f_mono) + 14 * SS

    img.resize((W, H), Image.LANCZOS).save(OUT, "PNG", optimize=True)
    print(f"Wrote {os.path.relpath(OUT, ROOT)} — {W}x{H}, "
          f"{os.path.getsize(OUT) / 1024:.0f} kB")
    if args.open:
        subprocess.run(["open", OUT], check=False)


if __name__ == "__main__":
    main()
