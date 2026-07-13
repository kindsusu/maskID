"""OCR-based detection (RapidOCR) with rotation passes and coordinate mapping.

The OCR engine is loaded lazily, once per process (first load takes a few
seconds).  Every page gets an upright (0 degree) pass; pages carrying a large
image (ID cards / verification screenshots) additionally get 90 and 270 degree
passes so sideways licence cards become readable.

Coordinate mapping (verified against SPEC 5.3)::

    render page -> pixmap at *dpi*  (original pixel size W x H)
    rotate array by k*90 deg CCW via np.rot90 -> OCR reads upright text
    OCR box is in the *rotated* pixel frame (x', y')
    inverse transform back to original pixels, then divide by dpi/72 -> PDF pt

        k = 0 : (x, y) = (x',       y')
        k = 1 : (x, y) = (W-1-y',   x')
        k = 3 : (x, y) = (y',       H-1-x')

Rotated passes render only a clip around each large image (plus margin)
instead of the whole page; W/H are then the clip's pixel size and the
pixmap origin offset (pix.x / pix.y, in device pixels) is added back after
the inverse transform to restore page coordinates.
"""

import fitz
import numpy as np

from .findings import Finding
from .patterns import find_addresses, find_rrn_full, find_rrn_partial, mask_display

MIN_IMAGE_PX = 250          # image side threshold that triggers rotation passes
DPI_UPRIGHT = 240
DPI_ROTATED = 300

_OCR = None


def get_ocr():
    """Lazily construct and cache the RapidOCR engine (offline, local models)."""
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR

        from .resources import rec_keys_path, rec_model_path

        # NOTE: rapidocr_onnxruntime 1.4.4 supports intra_op_num_threads= as a
        # Global kwarg (utils/parse_parameters.py -> utils/infer_engine.py),
        # but measurement on a 16-logical/8-physical-core CPU showed that
        # forcing os.cpu_count() (logical cores) is ~25% SLOWER per pass than
        # onnxruntime's default (-1 = physical cores).  We therefore keep the
        # default rather than overriding it.
        _OCR = RapidOCR(
            rec_model_path=rec_model_path(),
            rec_keys_path=rec_keys_path(),
        )
    return _OCR


# --------------------------------------------------------------------------
# geometry helpers
# --------------------------------------------------------------------------

def _inv(px, py, k, W, H):
    if k == 1:
        return (W - 1 - py, px)
    if k == 3:
        return (py, H - 1 - px)
    return (px, py)


def _inv_rect(x0, y0, x1, y1, k, W, H, scale):
    """Inverse-transform a reading-frame pixel rect to a PDF-point AABB."""
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    pts = [_inv(cx, cy, k, W, H) for cx, cy in corners]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs) / scale, min(ys) / scale, max(xs) / scale, max(ys) / scale)


def _frac_box(rb, start, end, length, pad_v=0.0):
    """Sub-box of reading-frame box *rb* for character span [start, end)."""
    x0, y0, x1, y1 = rb
    if length <= 0:
        return rb
    w = x1 - x0
    h = y1 - y0
    fx0 = x0 + (start / length) * w
    fx1 = x0 + (end / length) * w
    return (fx0, y0 - pad_v * h, fx1, y1 + pad_v * h)


def _pix_to_np(pix):
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        arr = arr[:, :, :3]
    return np.ascontiguousarray(arr)


def _inside_image(pdf_rect, image_rects):
    """Return the enclosing image rect whose area contains *pdf_rect*'s centre."""
    cx = (pdf_rect[0] + pdf_rect[2]) / 2.0
    cy = (pdf_rect[1] + pdf_rect[3]) / 2.0
    for r in image_rects:
        if r[0] <= cx <= r[2] and r[1] <= cy <= r[3]:
            return r
    return None


def _clamp(rect, bounds):
    return (
        max(rect[0], bounds[0]),
        max(rect[1], bounds[1]),
        min(rect[2], bounds[2]),
        min(rect[3], bounds[3]),
    )


def page_image_rects(page, min_px=MIN_IMAGE_PX):
    """PDF-space rects of images whose pixel size is at least *min_px* on a side."""
    rects = []
    for im in page.get_images(full=True):
        xref, width, height = im[0], im[2], im[3]
        if width < min_px and height < min_px:
            continue
        try:
            for r in page.get_image_rects(xref):
                rects.append((r[0], r[1], r[2], r[3]))
        except Exception:
            pass
    return rects


def _card_band(rrn_rb, reads, to_pdf, card_rect):
    """Address band below an RRN line, computed in the reading frame then mapped.

    Vertical : [rrn.bottom, rrn.bottom + 2.6*height]
    Horizontal: [rrn.x0 - h, max(rrn.x1, fragments.x1) + h]
    *to_pdf* maps a reading-frame box to page coordinates (handles rotation
    inverse, dpi scale and clip offset).  Clamped to the enclosing card rect
    so it never bleeds onto the page.
    """
    x0, y0, x1, y1 = rrn_rb
    h = y1 - y0
    band_y0 = y1
    band_y1 = y1 + 2.6 * h
    frag_x1 = x1
    for it in reads:
        bx0, by0, bx1, by1 = it["box"]
        cy = (by0 + by1) / 2.0
        if band_y0 <= cy <= band_y1 and bx1 > x0:
            frag_x1 = max(frag_x1, bx1)
    band = (x0 - h, band_y0, max(x1, frag_x1) + h, band_y1)
    return _clamp(to_pdf(band), card_rect)


# --------------------------------------------------------------------------
# passes
# --------------------------------------------------------------------------

def _run_pass(page, page_index, k, dpi, image_rects, address_on, clip_rect=None):
    """One OCR pass.  *clip_rect* (PDF coords) restricts rendering to a region;
    findings are always returned in full-page PDF coordinates."""
    if clip_rect is not None:
        pix = page.get_pixmap(dpi=dpi, clip=fitz.Rect(*clip_rect), alpha=False)
    else:
        pix = page.get_pixmap(dpi=dpi, alpha=False)
    if pix.width < 2 or pix.height < 2:
        return []
    arr = _pix_to_np(pix)
    W, H = pix.width, pix.height              # pre-rotation pixel size (of clip)
    if k:
        arr = np.rot90(arr, k)
    result, _ = get_ocr()(arr)
    if not result:
        return []

    scale = dpi / 72.0
    # Pixmap origin in device pixels (0 for full page, clip origin otherwise):
    # adding it after the inverse transform restores page coordinates.
    ox = pix.x / scale
    oy = pix.y / scale

    def to_pdf(rb):
        r = _inv_rect(*rb, k=k, W=W, H=H, scale=scale)
        return (r[0] + ox, r[1] + oy, r[2] + ox, r[3] + oy)

    reads = []
    for entry in result:
        box, text = entry[0], entry[1]
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        reads.append({"box": (min(xs), min(ys), max(xs), max(ys)), "text": text})

    out = []
    for item in reads:
        rb = item["box"]
        text = item["text"]
        L = len(text)
        pdf_line = to_pdf(rb)
        card_rect = _inside_image(pdf_line, image_rects)
        in_img = card_rect is not None

        full_spans = []
        # --- full RRN ---
        for hit in find_rrn_full(text):
            full_spans.append((hit["start"], hit["end"]))
            rect = to_pdf(_frac_box(rb, hit["start"], hit["end"], L))
            # No rect_back6 for OCR findings: box estimates are too loose for
            # sub-digit targeting, so back6 scope masks the whole number.
            out.append(Finding(
                id=-1, page=page_index, kind="rrn", source="ocr",
                text=mask_display("rrn", text[hit["start"]:hit["end"]]),
                rect=rect, rect_back6=None, conf=hit["conf"],
            ))
            if address_on and in_img:
                out.append(Finding(
                    id=-1, page=page_index, kind="address", source="ocr",
                    text="신분증 주소",
                    rect=_card_band(rb, reads, to_pdf, card_rect),
                ))

        # --- partial RRN: only inside an ID/verification image ---
        if in_img:
            for hit in find_rrn_partial(text, context_ok=True):
                if any(fs <= hit["start"] and hit["end"] <= fe for fs, fe in full_spans):
                    continue
                rect = to_pdf(_frac_box(rb, hit["start"], hit["end"], L))
                out.append(Finding(
                    id=-1, page=page_index, kind="rrn_partial", source="ocr",
                    text=mask_display("rrn_partial", text[hit["start"]:hit["end"]]),
                    rect=rect, rect_back6=None,
                ))
                if address_on:
                    out.append(Finding(
                        id=-1, page=page_index, kind="address", source="ocr",
                        text="신분증 주소",
                        rect=_card_band(rb, reads, to_pdf, card_rect),
                    ))

        # --- addresses recognised directly in an OCR line (bonus) ---
        if address_on:
            for start, end in find_addresses(text):
                rect = to_pdf(_frac_box(rb, start, end, L))
                out.append(Finding(
                    id=-1, page=page_index, kind="address", source="ocr",
                    text=mask_display("address", text[start:end]),
                    rect=rect,
                ))
    return out


def _union_area(rects):
    """Exact union area of axis-aligned rects (x-edge sweep)."""
    xs = sorted({r[0] for r in rects} | {r[2] for r in rects})
    total = 0.0
    for a, b in zip(xs, xs[1:]):
        w = b - a
        if w <= 0:
            continue
        mid = (a + b) / 2.0
        ivs = sorted((r[1], r[3]) for r in rects if r[0] <= mid <= r[2])
        cover = 0.0
        cur_end = None
        for y0, y1 in ivs:
            if cur_end is None or y0 > cur_end:
                cover += y1 - y0
                cur_end = y1
            elif y1 > cur_end:
                cover += y1 - cur_end
                cur_end = y1
        total += w * cover
    return total


CLIP_MARGIN_PT = 10.0       # margin around image rects for rotated passes
CLIP_MAX_FRAC = 0.70        # clip union above this page fraction -> full page


def detect_page_ocr(page, page_index, image_rects, address_on,
                    dpi_upright=DPI_UPRIGHT, dpi_rotated=DPI_ROTATED,
                    progress=None, cancel=None):
    """Run all OCR passes for a page and return findings.

    - 0 degree pass: whole page, always.
    - 90/270 degree passes: only around large images, clipped to each image
      rect +- CLIP_MARGIN_PT (clamped to the page).  When the clip union
      exceeds CLIP_MAX_FRAC of the page area the old full-page rotated
      passes are used instead (clipping would not save anything).

    *cancel* (optional ``threading.Event``) is checked before each pass;
    when set, ``markid.engine.OperationCancelled`` is raised (a single
    RapidOCR call cannot be interrupted mid-flight).
    """
    passes = [(0, dpi_upright, None)]
    if image_rects:
        pr = page.rect
        clips = []
        for r in image_rects:
            c = (
                max(pr.x0, r[0] - CLIP_MARGIN_PT),
                max(pr.y0, r[1] - CLIP_MARGIN_PT),
                min(pr.x1, r[2] + CLIP_MARGIN_PT),
                min(pr.y1, r[3] + CLIP_MARGIN_PT),
            )
            if c[2] > c[0] and c[3] > c[1]:
                clips.append(c)
        page_area = max(1.0, pr.width * pr.height)
        if clips and _union_area(clips) > CLIP_MAX_FRAC * page_area:
            clips = [None]
        for c in clips:
            passes.append((1, dpi_rotated, c))
            passes.append((3, dpi_rotated, c))

    out = []
    for idx, (k, dpi, clip) in enumerate(passes):
        if cancel is not None and cancel.is_set():
            # Lazy import: engine imports this module at load time, so the
            # exception class must be resolved at call time instead.
            from .engine import OperationCancelled
            raise OperationCancelled()
        if progress:
            progress(stage="ocr", page=page_index, sub=idx, total=len(passes))
        out += _run_pass(page, page_index, k, dpi, image_rects, address_on,
                         clip_rect=clip)
    return out
