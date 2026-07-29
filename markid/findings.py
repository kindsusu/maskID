"""Finding model, rectangle helpers, and de-duplication.

Coordinate convention (v1.4.1, empirically verified on PyMuPDF 1.25.2):
``Finding.rect`` / ``EditOp.rect`` are always in **display** space — the
rotation-applied coordinate system that ``get_pixmap`` renders and
``get_pixmap(clip=)`` addresses.  PyMuPDF APIs that speak the *unrotated*
space (``get_text`` bboxes, ``add_redact_annot``, ``annot.rect``,
``insert_image``, ``insert_htmlbox``) are converted at the call boundary
with :func:`to_display` / :func:`to_unrotated`.  For rotation 0 both are
the identity.
"""

from dataclasses import dataclass, field
from typing import Optional, Tuple

import fitz

Rect = Tuple[float, float, float, float]  # (x0, y0, x1, y1) in PDF points


def to_display(page, rect):
    """Unrotated page coords -> display (rotation-applied) coords."""
    if page.rotation % 360 == 0:
        return tuple(rect)
    return tuple(fitz.Rect(rect) * page.rotation_matrix)


def to_unrotated(page, rect):
    """Display (rotation-applied) coords -> unrotated page coords."""
    if page.rotation % 360 == 0:
        return tuple(rect)
    return tuple(fitz.Rect(rect) * page.derotation_matrix)


# Kind -> Korean label used in the GUI list.
KIND_LABEL_KO = {
    "rrn": "주민번호",
    "rrn_partial": "주민번호(앞자리)",
    "address": "주소",
    "manual": "수동",
}
SOURCE_LABEL_KO = {
    "text": "텍스트",
    "ocr": "OCR",
    "annot": "주석",
    "manual": "수동",
}


@dataclass
class Finding:
    id: int
    page: int                      # 0-based page index
    kind: str                      # rrn | rrn_partial | address | manual
    source: str                    # text | ocr | annot | manual
    text: str                      # display text (back digits already masked)
    rect: Rect                     # full masking rectangle (PDF points)
    rect_back6: Optional[Rect] = None   # sub-rect for scope=back6 (rrn only)
    conf: str = "high"             # high | low
    enabled: bool = True
    user_adjusted: bool = False    # box moved/resized by the user (v1.5.0)

    def rect_for_scope(self, scope: str) -> Optional[Rect]:
        """Return the rectangle to mask for *scope*, or None to skip.

        - user_adjusted: the user moved/resized this box — mask exactly the
          adjusted rect, whatever the scope or kind (user intent wins).
        - rrn         : back6 -> rect_back6 (fallback: whole rect), full -> rect.
                        OCR findings carry a width-weighted back6 estimate with
                        a safety bias (left edge widened toward the gender
                        digit) since v1.4.2; when the estimate is unavailable
                        the whole number is masked as before.
        - rrn_partial : back6 -> skipped (front digits only), full -> rect
        - address/manual: always the full rect
        """
        if self.user_adjusted:
            return self.rect
        if self.kind == "rrn":
            if scope == "back6":
                return self.rect_back6 if self.rect_back6 else self.rect
            return self.rect
        if self.kind == "rrn_partial":
            if scope == "back6":
                return None
            return self.rect
        return self.rect

    def to_report(self) -> dict:
        return {
            "page": self.page,             # 0-based page index
            "page_no": self.page + 1,      # 1-based, for humans reading the report
            "kind": self.kind,
            "source": self.source,
            "text": self.text,
            "conf": self.conf,
            "enabled": self.enabled,
            "user_adjusted": self.user_adjusted,
            "rect": [round(v, 2) for v in self.rect],
            "rect_back6": (
                [round(v, 2) for v in self.rect_back6] if self.rect_back6 else None
            ),
        }


# --------------------------------------------------------------------------
# Rectangle geometry
# --------------------------------------------------------------------------

def union_rect(rects):
    xs0 = [r[0] for r in rects]
    ys0 = [r[1] for r in rects]
    xs1 = [r[2] for r in rects]
    ys1 = [r[3] for r in rects]
    return (min(xs0), min(ys0), max(xs1), max(ys1))


def rect_area(r):
    return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])


def intersect_area(a, b):
    x0 = max(a[0], b[0])
    y0 = max(a[1], b[1])
    x1 = min(a[2], b[2])
    y1 = min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return (x1 - x0) * (y1 - y0)


def iou(a, b):
    inter = intersect_area(a, b)
    if inter <= 0:
        return 0.0
    return inter / (rect_area(a) + rect_area(b) - inter)


def contains(outer, inner, tol=0.6):
    """True when *inner* is mostly inside *outer*."""
    ia = rect_area(inner)
    if ia <= 0:
        return False
    return intersect_area(outer, inner) / ia >= tol


def dedup(findings, iou_thr=0.4):
    """Drop OCR findings that overlap a text/annot finding on the same page.

    Preference order: text/annot findings win over OCR findings (SPEC 5.4).
    Overlap = IoU >= threshold OR one rect largely contains the other.
    Also removes duplicate OCR findings that overlap each other (keep first).
    """
    src_rank = {"text": 0, "annot": 0, "manual": 0, "ocr": 1}
    # Stable-sort so preferred sources are considered first as "keepers".
    ordered = sorted(range(len(findings)), key=lambda i: src_rank.get(findings[i].source, 2))
    kept = []
    result_idx = set()
    for i in ordered:
        f = findings[i]
        drop = False
        for j in kept:
            g = findings[j]
            if g.page != f.page or g.kind != f.kind:
                continue
            if iou(f.rect, g.rect) >= iou_thr or contains(g.rect, f.rect) or contains(f.rect, g.rect):
                drop = True
                break
        if not drop:
            kept.append(i)
            result_idx.add(i)
    # Preserve original ordering for the survivors.
    return [findings[i] for i in range(len(findings)) if i in result_idx]


def prune_loose_rrn(findings):
    """Drop loose (line-wide) duplicates of the same OCR-read number.

    A whole-line OCR read that *contains* an RRN yields a character-fraction
    box estimate spanning much of the line, while another read of the number
    as its own segment yields a tight box.  Both survive :func:`dedup` when
    the vertical overlap is too small for IoU/containment.

    Rule (validated on a real rotated-scan sample): for two **OCR** findings
    of the same kind and same display text on the same page whose vertical
    ranges overlap (same visual row) and whose horizontal ranges intersect,
    keep the tighter (smaller-area) box and drop the looser one.
    """
    drop = set()
    for i, f in enumerate(findings):
        if f.source != "ocr" or f.kind not in ("rrn", "rrn_partial"):
            continue
        for j, g in enumerate(findings):
            if j <= i or g.source != "ocr":
                continue
            if g.page != f.page or g.kind != f.kind or g.text != f.text:
                continue
            fh = f.rect[3] - f.rect[1]
            gh = g.rect[3] - g.rect[1]
            v_ov = min(f.rect[3], g.rect[3]) - max(f.rect[1], g.rect[1])
            if v_ov < 0.5 * max(1e-6, min(fh, gh)):
                continue                      # different rows: keep both
            if min(f.rect[2], g.rect[2]) - max(f.rect[0], g.rect[0]) <= 0:
                continue                      # no horizontal intersection
            drop.add(j if rect_area(g.rect) >= rect_area(f.rect) else i)
    return [f for k, f in enumerate(findings) if k not in drop]
