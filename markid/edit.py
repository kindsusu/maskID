"""Text replacement (edit) operations.

Selected characters are removed by a true redaction that keeps background
images and vector line art (table borders), then the replacement string is
typeset into the same box with ``insert_htmlbox`` (PyMuPDF picks a suitable
font automatically, including CJK).
"""

import html
from dataclasses import dataclass
from typing import Optional, Tuple

import fitz

Rect = Tuple[float, float, float, float]


@dataclass
class EditOp:
    id: int
    page: int                      # 0-based page index
    rect: Rect                     # tight union of the selected char bboxes
    old_text: str
    new_text: str
    size: float                    # font size in pt (from the source span)
    color: Tuple[float, float, float]  # 0..1 RGB (from the source span)
    enabled: bool = True


def _span_color_rgb(color_int):
    r = ((color_int >> 16) & 255) / 255.0
    g = ((color_int >> 8) & 255) / 255.0
    b = (color_int & 255) / 255.0
    return (r, g, b)


def chars_in_rect(page, rect):
    """Collect text-layer chars whose bbox centre lies inside *rect*.

    Returns ``(text, tight_rect, size, color)`` — chars joined in line order
    (lines joined with a space), the tight union of their bboxes, and the
    font size / colour of the first contributing span — or ``None`` when the
    selection contains no text-layer characters (e.g. scanned images).
    """
    x0, y0, x1, y1 = rect
    d = page.get_text("rawdict")
    picked_lines = []
    first_span = None
    for block in d.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            picked = []
            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    bb = ch["bbox"]
                    cx = (bb[0] + bb[2]) / 2.0
                    cy = (bb[1] + bb[3]) / 2.0
                    if x0 <= cx <= x1 and y0 <= cy <= y1:
                        picked.append((ch["c"], bb))
                        if first_span is None:
                            first_span = span
            if picked:
                picked_lines.append((line["bbox"][1], line["bbox"][0], picked))
    if not picked_lines:
        return None
    picked_lines.sort(key=lambda t: (t[0], t[1]))
    text = " ".join(
        "".join(c for c, _ in chars) for _y, _x, chars in picked_lines
    )
    bbs = [bb for _y, _x, chars in picked_lines for _c, bb in chars]
    tight = (
        min(b[0] for b in bbs),
        min(b[1] for b in bbs),
        max(b[2] for b in bbs),
        max(b[3] for b in bbs),
    )
    size = float(first_span.get("size", 10.0)) if first_span else 10.0
    color = _span_color_rgb(first_span.get("color", 0)) if first_span else (0, 0, 0)
    return text, tight, size, color


def _fit_font_size(text, size, max_width):
    """Font size that lets a single nowrap line fit *max_width*.

    ``insert_htmlbox`` CLIPS horizontal overflow of a nowrap line instead of
    scaling it down (verified on PyMuPDF 1.25.2: returns scale=1.0 and cuts
    the text at the box edge), so the fitting must happen before insertion.
    Width is estimated with the built-in CJK font (Droid Sans Fallback),
    with a small safety margin.
    """
    if max_width <= 0 or not text:
        return size
    try:
        w = fitz.Font("cjk").text_length(text, fontsize=size)
    except Exception:
        w = 0.6 * size * len(text)               # crude fallback estimate
    if w <= max_width:
        return size
    return max(1.0, size * (max_width / w) * 0.98)


def apply_edit(page, op) -> bool:
    """Apply one EditOp to *page*.

    The original characters are physically removed (redaction that keeps
    images and line art), then the new text is inserted into the same box.
    Returns True when the inserted text had to shrink below 0.5x to fit
    (caller may want to warn the user).
    """
    rect = fitz.Rect(*op.rect)
    page.add_redact_annot(rect)
    page.apply_redactions(
        images=fitz.PDF_REDACT_IMAGE_NONE,       # keep background images
        graphics=fitz.PDF_REDACT_LINE_ART_NONE,  # keep table borders etc.
    )
    if not op.new_text.strip():
        return False                              # deletion only
    box = fitz.Rect(rect.x0, rect.y0 - 1.5, rect.x1, rect.y1 + 1.5)
    fitted = _fit_font_size(op.new_text, op.size, box.width)
    color_hex = "#%02x%02x%02x" % tuple(
        max(0, min(255, int(round(c * 255)))) for c in op.color
    )
    snippet = (
        '<span style="font-size:%gpt; color:%s; white-space:nowrap">%s</span>'
        % (fitted, color_hex, html.escape(op.new_text))
    )
    _spare, scale = page.insert_htmlbox(box, snippet, scale_low=0)
    effective = scale * (fitted / op.size if op.size else 1.0)
    return 0 < effective < 0.5


def apply_edits(doc, ops):
    """Apply enabled EditOps (page/id order). Returns ids that shrank < 0.5x."""
    warns = []
    for op in sorted((o for o in ops if o.enabled), key=lambda o: (o.page, o.id)):
        try:
            if apply_edit(doc[op.page], op):
                warns.append(op.id)
        except Exception:
            warns.append(op.id)
    return warns
