"""Masking: true redaction (data removal) followed by black box or mosaic.

Masking is never cover-only.  Every rect is redacted with
``apply_redactions`` so the underlying text glyphs and image pixels are
physically removed from the PDF; the visual treatment (black fill or a
pixelated raster) is applied on top of the already-cleared area.
"""

import io

import fitz
from PIL import Image

from .findings import to_unrotated


def pad_rect(rect, pad=1.0):
    """Grow *rect* by ``pad`` pt horizontally; vertical padding capped at 20%."""
    x0, y0, x1, y1 = rect
    h = y1 - y0
    vpad = min(pad, 0.2 * h) if h > 0 else pad
    return (x0 - pad, y0 - vpad, x1 + pad, y1 + vpad)


def _pixelate_png(page, rect, dpi=200):
    """Render *rect* and return a heavily pixelated PNG of the same area."""
    pix = page.get_pixmap(clip=fitz.Rect(*rect), dpi=dpi, alpha=False)
    if pix.width == 0 or pix.height == 0:
        return None
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    block = max(6, min(40, pix.height // 3)) if pix.height else 6
    small = img.resize(
        (max(1, pix.width // block), max(1, pix.height // block)), Image.NEAREST
    )
    out = small.resize((pix.width, pix.height), Image.NEAREST)
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def snap_rect_away_from_lines(rect, line_bboxes, graze_frac=0.35, gap=0.2):
    """Shrink *rect* vertically so it stops grazing neighbouring text lines.

    OCR detector boxes are loose: an edge can overhang an adjacent text line
    by a fraction of a point, and ``apply_redactions`` deletes every glyph the
    rect merely touches — wiping a line that was never targeted.

    For each text line that overlaps *rect* horizontally and whose vertical
    overlap is positive but below ``graze_frac`` of the line height (a graze,
    not a target), the rect edge is pulled clear of that line.  Lines with
    >= ``graze_frac`` vertical overlap are the rect's actual target and are
    never shrunk away from.  If snapping would invert the rect, the original
    rect is returned unchanged (over-masking is safer than under-masking).
    """
    x0, y0, x1, y1 = rect
    cy = (y0 + y1) / 2.0
    for lx0, ly0, lx1, ly1 in line_bboxes:
        if min(x1, lx1) - max(x0, lx0) <= 0:
            continue                      # no horizontal overlap
        lh = ly1 - ly0
        if lh <= 0:
            continue
        ov = min(y1, ly1) - max(y0, ly0)
        if ov <= 0 or ov >= graze_frac * lh:
            continue                      # untouched, or genuinely targeted
        if (ly0 + ly1) / 2.0 > cy:        # grazed line sits below the rect
            y1 = min(y1, ly0 - gap)
        else:                             # grazed line sits above
            y0 = max(y0, ly1 + gap)
    if y1 <= y0:
        return rect
    return (x0, y0, x1, y1)


def apply_masks(page, rect_items, style, line_bboxes=None):
    """Redact and visually mask rects on *page*.

    *rect_items* is a list of ``(rect, source)`` pairs in **display**
    coordinates (as is every Finding rect).  OCR-sourced rects are snapped
    away from neighbouring text lines after padding (their detector boxes
    are loose); text/annot rects are char-bbox precise and manual rects
    reflect user intent, so those are used as-is.

    Coordinate boundaries (verified on PyMuPDF 1.25.2): ``get_pixmap(clip=)``
    takes display coords — used as-is for the mosaic raster — while
    ``add_redact_annot`` and ``insert_image`` take unrotated coords, so rects
    are converted right at those calls.  Rotation 0 keeps everything as-is.

    Returns the number of rectangles masked.
    """
    clean = []
    for r, source in rect_items:
        if not r:
            continue
        r = pad_rect(r)
        if source == "ocr" and line_bboxes:
            r = snap_rect_away_from_lines(r, line_bboxes)
        if r[2] > r[0] and r[3] > r[1]:
            clean.append(r)
    if not clean:
        return 0

    # Capture pixelated rasters *before* the pixels are removed
    # (clip= addresses display space: pass the display rect unchanged).
    mosaics = []
    if style == "mosaic":
        for r in clean:
            try:
                mosaics.append((r, _pixelate_png(page, r)))
            except Exception:
                mosaics.append((r, None))

    fill = (0, 0, 0) if style == "box" else (1, 1, 1)
    for r in clean:
        page.add_redact_annot(fitz.Rect(*to_unrotated(page, r)), fill=fill)

    # Physically remove text + image pixels; keep vector line art (table borders).
    page.apply_redactions(
        images=fitz.PDF_REDACT_IMAGE_PIXELS,
        graphics=fitz.PDF_REDACT_LINE_ART_NONE,
    )

    if style == "mosaic":
        for r, png in mosaics:
            if png:
                page.insert_image(fitz.Rect(*to_unrotated(page, r)),
                                  stream=png, keep_proportion=False)

    return len(clean)
