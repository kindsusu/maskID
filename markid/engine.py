"""Document orchestration: open, detect, mask, save.

Progress callbacks receive keyword arguments so the GUI and CLI can render
whatever they need without a rigid signature::

    progress(stage="text"|"ocr"|"mask"|"save",
             file=<path>, file_index=i, file_total=n,
             page=<0-based>, total=<page count>, sub=..., sub_total=...)
"""

import os
from dataclasses import dataclass
from typing import List, Optional

import fitz

from .detect_ocr import detect_page_ocr, page_image_rects
from .detect_text import delete_rrn_annots, detect_annots, detect_text
from .edit import apply_edits
from .findings import Finding, dedup
from .mask import apply_masks

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".gif"}


class EncryptedPdfError(Exception):
    """Raised when an input PDF requires a password."""


@dataclass
class Options:
    scope: str = "back6"     # "full" | "back6"
    style: str = "box"       # "box" | "mosaic"
    address: bool = False
    ocr: bool = True
    png: bool = False
    outdir: Optional[str] = None


# --------------------------------------------------------------------------
# document open / path helpers
# --------------------------------------------------------------------------

def open_document(path):
    """Open a PDF or image file as a fitz document.

    Images are wrapped into a single-page PDF so the same pipeline applies.
    Raises EncryptedPdfError for password-protected PDFs.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXTS:
        img = fitz.open(path)
        pdf_bytes = img.convert_to_pdf()
        img.close()
        return fitz.open("pdf", pdf_bytes)

    doc = fitz.open(path)
    if doc.needs_pass:
        doc.close()
        raise EncryptedPdfError("암호화된 PDF는 지원하지 않습니다.")
    return doc


def output_path_for(input_path, opts, suffix="_masked"):
    outdir = opts.outdir or os.path.join(
        os.path.dirname(os.path.abspath(input_path)), "masked"
    )
    name = os.path.splitext(os.path.basename(input_path))[0] + suffix + ".pdf"
    return os.path.join(outdir, name)


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------

def detect(doc, opts, progress=None, **pinfo) -> List[Finding]:
    """Run all detectors over *doc* and return de-duplicated, id-numbered findings."""
    findings: List[Finding] = []
    total = doc.page_count
    for i in range(total):
        page = doc[i]
        if progress:
            progress(stage="text", page=i, total=total, **pinfo)
        findings += detect_text(page, i, opts.address)
        findings += detect_annots(page, i)
        if opts.ocr:
            irects = page_image_rects(page)
            findings += detect_page_ocr(
                page, i, irects, opts.address, progress=progress
            )
    findings = dedup(findings)
    for idx, f in enumerate(findings):
        f.id = idx
    return findings


# --------------------------------------------------------------------------
# result re-verification
# --------------------------------------------------------------------------

def verify_result(doc, opts=None) -> dict:
    """Re-scan a saved (masked) document for *residual* complete RRNs.

    Re-runs the text-layer and annotation detectors (addresses OFF, no OCR)
    and counts **only** ``kind == "rrn"`` (complete 13-digit) findings.
    Partial RRNs and addresses are excluded on purpose to avoid over-alarming.
    OCR is intentionally skipped for speed (a residual image number cannot be
    confirmed cheaply); this is documented for the user in the README.

    Returns ``{"residual": n, "pages": [...]}`` with 1-based, sorted,
    de-duplicated page numbers that still carry a complete RRN.  A residual > 0
    typically means the user unchecked (excluded) that finding on purpose — it
    is reported honestly either way.
    """
    residual = 0
    pages = set()
    for i in range(doc.page_count):
        page = doc[i]
        found = detect_text(page, i, False)      # address=False
        found += detect_annots(page, i)
        for f in found:
            if f.kind == "rrn":                  # complete RRN only
                residual += 1
                pages.add(i + 1)                 # 1-based for humans
    return {"residual": residual, "pages": sorted(pages)}


# --------------------------------------------------------------------------
# masking / save
# --------------------------------------------------------------------------

def apply_and_save(doc, findings, opts, out_path, edits=None, progress=None, **pinfo):
    """Apply text edits, then enabled findings; save. Returns (count, png_paths).

    *edits* (GUI only, list of EditOp) are applied first: each edit's own
    redaction completes before the mask redactions run, so an edit and a mask
    on the same page never interfere.
    """
    if edits:
        apply_edits(doc, edits)

    by_page = {}
    for f in findings:
        if not f.enabled:
            continue
        rect = f.rect_for_scope(opts.scope)
        if rect is None:
            continue
        by_page.setdefault(f.page, []).append((rect, f.source))

    # Annot findings the user unchecked must survive: collect their rects so
    # delete_rrn_annots skips the matching annotations.  When no annot
    # findings exist at all this map stays empty and every RRN-bearing
    # annotation is deleted, exactly as before (CLI path unchanged).
    keep_annot_rects = {}
    for f in findings:
        if f.source == "annot" and not f.enabled:
            keep_annot_rects.setdefault(f.page, []).append(f.rect)

    masked = 0
    total = doc.page_count
    for i in range(total):
        page = doc[i]
        delete_rrn_annots(page, skip_rects=keep_annot_rects.get(i))
        rects = by_page.get(i, [])
        if rects:
            # Text-line bboxes let apply_masks snap loose OCR rects away from
            # adjacent lines (a grazed line would otherwise be fully redacted).
            line_bboxes = (
                _page_line_bboxes(page)
                if any(src == "ocr" for _r, src in rects)
                else None
            )
            masked += apply_masks(page, rects, opts.style, line_bboxes)
        if progress:
            progress(stage="mask", page=i, total=total, **pinfo)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    if progress:
        progress(stage="save", page=total, total=total, **pinfo)
    # garbage=3 + deflate ensures removed content does not linger in the file.
    doc.save(out_path, garbage=3, deflate=True)

    pngs = []
    if opts.png:
        pngs = _save_pngs(doc, out_path)
    return masked, pngs


def _page_line_bboxes(page):
    """Text-layer line bounding boxes for one page (for OCR-rect snapping)."""
    out = []
    try:
        d = page.get_text("dict")
    except Exception:
        return out
    for block in d.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            bb = line.get("bbox")
            if bb:
                out.append(tuple(bb))
    return out


def save_edits_only(input_path, edits, opts):
    """Apply enabled text edits and save — no detection, masking or annot
    deletion whatsoever (GUI '수정만 저장').

    Output uses the same directory rule as masking but the ``_edited.pdf``
    suffix so it never collides with a ``_masked.pdf``.
    Returns (applied_count, out_path, png_paths).
    """
    doc = open_document(input_path)
    try:
        enabled = [e for e in (edits or []) if e.enabled]
        apply_edits(doc, enabled)
        out_path = output_path_for(input_path, opts, suffix="_edited")
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        doc.save(out_path, garbage=3, deflate=True)
        pngs = _save_pngs(doc, out_path) if opts.png else []
        return len(enabled), out_path, pngs
    finally:
        doc.close()


def _save_pngs(doc, out_path, dpi=150):
    base = os.path.splitext(out_path)[0]
    paths = []
    for i in range(doc.page_count):
        pix = doc[i].get_pixmap(dpi=dpi, alpha=False)
        p = "%s_p%02d.png" % (base, i + 1)
        pix.save(p)
        paths.append(p)
    return paths


# --------------------------------------------------------------------------
# one-shot convenience (used by the CLI)
# --------------------------------------------------------------------------

def run(input_path, opts, progress=None, edits=None, **pinfo):
    """Detect + mask + save one document. Returns a report dict.

    *edits* (GUI batch path) are applied to the freshly opened document
    before detection, so detection sees the edited content; apply_and_save
    is then called without edits (already applied).
    """
    doc = open_document(input_path)
    try:
        if edits:
            apply_edits(doc, edits)
        findings = detect(doc, opts, progress, **pinfo)
        out_path = output_path_for(input_path, opts)
        masked, pngs = apply_and_save(
            doc, findings, opts, out_path, progress=progress, **pinfo
        )
        # Re-open the saved copy and confirm no complete RRN text remains.
        verify = {"residual": -1, "pages": []}   # -1 == verification failed
        try:
            vdoc = fitz.open(out_path)
            try:
                verify = verify_result(vdoc, opts)
            finally:
                vdoc.close()
        except Exception:
            pass
        return {
            "input": os.path.abspath(input_path),
            "output": os.path.abspath(out_path),
            "masked_count": masked,
            "scope": opts.scope,
            "style": opts.style,
            "address": opts.address,
            "ocr": opts.ocr,
            "findings": [f.to_report() for f in findings],
            "png": [os.path.abspath(p) for p in pngs],
            "verify": verify,
        }
    finally:
        doc.close()
