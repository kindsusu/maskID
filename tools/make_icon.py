#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Generate the MarkID program icon (assets/MarkID.ico).

Reproducible build script. Requires Pillow (bundled in the project venv).

Design
------
Deep-navy (#1B3A5C) rounded-square badge, a white document silhouette, and two
solid black masking bars across the document -- the redaction symbol. No text,
kept deliberately simple so it stays sharp down to 16x16.

Usage
-----
    .venv\\Scripts\\python.exe tools\\make_icon.py

Writes a single multi-resolution .ico (256/128/64/48/32/16) to
``assets/MarkID.ico`` (relative to the repository root), then re-opens it to
confirm every frame is present.
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw

# --- palette -------------------------------------------------------------
NAVY = (27, 58, 92, 255)          # #1B3A5C rounded-square background
PAPER = (255, 255, 255, 255)      # white document
PAPER_SHADOW = (10, 26, 45, 90)   # faint navy drop-shadow under the document
REDACT = (13, 13, 13, 255)        # near-black redaction bars
GUIDE = (196, 208, 220, 255)      # light-grey "normal text" guide lines

# Icon frame sizes packed into the single .ico.
SIZES = (256, 128, 64, 48, 32, 16)
# Supersampling factor: render large, downscale with LANCZOS for clean edges.
SS = 8

ICON_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "assets", "MarkID.ico")


def _rr(draw: ImageDraw.ImageDraw, box, radius, fill):
    """rounded_rectangle helper (kept for older Pillow signatures)."""
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def render(size: int) -> Image.Image:
    """Render one square RGBA frame of the icon at the given pixel size."""
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def px(frac: float) -> float:
        return frac * S

    # 1) Rounded-square navy background badge.
    margin = px(0.055)
    _rr(d, [margin, margin, S - margin, S - margin], radius=px(0.185), fill=NAVY)

    # 2) White document silhouette (portrait), with a faint drop shadow.
    doc_l, doc_r = px(0.30), px(0.70)
    doc_t, doc_b = px(0.195), px(0.815)
    doc_radius = px(0.045)
    off = px(0.018)
    _rr(d, [doc_l + off, doc_t + off, doc_r + off, doc_b + off],
        radius=doc_radius, fill=PAPER_SHADOW)
    _rr(d, [doc_l, doc_t, doc_r, doc_b], radius=doc_radius, fill=PAPER)

    # 3) Content lines inside the document.
    line_l = doc_l + px(0.055)
    line_r = doc_r - px(0.055)
    bar_h = px(0.058)
    guide_h = px(0.026)

    def hline(cy: float, x0: float, x1: float, h: float, fill):
        d.rounded_rectangle([x0, cy - h / 2, x1, cy + h / 2],
                            radius=h / 2, fill=fill)

    # Row 1: short grey guide line (normal text).
    hline(px(0.335), line_l, line_l + (line_r - line_l) * 0.62, guide_h, GUIDE)
    # Row 2: black redaction bar (full width).
    hline(px(0.445), line_l, line_r, bar_h, REDACT)
    # Row 3: medium grey guide line.
    hline(px(0.560), line_l, line_l + (line_r - line_l) * 0.80, guide_h, GUIDE)
    # Row 4: black redaction bar (slightly shorter).
    hline(px(0.670), line_l, line_l + (line_r - line_l) * 0.88, bar_h, REDACT)

    # Downsample to the target size for anti-aliased edges.
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    os.makedirs(os.path.dirname(ICON_PATH), exist_ok=True)
    frames = [render(s) for s in SIZES]
    # Save one multi-resolution .ico; the largest frame carries sizes[].
    frames[0].save(ICON_PATH, format="ICO",
                   sizes=[(s, s) for s in SIZES])
    print(f"[make_icon] wrote {ICON_PATH}")

    # Re-open and confirm every requested frame is present.
    with Image.open(ICON_PATH) as ico:
        present = sorted({sz for sz in ico.info.get("sizes", set())}) \
            if ico.info.get("sizes") else None
    # Robust frame check across Pillow versions.
    got = set()
    with Image.open(ICON_PATH) as ico:
        for sz in SIZES:
            ico.size = (sz, sz)
            fr = ico.copy()
            got.add(fr.size)
    missing = [s for s in SIZES if (s, s) not in got]
    print(f"[make_icon] frames verified present: {sorted(got, reverse=True)}")
    if missing:
        raise SystemExit(f"[make_icon] MISSING frames: {missing}")
    print("[make_icon] OK")


if __name__ == "__main__":
    main()
