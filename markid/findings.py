"""Finding model, rectangle helpers, and de-duplication."""

from dataclasses import dataclass, field
from typing import Optional, Tuple

Rect = Tuple[float, float, float, float]  # (x0, y0, x1, y1) in PDF points


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

    def rect_for_scope(self, scope: str) -> Optional[Rect]:
        """Return the rectangle to mask for *scope*, or None to skip.

        - rrn (text)  : back6 -> rect_back6 (fallback rect), full -> rect
        - rrn (ocr)   : always the whole rect — OCR boxes are too imprecise
                        for sub-digit targeting (rotated card numbers can
                        leave digits visible), so back6 masks the full number
        - rrn_partial : back6 -> skipped (front digits only), full -> rect
        - address/manual: always the full rect
        """
        if self.kind == "rrn":
            if scope == "back6":
                if self.source == "ocr":
                    return self.rect
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
