"""Text-layer and annotation detection.

Assembles rawdict character boxes into visual lines, then applies the pure
pattern logic.  Precise masking rectangles come from the union of the matched
characters' bounding boxes, so masking never spills into neighbouring text.
"""

from .findings import Finding, iou, union_rect
from .patterns import (
    RRN_LABEL_RE,
    find_addresses,
    find_rrn_full,
    find_rrn_partial,
    mask_display,
)


def _collect_lines(page):
    """Return visual text lines with per-character boxes.

    Each entry: ``{"text", "chars": [(ch, (x0,y0,x1,y1)), ...], "bbox"}``.
    ``text[i]`` corresponds 1:1 to ``chars[i]`` (rawdict stores one char per
    entry), so a regex span on ``text`` maps directly onto character boxes.
    """
    d = page.get_text("rawdict")
    lines = []
    for block in d.get("blocks", []):
        if block.get("type") != 0:  # skip image blocks
            continue
        for line in block.get("lines", []):
            chars = []
            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    chars.append((ch["c"], tuple(ch["bbox"])))
            if not chars:
                continue
            lines.append(
                {
                    "text": "".join(c for c, _ in chars),
                    "chars": chars,
                    "bbox": tuple(line["bbox"]),
                }
            )
    return lines


def _same_row(a, b):
    ay0, ay1 = a["bbox"][1], a["bbox"][3]
    by0, by1 = b["bbox"][1], b["bbox"][3]
    overlap = min(ay1, by1) - max(ay0, by0)
    return overlap > 0.5 * min(ay1 - ay0, by1 - by0)


def _has_rrn_label_left(value_line, lines):
    """True when a ``주민`` label sits in the same row, left of *value_line*."""
    vx0 = value_line["bbox"][0]
    for other in lines:
        if other is value_line:
            continue
        if not RRN_LABEL_RE.search(other["text"]):
            continue
        if other["bbox"][0] < vx0 and _same_row(other, value_line):
            return True
    return False


def _rect_from_span(chars, start, end):
    return union_rect([bb for _, bb in chars[start:end]])


def detect_text(page, page_index, address_on):
    """Detect RRNs (full/partial) and, optionally, addresses in the text layer."""
    lines = _collect_lines(page)
    out = []
    for line in lines:
        text = line["text"]
        chars = line["chars"]

        # --- full RRN ---
        full_spans = []
        for hit in find_rrn_full(text):
            full_spans.append((hit["start"], hit["end"]))
            conf = hit["conf"]
            if conf == "low" and _has_rrn_label_left(line, lines):
                conf = "high"
            out.append(
                Finding(
                    id=-1,
                    page=page_index,
                    kind="rrn",
                    source="text",
                    text=mask_display("rrn", text[hit["start"]:hit["end"]]),
                    rect=_rect_from_span(chars, hit["start"], hit["end"]),
                    rect_back6=_rect_from_span(chars, hit["back6_start"], hit["back6_end"]),
                    conf=conf,
                )
            )

        # --- partial RRN (front 6 only) ---
        context = _has_rrn_label_left(line, lines)
        for hit in find_rrn_partial(text, context_ok=context):
            # skip if this span sits inside an already-matched full RRN
            if any(fs <= hit["start"] and hit["end"] <= fe for fs, fe in full_spans):
                continue
            out.append(
                Finding(
                    id=-1,
                    page=page_index,
                    kind="rrn_partial",
                    source="text",
                    text=mask_display("rrn_partial", text[hit["start"]:hit["end"]]),
                    rect=_rect_from_span(chars, hit["start"], hit["end"]),
                    rect_back6=None,
                    conf="high",
                )
            )

        # --- addresses ---
        if address_on:
            for start, end in find_addresses(text):
                out.append(
                    Finding(
                        id=-1,
                        page=page_index,
                        kind="address",
                        source="text",
                        text=mask_display("address", text[start:end]),
                        rect=_rect_from_span(chars, start, end),
                        rect_back6=None,
                    )
                )
    return out


def _annot_text(annot):
    parts = []
    info = annot.info or {}
    if info.get("content"):
        parts.append(info["content"])
    try:
        fv = annot.field_value
        if fv:
            parts.append(str(fv))
    except Exception:
        pass
    return " ".join(parts).strip()


def detect_annots(page, page_index):
    """Detect RRNs inside annotations (FreeText/Square/Widget overlays)."""
    out = []
    try:
        annots = list(page.annots() or [])
    except Exception:
        annots = []
    for annot in annots:
        text = _annot_text(annot)
        if not text:
            continue
        full = find_rrn_full(text)
        partial = find_rrn_partial(text)
        if not full and not partial:
            continue
        rect = tuple(annot.rect)
        kind = "rrn" if full else "rrn_partial"
        raw = text[full[0]["start"]:full[0]["end"]] if full else text
        out.append(
            Finding(
                id=-1,
                page=page_index,
                kind=kind,
                source="annot",
                text=mask_display(kind, raw),
                rect=rect,
                rect_back6=None,  # annot rect is whole overlay; no sub-rect
            )
        )
    return out


def delete_rrn_annots(page, skip_rects=None):
    """Delete annotations whose content/value contains an RRN. Returns count.

    Annotations matching any rect in *skip_rects* are preserved — used for
    annot findings the user unchecked in the GUI (enabled=False).  Matching:
    annot rect centre inside a skip rect, or IoU >= 0.4.
    """
    try:
        annots = list(page.annots() or [])
    except Exception:
        return 0
    skip_rects = skip_rects or []
    n = 0
    for annot in annots:
        text = _annot_text(annot)
        if not (text and (find_rrn_full(text) or find_rrn_partial(text))):
            continue
        r = annot.rect
        cx = (r.x0 + r.x1) / 2.0
        cy = (r.y0 + r.y1) / 2.0
        keep = any(
            (s[0] <= cx <= s[2] and s[1] <= cy <= s[3])
            or iou((r.x0, r.y0, r.x1, r.y1), s) >= 0.4
            for s in skip_rects
        )
        if keep:
            continue
        page.delete_annot(annot)
        n += 1
    return n
