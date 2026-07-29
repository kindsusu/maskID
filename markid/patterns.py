"""Pure detection logic (regex + validation).

Every function here is a pure function over strings so it can be unit tested
without PDFs or OCR.  Geometry / coordinate handling lives in the detect_*
modules; this module only decides *what* text is a resident-registration
number (RRN) or an address and *which character span* to mask.

Terminology
-----------
RRN full     : ``YYMMDD-Gxxxxxx`` (6 digits, separator, 7 digits, gender 1-8).
RRN partial  : only the front 6 digits (+ optional trailing hyphen).  Only
               treated as PII when a ``주민`` label sits to its left, or when
               it originates from inside an ID-card / verification image.
"""

import re

# --------------------------------------------------------------------------
# RRN patterns
# --------------------------------------------------------------------------

# Full RRN.  Lookbehind/lookahead reject numbers that are part of a longer
# digit / dash / dot run (business numbers, account numbers, dates, phone
# numbers).  Separator may be hyphen / non-breaking hyphen / en-dash with
# optional surrounding space.  Second group must start 1-8 (valid gender code).
RRN_FULL_RE = re.compile(
    r"(?<![\d\-.‐‑‒–])"
    r"(\d{6})\s?[-‐‑‒–]\s?([1-8]\d{6})"
    r"(?![\d\-.‐‑‒–])"
)

# A bare front-6 token (birth date portion), not glued to more digits/dots.
# A trailing hyphen is allowed *only* when nothing (digit) follows it, so this
# never fires on the front half of a full RRN.
RRN_PARTIAL_RE = re.compile(
    r"(?<![\d\-.‐‑‒–])(\d{6})\s?[-‐‑‒–]?"
    r"(?![\d.\-‐‑‒–])"
)

RRN_LABEL_RE = re.compile(r"주민")

# Corporate / business context markers.  A corporate registration number
# (법인등록번호, 6-7 digits) is structurally identical to an RRN and its date
# part often validates, so context is the only reliable discriminator.
CORP_CONTEXT_RE = re.compile(r"사업자|법인|주식회사|\(주|㈜")


def corp_context_excluded(line_text, above_texts=()):
    """True when RRN-shaped numbers in *line_text* are corporate context.

    Rules (over-masking is preferred to under-masking):
    - a ``주민`` label in the line always wins -> never excluded;
    - a corporate marker in the same line -> excluded;
    - otherwise, when the line itself carries no clue, a corporate marker in
      an adjacent line above (caller supplies *above_texts*) excludes —
      unless that line mentions ``주민``.
    """
    if RRN_LABEL_RE.search(line_text):
        return False
    if CORP_CONTEXT_RE.search(line_text):
        return True
    for t in above_texts:
        if RRN_LABEL_RE.search(t):
            return False
        if CORP_CONTEXT_RE.search(t):
            return True
    return False


def valid_date6(six):
    """True when the 6-digit prefix looks like a real YYMMDD date."""
    if len(six) != 6 or not six.isdigit():
        return False
    mm = int(six[2:4])
    dd = int(six[4:6])
    return 1 <= mm <= 12 and 1 <= dd <= 31


def find_rrn_full(text):
    """Find full RRNs in *text*.

    Returns a list of dicts::

        {"start", "end",            # span of the whole RRN
         "back6_start", "back6_end",# span of the last 6 digits (masked in back6)
         "conf"}                    # "high" | "low"  (label may promote later)
    """
    out = []
    for m in RRN_FULL_RE.finditer(text):
        g2s, g2e = m.span(2)
        out.append(
            {
                "start": m.start(),
                "end": m.end(),
                "back6_start": g2e - 6,   # last 6 chars of the 7-digit group
                "back6_end": g2e,
                "conf": "high" if valid_date6(m.group(1)) else "low",
            }
        )
    return out


def find_rrn_partial(text, context_ok=False):
    """Find front-6 partial RRNs in *text*.

    A candidate qualifies only when *context_ok* is True (caller established
    that a ``주민`` label sits in the same visual row, or the token comes from
    inside an ID-card image) **or** a ``주민`` label appears to its left within
    *text* itself.

    Returns a list of dicts ``{"start", "end"}`` (span of the digits +
    optional hyphen).  ``back6`` is never applicable to partials.
    """
    out = []
    for m in RRN_PARTIAL_RE.finditer(text):
        left = text[: m.start()]
        if context_ok or RRN_LABEL_RE.search(left):
            out.append({"start": m.start(), "end": m.end()})
    return out


# --------------------------------------------------------------------------
# Address patterns
# --------------------------------------------------------------------------

_SIDO_FULL = (
    r"서울특별시|부산광역시|대구광역시|인천광역시|광주광역시|대전광역시|울산광역시|"
    r"세종특별자치시|경기도|강원특별자치도|강원도|충청북도|충청남도|"
    r"전북특별자치도|전라북도|전라남도|경상북도|경상남도|제주특별자치도"
)
# Short forms must be followed by whitespace so they behave like a standalone
# administrative token (avoids matching words that merely start with these
# two syllables).
_SIDO_SHORT = (
    r"(?:서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|"
    r"전북|전남|경북|경남|제주)(?=\s)"
)
# Loose form tolerates 1-2 char OCR errors in the middle, e.g. 세종특별자지시.
_SIDO_LOOSE = r"[가-힣]{2}특별[가-힣]{0,3}시|[가-힣]{2}특별[가-힣]{0,3}도"

_SIDO = r"(?:%s|%s|%s)" % (_SIDO_FULL, _SIDO_SHORT, _SIDO_LOOSE)

# sido ... <road name>로/길 <number>
ADDR_STREET_RE = re.compile(
    _SIDO + r"[\s가-힣A-Za-z0-9()]*?(?:로|길)\s*\d+(?:[-\s]?\d+)?(?:번지)?"
)
# building/unit detail, e.g. 202동611호 / 112동 1005호 / B동 901호.
# Building token: digits or one Latin letter.  A single Hangul syllable before
# 동 is deliberately excluded (words ending in 동 would false-positive).
ADDR_DETAIL_RE = re.compile(r"(?:\d+|[A-Za-z])\s*동\s*\d+\s*호")

# Address label words (used by the text-layer detector to scope value cells).
ADDR_LABEL_RE = re.compile(r"자택주소|회사주소|상세주소|거주지|주소")


def _merge_spans(spans):
    if not spans:
        return []
    spans = sorted(spans)
    merged = [list(spans[0])]
    for s, e in spans[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def find_addresses(text):
    """Find address spans in *text* (street form + detail form, merged)."""
    spans = []
    for m in ADDR_STREET_RE.finditer(text):
        spans.append((m.start(), m.end()))
    for m in ADDR_DETAIL_RE.finditer(text):
        spans.append((m.start(), m.end()))
    return _merge_spans(spans)


# --------------------------------------------------------------------------
# Display helpers
# --------------------------------------------------------------------------

def mask_display(kind, raw):
    """Return a privacy-preserving preview string for UI / report."""
    raw = raw.strip()
    if kind == "rrn":
        # keep up to the gender digit, star the rest: 900101-2******
        digits_seen = 0
        out = []
        for ch in raw:
            if ch.isdigit():
                digits_seen += 1
                out.append(ch if digits_seen <= 7 else "*")
            else:
                out.append(ch)
        return "".join(out)
    if kind == "rrn_partial":
        return raw
    if kind in ("address", "manual"):
        if len(raw) <= 8:
            return raw
        return raw[:6] + "…"
    return raw
