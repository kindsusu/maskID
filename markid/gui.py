# -*- coding: utf-8 -*-
"""Tkinter GUI (all user-facing text in Korean).

Layout: a top navy header bar, a left control panel grouped into cards
(대상 파일 / 마스킹 옵션 / 실행 / 진행 상황), a right area with a preview canvas
(original / result toggle, page nav, detection overlays) above a findings table
with per-row checkboxes, and a bottom status bar.

Detection and masking run on a background thread; the worker communicates with
the Tk main loop through a thread-safe queue polled via ``after``.

Visual system (v1.3 restyle): deep-navy corporate palette on a light theme,
built with a ``clam``-based ttk.Style so buttons/treeview/progressbar share
consistent hover/pressed/disabled states.  Cards and coloured surfaces use tk
widgets for precise background control; interactive controls use styled ttk.
"""

import os
import queue
import threading
import traceback

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import fitz

from . import __version__
from .edit import EditOp, apply_edits, chars_in_rect
from .engine import (
    EncryptedPdfError,
    OperationCancelled,
    Options,
    apply_and_save,
    detect,
    extract_pages,
    open_document,
    output_path_for,
    parse_page_ranges,
    run,
    save_edits_only,
    verify_result,
)
from .findings import KIND_LABEL_KO, SOURCE_LABEL_KO, Finding, contains, iou

# --------------------------------------------------------------------------
# Design tokens
# --------------------------------------------------------------------------
NAVY = "#1B3A5C"          # primary
NAVY_HOVER = "#24476E"    # hover
NAVY_DARK = "#142C46"     # pressed / strong action
APP_BG = "#F5F7FA"        # app background
CARD_BG = "#FFFFFF"       # card / panel
BORDER = "#D8DEE6"        # separators / card edges
TEXT = "#1A202C"          # body text
TEXT_SUB = "#5A6472"      # secondary text
OK = "#178A3A"            # success
WARN = "#B7791F"          # warning
DANGER = "#C53030"        # danger
CANVAS_BG = "#E9EDF2"     # preview backdrop
HEAD_BG = "#EEF2F7"       # treeview heading / status
ROW_ODD = "#F4F6F9"       # zebra row
STATUS_BG = "#EDF1F6"     # status bar surface
PAGE_EDGE = "#C2CAD4"     # preview page border ("paper" feel)

FONT = ("맑은 고딕", 10)
FONT_BOLD = ("맑은 고딕", 10, "bold")   # section titles
FONT_TITLE = ("맑은 고딕", 14, "bold")  # header title
FONT_GROUP = ("맑은 고딕", 9, "bold")   # sub-group labels
FONT_SUB = ("맑은 고딕", 9)             # captions / status / header meta
FONT_LOG = ("맑은 고딕", 9)             # progress log

# Detection overlay colours — semantic meaning preserved, harmonised to palette.
COLOR = {
    "rrn": "#C53030",         # 주민번호 - red (danger)
    "rrn_partial": "#C53030",
    "address": "#1B5FBF",     # 주소 - blue
    "manual": "#178A3A",      # 수동 - green
    "edit": "#DD7A1A",        # 글자수정 - orange
    "disabled": "#9AA0A6",    # 제외됨 - gray
}

ZOOM_MIN = 0.25
ZOOM_MAX = 4.0
ZOOM_STEP = 1.2


def _ell(s, n=12):
    return s if len(s) <= n else s[:n] + "…"


def _work_area_height(root):
    """Height of the primary monitor's work area (taskbar excluded).

    Uses SPI_GETWORKAREA so the value lives in the same (possibly
    DPI-virtualised) coordinate space Tk positions windows in; falls back to
    the full screen height off Windows or on API failure.
    """
    try:
        import ctypes
        import ctypes.wintypes
        rect = ctypes.wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0,
                                                      ctypes.byref(rect), 0):
            return int(rect.bottom - rect.top)
    except Exception:
        pass
    try:
        return root.winfo_screenheight()
    except Exception:
        return 880


CHECK_ON = "☑"   # ☑
CHECK_OFF = "☐"  # ☐


class MarkIDApp:
    def __init__(self, root):
        self.root = root
        root.title("MarkID - 계약서 주민등록번호 마스킹 도구 v%s" % __version__)
        # Fit small work areas: 150% DPI laptops clamp windows to ~670 logical
        # px, which used to cut off the bottom of the left panel.
        h = min(840, max(600, _work_area_height(root) - 40))
        root.geometry("1280x%d" % h)
        root.minsize(1100, 640)
        try:
            root.configure(background=APP_BG)
        except Exception:
            pass
        # Application icon (the .ico is produced by the build tooling; tolerate
        # its absence in a source checkout).
        try:
            from .resources import icon_path
            p = icon_path()
            if os.path.isfile(p):
                root.iconbitmap(p)
        except Exception:
            pass

        self.files = []
        self.work_doc = None        # working copy: original + enabled edits
        self.result_doc = None      # masked result document (in memory)
        self.result_path = None
        self.current_path = None    # file the work_doc/edits belong to
        self.findings = []
        self.edits = []             # EditOp list (text replacements)
        self.cur_page = 0
        self.page_count = 0
        self.preview_mode = "original"
        self.scale = 1.0
        self.zoom = 1.0             # 1.0 = fit-to-window scale
        self.show_overlays = True   # draw detection/edit boxes on the preview
        self.tkimg = None
        self.manual_mode = False
        self.edit_mode = False
        self._drag_start = None
        self._busy = False
        self.sel_id = None            # selected finding id (box editing, v1.5)
        self._box_drag = None         # in-progress move/resize of the selection
        self._box_prev_item = None    # live drag preview canvas item
        self.undo_stack = []          # ("manual_add", id) / ("edit_add", id) /
                                      # ("delete_finding", Finding) / ("delete_edit", EditOp) /
                                      # ("adjust", (id, old_rect, old_back6, old_flag))
        self.cancel_event = threading.Event()
        self.msg_q = queue.Queue()

        self._build()
        self.root.after(100, self._poll_queue)

    # ------------------------------------------------------------------ UI
    def _init_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", font=FONT, background=APP_BG, foreground=TEXT)
        style.configure("TFrame", background=APP_BG)
        style.configure("TLabel", background=APP_BG, foreground=TEXT)
        style.configure("TSeparator", background=BORDER)
        style.configure("TEntry", fieldbackground=CARD_BG, foreground=TEXT,
                        bordercolor=BORDER, insertcolor=TEXT)

        # -- Buttons -----------------------------------------------------
        # Default (neutral) button: visible solid border so it reads as a
        # button, not flat text.
        style.configure("TButton", font=FONT, padding=(8, 4),
                        background=CARD_BG, foreground=TEXT,
                        bordercolor="#B6C0CC", relief="solid", borderwidth=1,
                        focusthickness=0)
        style.map("TButton",
                  background=[("active", HEAD_BG), ("pressed", "#E2E8F0"),
                              ("disabled", "#F0F2F5")],
                  foreground=[("disabled", "#A0A6B0")],
                  bordercolor=[("active", NAVY), ("disabled", BORDER)])

        # Compact bordered button (dense rows in the left panel).
        style.configure("Compact.TButton", font=FONT_SUB, padding=(4, 3),
                        background=CARD_BG, foreground=TEXT,
                        bordercolor="#B6C0CC", relief="solid", borderwidth=1,
                        focusthickness=0)
        style.map("Compact.TButton",
                  background=[("active", HEAD_BG), ("pressed", "#E2E8F0"),
                              ("disabled", "#F0F2F5")],
                  foreground=[("disabled", "#A0A6B0")],
                  bordercolor=[("active", NAVY), ("disabled", BORDER)])

        # Primary (검사/수동 저장 등 주요 실행) - navy fill.
        style.configure("Primary.TButton", font=FONT_BOLD, padding=(10, 3),
                        background=NAVY, foreground="#FFFFFF",
                        relief="flat", borderwidth=0)
        style.map("Primary.TButton",
                  background=[("active", NAVY_HOVER), ("pressed", NAVY_DARK),
                              ("disabled", "#9FB0C2")],
                  foreground=[("disabled", "#EAEFF4")])

        # Strong (검출 항목 마스킹 저장) - deeper navy for emphasis.
        style.configure("Strong.TButton", font=FONT_BOLD, padding=(10, 3),
                        background=NAVY_DARK, foreground="#FFFFFF",
                        relief="flat", borderwidth=0)
        style.map("Strong.TButton",
                  background=[("active", NAVY), ("pressed", "#0E2338"),
                              ("disabled", "#9FB0C2")],
                  foreground=[("disabled", "#EAEFF4")])

        # Secondary (③) - white fill, navy outline + text.
        style.configure("Secondary.TButton", font=FONT_BOLD, padding=(10, 3),
                        background=CARD_BG, foreground=NAVY,
                        relief="solid", borderwidth=1, bordercolor=NAVY)
        style.map("Secondary.TButton",
                  background=[("active", HEAD_BG), ("pressed", "#E2E8F0"),
                              ("disabled", "#F5F7FA")],
                  foreground=[("disabled", "#A0A6B0")],
                  bordercolor=[("disabled", BORDER)])

        # Compact primary / secondary (half-width action rows, 9pt bold).
        style.configure("CompactPrimary.TButton", font=FONT_GROUP, padding=(6, 3),
                        background=NAVY, foreground="#FFFFFF",
                        relief="flat", borderwidth=0)
        style.map("CompactPrimary.TButton",
                  background=[("active", NAVY_HOVER), ("pressed", NAVY_DARK),
                              ("disabled", "#9FB0C2")],
                  foreground=[("disabled", "#EAEFF4")])
        style.configure("CompactSecondary.TButton", font=FONT_GROUP, padding=(6, 3),
                        background=CARD_BG, foreground=NAVY,
                        relief="solid", borderwidth=1, bordercolor=NAVY)
        style.map("CompactSecondary.TButton",
                  background=[("active", HEAD_BG), ("pressed", "#E2E8F0"),
                              ("disabled", "#F5F7FA")],
                  foreground=[("disabled", "#A0A6B0")],
                  bordercolor=[("disabled", BORDER)])
        # Compact toggle (left-panel manual-mode button, on-state navy).
        style.configure("CompactToggled.TButton", font=FONT_GROUP, padding=(4, 3),
                        background=NAVY, foreground="#FFFFFF",
                        relief="flat", borderwidth=0)
        style.map("CompactToggled.TButton",
                  background=[("active", NAVY_HOVER), ("pressed", NAVY_DARK)])

        # Toolbar default / toggled.
        style.configure("Tool.TButton", font=FONT, padding=(8, 5),
                        background=CARD_BG, foreground=TEXT,
                        relief="flat", borderwidth=1, bordercolor=BORDER)
        style.map("Tool.TButton",
                  background=[("active", HEAD_BG), ("pressed", "#E2E8F0"),
                              ("disabled", "#F0F2F5")],
                  bordercolor=[("active", NAVY), ("disabled", BORDER)],
                  foreground=[("disabled", "#A0A6B0")])
        style.configure("Toggled.TButton", font=FONT_BOLD, padding=(8, 5),
                        background=NAVY, foreground="#FFFFFF",
                        relief="flat", borderwidth=0)
        style.map("Toggled.TButton",
                  background=[("active", NAVY_HOVER), ("pressed", NAVY_DARK)])

        # -- Treeview ----------------------------------------------------
        style.configure("Treeview", font=FONT, rowheight=24,
                        background=CARD_BG, fieldbackground=CARD_BG,
                        foreground=TEXT, bordercolor=BORDER, borderwidth=1)
        style.configure("Treeview.Heading", font=FONT_BOLD,
                        background=HEAD_BG, foreground=NAVY, relief="flat",
                        padding=(4, 4))
        style.map("Treeview.Heading", background=[("active", "#E2E8F0")])
        style.map("Treeview",
                  background=[("selected", NAVY)],
                  foreground=[("selected", "#FFFFFF")])

        # -- Progressbar -------------------------------------------------
        style.configure("Horizontal.TProgressbar",
                        background=NAVY, troughcolor="#E2E8F0",
                        bordercolor=BORDER, lightcolor=NAVY, darkcolor=NAVY)

        # -- Scrollbar (subtle) -----------------------------------------
        style.configure("TScrollbar", background=HEAD_BG,
                        troughcolor=APP_BG, bordercolor=BORDER,
                        arrowcolor=TEXT_SUB)

    def _build(self):
        self._init_style()

        self._build_header(self.root)
        self._build_statusbar(self.root)   # bottom (packed before body fills)

        body = tk.Frame(self.root, bg=APP_BG)
        body.pack(side="top", fill="both", expand=True)

        left = tk.Frame(body, bg=APP_BG, width=352)
        left.pack(side="left", fill="y", padx=(8, 4), pady=4)
        left.pack_propagate(False)
        right = tk.Frame(body, bg=APP_BG)
        right.pack(side="left", fill="both", expand=True, padx=(4, 8), pady=4)

        self._build_left(left)
        self._build_right(right)
        self._bind_shortcuts()

    # ---------------------------------------------------------- header
    def _build_header(self, parent):
        hdr = tk.Frame(parent, bg=NAVY, height=44)
        hdr.pack(side="top", fill="x")
        hdr.pack_propagate(False)
        self.hdr = hdr

        left = tk.Frame(hdr, bg=NAVY)
        left.pack(side="left", padx=16)
        tk.Label(left, text="MarkID", bg=NAVY, fg="#FFFFFF",
                 font=FONT_TITLE).pack(side="left")
        tk.Label(left, text="계약서 개인정보 마스킹 도구", bg=NAVY,
                 fg="#C3D2E2", font=FONT).pack(side="left", padx=(10, 0))

        right = tk.Frame(hdr, bg=NAVY)
        right.pack(side="right", padx=14)
        tk.Label(right, text="v%s · HRteam" % __version__, bg=NAVY,
                 fg="#AFC0D4", font=FONT_SUB).pack(side="left", padx=(0, 12))
        self.btn_info = tk.Button(
            right, text="정보", command=self.show_info, relief="flat",
            bg=NAVY_HOVER, fg="#FFFFFF", activebackground=NAVY_DARK,
            activeforeground="#FFFFFF", bd=0, padx=12, pady=3, font=FONT,
            cursor="hand2", highlightthickness=0,
        )
        self.btn_info.pack(side="left")

    # -------------------------------------------------------- status bar
    def _build_statusbar(self, parent):
        wrap = tk.Frame(parent, bg=BORDER)     # 1px top rule
        wrap.pack(side="bottom", fill="x")
        bar = tk.Frame(wrap, bg=STATUS_BG, height=22)
        bar.pack(fill="x", pady=(1, 0))
        bar.pack_propagate(False)
        self.sb_file = tk.Label(bar, text="파일 없음", bg=STATUS_BG,
                                fg=TEXT_SUB, font=FONT_SUB, anchor="w")
        self.sb_file.pack(side="left", padx=12)
        self.sb_status = tk.Label(bar, text="대기", bg=STATUS_BG, fg=NAVY,
                                  font=FONT_SUB, anchor="e")
        self.sb_status.pack(side="right", padx=12)
        self.sb_page = tk.Label(bar, text="- / -  ·  검출 0건", bg=STATUS_BG,
                                fg=TEXT_SUB, font=FONT_SUB, anchor="center")
        self.sb_page.pack(side="left", expand=True)

    def _refresh_status(self):
        name = os.path.basename(self.current_path) if self.current_path else "파일 없음"
        self.sb_file.config(text=_ell(name, 40))
        if self.page_count:
            self.sb_page.config(text="페이지 %d / %d  ·  검출 %d건"
                                % (self.cur_page + 1, self.page_count, len(self.findings)))
        else:
            self.sb_page.config(text="- / -  ·  검출 0건")

    def _set_state(self, text):
        self.sb_status.config(text=text)

    # ------------------------------------------------------ card helpers
    def _card(self, parent, title, expand=False):
        shell = tk.Frame(parent, bg=CARD_BG, highlightbackground=BORDER,
                         highlightcolor=BORDER, highlightthickness=1, bd=0)
        shell.pack(fill="both" if expand else "x", expand=expand,
                   pady=(0, 2))
        if title:
            tk.Label(shell, text=title, bg=CARD_BG, fg=NAVY, font=FONT_BOLD,
                     anchor="w").pack(fill="x", padx=8, pady=(3, 1))
        body = tk.Frame(shell, bg=CARD_BG)
        body.pack(fill="both", expand=True, padx=8,
                  pady=(3 if not title else 0, 3))
        return body

    def _radio(self, parent, text, var, value):
        return tk.Radiobutton(
            parent, text=text, variable=var, value=value, bg=CARD_BG,
            fg=TEXT, activebackground=CARD_BG, activeforeground=TEXT,
            selectcolor=CARD_BG, font=FONT, anchor="w",
            highlightthickness=0, bd=0, pady=0, cursor="hand2",
        )

    def _check(self, parent, text, var):
        return tk.Checkbutton(
            parent, text=text, variable=var, bg=CARD_BG, fg=TEXT,
            activebackground=CARD_BG, activeforeground=TEXT,
            selectcolor=CARD_BG, font=FONT, anchor="w",
            highlightthickness=0, bd=0, pady=0, cursor="hand2",
        )

    def _grouprow(self, parent, title, caption=None):
        """One dense row: bold group label left, gray caption right."""
        r = tk.Frame(parent, bg=CARD_BG)
        r.pack(fill="x")
        tk.Label(r, text=title, bg=CARD_BG, fg=NAVY, font=FONT_GROUP,
                 anchor="w").pack(side="left")
        if caption:
            tk.Label(r, text=caption, bg=CARD_BG, fg=TEXT_SUB, font=FONT_SUB,
                     anchor="e").pack(side="right")

    # ------------------------------------------------------------ left
    def _build_left(self, parent):
        # -- 대상 파일 (add/remove buttons beside the list to save a row) --
        c = self._card(parent, "대상 파일")
        fl = tk.Frame(c, bg=CARD_BG)
        fl.pack(fill="x")
        self.file_list = tk.Listbox(fl, height=4, font=FONT,
                                    selectmode="extended", bg=CARD_BG,
                                    fg=TEXT, highlightthickness=1,
                                    highlightbackground=BORDER,
                                    selectbackground=NAVY,
                                    selectforeground="#FFFFFF",
                                    relief="flat", activestyle="none")
        self.file_list.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(fl, command=self.file_list.yview)
        sb.pack(side="left", fill="y")
        self.file_list.config(yscrollcommand=sb.set)
        self.file_list.bind("<<ListboxSelect>>", self._on_file_select)
        fb = tk.Frame(fl, bg=CARD_BG)
        fb.pack(side="left", fill="y", padx=(5, 0))
        ttk.Button(fb, text="파일 추가", style="Compact.TButton",
                   command=self.add_files).pack(fill="x")
        ttk.Button(fb, text="제거", style="Compact.TButton",
                   command=self.remove_files).pack(fill="x", pady=(3, 0))

        # -- 마스킹 옵션 (dense two-column grid) --
        c = self._card(parent, "마스킹 옵션")
        g = tk.Frame(c, bg=CARD_BG)
        g.pack(fill="x")
        g.columnconfigure(1, weight=1)

        def glabel(text, row):
            tk.Label(g, text=text, bg=CARD_BG, fg=NAVY, font=FONT_GROUP,
                     anchor="w").grid(row=row, column=0, sticky="nw",
                                      padx=(0, 6), pady=(2, 0))

        self.var_scope = tk.StringVar(value="back6")
        glabel("마스킹 범위", 0)
        self._radio(g, "주민등록번호 전체", self.var_scope,
                    "full").grid(row=0, column=1, sticky="w")
        self._radio(g, "뒷 6자리만 (예: 900101-2●●●●●●)", self.var_scope,
                    "back6").grid(row=1, column=1, sticky="w")

        self.var_style = tk.StringVar(value="box")
        glabel("마스킹 방식", 2)
        sf = tk.Frame(g, bg=CARD_BG)
        sf.grid(row=2, column=1, sticky="w")
        self._radio(sf, "검은색 박스", self.var_style, "box").pack(side="left")
        self._radio(sf, "모자이크", self.var_style,
                    "mosaic").pack(side="left", padx=(8, 0))

        # OCR on/off moved to the two detect buttons (텍스트/사진 포함 검사).
        self.var_addr = tk.BooleanVar(value=False)
        self.var_png = tk.BooleanVar(value=False)
        cf = tk.Frame(g, bg=CARD_BG)
        cf.grid(row=3, column=0, columnspan=2, sticky="w", pady=(1, 0))
        self._check(cf, "주소도 마스킹", self.var_addr).pack(side="left")
        self._check(cf, "페이지 PNG로도 저장",
                    self.var_png).pack(side="left", padx=(8, 0))

        glabel("출력 폴더", 4)
        of = tk.Frame(g, bg=CARD_BG)
        of.grid(row=4, column=1, sticky="we", pady=(1, 0))
        self.var_outdir = tk.StringVar(value="")
        ttk.Entry(of, textvariable=self.var_outdir).pack(side="left",
                                                         fill="x", expand=True)
        ttk.Button(of, text="찾아보기", style="Compact.TButton",
                   command=self._browse_outdir).pack(side="left", padx=(4, 0))

        tk.Label(g, text="(비우면 원본 폴더\\masked)", bg=CARD_BG,
                 fg=TEXT_SUB, font=FONT_SUB, anchor="w").grid(
                     row=5, column=1, sticky="w")

        # -- 실행 (card title omitted: the group rows label the card;
        #    needed so the whole panel fits a 640px-high window) --
        c = self._card(parent, None)
        # group 1: 자동 마스킹 — detect (text-only / with OCR), then mask.
        self._grouprow(c, "자동 마스킹", "검사 후 체크된 항목을 마스킹")
        dr = tk.Frame(c, bg=CARD_BG)
        dr.pack(fill="x", pady=(1, 0))
        self.btn_detect_text = ttk.Button(dr, text="텍스트 검사 (빠름)",
                                          style="CompactPrimary.TButton",
                                          command=lambda: self.on_detect(ocr=False))
        self.btn_detect_text.pack(side="left", fill="x", expand=True)
        self.btn_detect_ocr = ttk.Button(dr, text="사진 포함 검사 (OCR)",
                                         style="CompactPrimary.TButton",
                                         command=lambda: self.on_detect(ocr=True))
        self.btn_detect_ocr.pack(side="left", fill="x", expand=True, padx=(5, 0))
        self.btn_mask = ttk.Button(c, text="검출 항목 마스킹 저장",
                                   style="Strong.TButton", command=self.on_mask)
        self.btn_mask.pack(fill="x", pady=(2, 0))

        tk.Frame(c, bg=BORDER, height=1).pack(fill="x", pady=2)

        # group 2: 수동 마스킹 — toggle (synced with toolbar) + quick save.
        self._grouprow(c, "수동 마스킹", "검사 없이 지정한 영역만 즉시 마스킹")
        mr = tk.Frame(c, bg=CARD_BG)
        mr.pack(fill="x", pady=(1, 0))
        self.btn_manual_panel = ttk.Button(mr, text="수동 영역 추가: 꺼짐",
                                           style="Compact.TButton",
                                           command=self.toggle_manual)
        self.btn_manual_panel.pack(side="left", fill="x", expand=True)
        self.btn_mask_manual = ttk.Button(mr, text="수동 영역만 저장",
                                          style="CompactPrimary.TButton",
                                          command=self.on_mask_manual)
        self.btn_mask_manual.pack(side="left", fill="x", expand=True, padx=(5, 0))

        tk.Frame(c, bg=BORDER, height=1).pack(fill="x", pady=2)

        # group 3: 기타 — batch (OCR fixed on), extract, edits-only, reset.
        self._grouprow(c, "기타", "일괄 처리는 사진 포함 검사로 동작")
        o1 = tk.Frame(c, bg=CARD_BG)
        o1.pack(fill="x", pady=(1, 0))
        self.btn_batch = ttk.Button(o1, text="전체 파일 일괄 처리",
                                    style="CompactSecondary.TButton",
                                    command=self.on_batch)
        self.btn_batch.pack(side="left", fill="x", expand=True)
        self.btn_extract = ttk.Button(o1, text="페이지 추출",
                                      style="Compact.TButton",
                                      command=self.on_extract)
        self.btn_extract.pack(side="left", fill="x", expand=True, padx=(5, 0))
        o2 = tk.Frame(c, bg=CARD_BG)
        o2.pack(fill="x", pady=(2, 0))
        self.btn_editsave = ttk.Button(o2, text="글자수정만 저장",
                                       style="Compact.TButton",
                                       command=self.on_save_edits)
        self.btn_editsave.pack(side="left", fill="x", expand=True)
        self.btn_reset = ttk.Button(o2, text="초기화 (파일 닫기)",
                                    style="Compact.TButton", command=self.on_reset)
        self.btn_reset.pack(side="left", fill="x", expand=True, padx=(5, 0))

        # -- 진행 상황 (progressbar shares the title row; log expands) --
        c = self._card(parent, None, expand=True)
        tr = tk.Frame(c, bg=CARD_BG)
        tr.pack(fill="x", pady=(0, 2))
        tk.Label(tr, text="진행 상황", bg=CARD_BG, fg=NAVY, font=FONT_BOLD,
                 anchor="w").pack(side="left")
        self.progress = ttk.Progressbar(tr, mode="determinate",
                                        style="Horizontal.TProgressbar")
        self.progress.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.btn_stop = ttk.Button(tr, text="중지", style="Compact.TButton",
                                   command=self.on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(5, 0))
        lf = tk.Frame(c, bg=CARD_BG)
        lf.pack(fill="both", expand=True)
        self.log = tk.Text(lf, height=4, font=FONT_LOG, wrap="word",
                           state="disabled", bg=CARD_BG, fg=TEXT,
                           relief="flat", highlightthickness=1,
                           highlightbackground=BORDER)
        self.log.pack(side="left", fill="both", expand=True)
        lsb = ttk.Scrollbar(lf, command=self.log.yview)
        lsb.pack(side="left", fill="y")
        self.log.config(yscrollcommand=lsb.set)

    # ----------------------------------------------------------- right
    def _build_right(self, parent):
        # Vertical paned window: top = preview, bottom = findings list.
        # (tk.PanedWindow is used because it supports per-pane minsize,
        # which ttk.Panedwindow does not; initial 3:1 ratio set via sash.)
        self.paned = tk.PanedWindow(
            parent, orient="vertical", sashwidth=6, sashrelief="flat",
            opaqueresize=True, background=BORDER,
        )
        self.paned.pack(fill="both", expand=True)

        topf = tk.Frame(self.paned, bg=APP_BG)
        botf = tk.Frame(self.paned, bg=APP_BG)
        self.paned.add(topf, minsize=120, stretch="always")
        self.paned.add(botf, minsize=120, stretch="never")
        self._sash_placed = False
        self.paned.bind("<Configure>", self._init_sash)

        # -- toolbar (grouped) --
        top = tk.Frame(topf, bg=APP_BG)
        top.pack(fill="x", pady=(0, 4))

        g1 = tk.Frame(top, bg=APP_BG); g1.pack(side="left")
        ttk.Button(g1, text="◀", width=3, style="Tool.TButton",
                   command=lambda: self.change_page(-1)).pack(side="left")
        self.lbl_page = tk.Label(g1, text="- / -", width=9, anchor="center",
                                 bg=APP_BG, fg=TEXT, font=FONT)
        self.lbl_page.pack(side="left", padx=1)
        ttk.Button(g1, text="▶", width=3, style="Tool.TButton",
                   command=lambda: self.change_page(1)).pack(side="left")

        self._toolsep(top)
        g2 = tk.Frame(top, bg=APP_BG); g2.pack(side="left")
        self.btn_toggle = ttk.Button(g2, text="결과 보기", style="Tool.TButton",
                                     command=self.toggle_preview, state="disabled")
        self.btn_toggle.pack(side="left")

        self._toolsep(top)
        g3 = tk.Frame(top, bg=APP_BG); g3.pack(side="left")
        self.btn_manual = ttk.Button(g3, text="수동 영역 추가: 꺼짐",
                                     style="Tool.TButton", command=self.toggle_manual)
        self.btn_manual.pack(side="left")
        self.btn_edit = ttk.Button(g3, text="글자 수정: 꺼짐", style="Tool.TButton",
                                   command=self.toggle_edit)
        self.btn_edit.pack(side="left", padx=(4, 0))

        self._toolsep(top)
        g4 = tk.Frame(top, bg=APP_BG); g4.pack(side="left")
        ttk.Button(g4, text="－", width=3, style="Tool.TButton",
                   command=lambda: self.change_zoom(1.0 / ZOOM_STEP)).pack(side="left")
        self.lbl_zoom = tk.Label(g4, text="100%", width=6, anchor="center",
                                 bg=APP_BG, fg=TEXT, font=FONT)
        self.lbl_zoom.pack(side="left", padx=1)
        ttk.Button(g4, text="＋", width=3, style="Tool.TButton",
                   command=lambda: self.change_zoom(ZOOM_STEP)).pack(side="left")
        ttk.Button(g4, text="창맞춤", style="Tool.TButton",
                   command=self.zoom_fit).pack(side="left", padx=(4, 0))

        self._toolsep(top)
        g5 = tk.Frame(top, bg=APP_BG); g5.pack(side="left")
        self.btn_overlay = ttk.Button(g5, text="표시 숨기기", style="Tool.TButton",
                                      command=self.toggle_overlays)
        self.btn_overlay.pack(side="left")
        self.lbl_mode = tk.Label(top, text="원본", bg=APP_BG, fg=TEXT_SUB,
                                 font=FONT)
        self.lbl_mode.pack(side="left", padx=10)

        # -- preview canvas --
        cvf = tk.Frame(topf, bg=APP_BG)
        cvf.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(cvf, background=CANVAS_BG, highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        vs = ttk.Scrollbar(cvf, orient="vertical", command=self.canvas.yview)
        vs.pack(side="left", fill="y")
        hs = ttk.Scrollbar(topf, orient="horizontal", command=self.canvas.xview)
        hs.pack(fill="x")
        self.canvas.config(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.canvas.bind("<Configure>", lambda e: self.render_preview())
        self.canvas.bind("<ButtonPress-1>", self._on_canvas_press)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)
        self.canvas.bind("<Control-MouseWheel>", self._on_ctrl_wheel)
        # Wheel scroll (canvas-local, so the findings tree keeps its own
        # class binding untouched): plain = vertical, Shift = horizontal.
        self.canvas.bind("<MouseWheel>", self._on_wheel_v)
        self.canvas.bind("<Shift-MouseWheel>", self._on_wheel_h)
        # box editing (v1.5): hover cursor + right-click context menu
        self.canvas.bind("<Motion>", self._on_canvas_motion)
        self.canvas.bind("<Button-3>", self._on_canvas_rclick)

        # -- findings table (header row hosts undo / delete controls) --
        th = tk.Frame(botf, bg=APP_BG)
        th.pack(fill="x", pady=(4, 2))
        tk.Label(th, text="검출 목록 (체크 해제 시 제외)", bg=APP_BG, fg=NAVY,
                 font=FONT_BOLD, anchor="w").pack(side="left")
        self.btn_delsel = ttk.Button(th, text="선택 삭제", style="Compact.TButton",
                                     command=self.on_delete_selected)
        self.btn_delsel.pack(side="right")
        self.btn_undo = ttk.Button(th, text="실행 취소", style="Compact.TButton",
                                   command=self.on_undo)
        self.btn_undo.pack(side="right", padx=(0, 5))
        tf = tk.Frame(botf, bg=APP_BG)
        tf.pack(fill="both", expand=True)
        cols = ("chk", "page", "kind", "text", "source")
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", height=8, selectmode="browse")
        for c, t, w in (("chk", "☑", 34), ("page", "페이지", 54),
                        ("kind", "종류", 120), ("text", "내용 미리보기", 240),
                        ("source", "출처", 70)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="center" if c in ("chk", "page", "source") else "w")
        self.tree.tag_configure("odd", background=ROW_ODD)
        self.tree.tag_configure("off", foreground=TEXT_SUB)
        self.tree.pack(side="left", fill="both", expand=True)
        tsb = ttk.Scrollbar(tf, command=self.tree.yview)
        tsb.pack(side="left", fill="y")
        self.tree.config(yscrollcommand=tsb.set)
        self.tree.bind("<ButtonRelease-1>", self._on_tree_click)
        self.tree.bind("<Double-1>", self._on_tree_double)
        self.tree.bind("<Delete>", lambda e: self.on_delete_selected())
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

    def _toolsep(self, parent):
        tk.Frame(parent, bg=BORDER, width=1, height=22).pack(
            side="left", fill="y", padx=6, pady=2)

    def _bind_shortcuts(self):
        # PgUp/PgDn page nav, Ctrl+O add files, Ctrl+Z undo.
        # (Ctrl+wheel zoom stays on the canvas.)
        self.root.bind("<Prior>", lambda e: self.change_page(-1))
        self.root.bind("<Next>", lambda e: self.change_page(1))
        self.root.bind("<Control-o>", lambda e: self.add_files())
        self.root.bind("<Control-O>", lambda e: self.add_files())
        self.root.bind("<Control-z>", self.on_undo)
        self.root.bind("<Control-Z>", self.on_undo)

    def _init_sash(self, _e=None):
        """Place the sash once at a 3:1 preview/list ratio."""
        if self._sash_placed:
            return
        h = self.paned.winfo_height()
        if h > 320:
            try:
                self.paned.sash_place(0, 0, int(h * 0.75))
                self._sash_placed = True
            except Exception:
                pass

    # ------------------------------------------------------- info dialog
    def show_info(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("MarkID 정보")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        dlg.configure(bg=CARD_BG)
        frm = tk.Frame(dlg, bg=CARD_BG)
        frm.pack(fill="both", expand=True, padx=20, pady=18)

        tk.Label(frm, text="MarkID", bg=CARD_BG, fg=NAVY,
                 font=("맑은 고딕", 16, "bold")).pack(anchor="w")
        tk.Label(frm, text="계약서 개인정보 마스킹 도구", bg=CARD_BG,
                 fg=TEXT_SUB, font=FONT).pack(anchor="w", pady=(0, 12))

        grid = tk.Frame(frm, bg=CARD_BG)
        grid.pack(anchor="w")
        rows = (("버전", "v%s" % __version__), ("게시자", "김권수"),
                ("조직", "HRteam"))
        for i, (k, v) in enumerate(rows):
            tk.Label(grid, text=k, bg=CARD_BG, fg=TEXT_SUB, font=FONT,
                     width=6, anchor="w").grid(row=i, column=0, sticky="w", pady=1)
            tk.Label(grid, text=v, bg=CARD_BG, fg=TEXT, font=FONT_BOLD,
                     anchor="w").grid(row=i, column=1, sticky="w", pady=1)

        tk.Frame(frm, bg=BORDER, height=1).pack(fill="x", pady=14)
        tk.Label(frm, text="완전 오프라인 — 어떤 데이터도 PC 밖으로 전송되지 않습니다.",
                 bg=CARD_BG, fg=OK, font=FONT, wraplength=360,
                 justify="left").pack(anchor="w")
        tk.Label(frm, text="오픈소스 고지: THIRD_PARTY_NOTICES.md 참조",
                 bg=CARD_BG, fg=TEXT_SUB, font=FONT_SUB, wraplength=360,
                 justify="left").pack(anchor="w", pady=(6, 0))

        btnrow = tk.Frame(frm, bg=CARD_BG)
        btnrow.pack(anchor="e", pady=(16, 0))
        ttk.Button(btnrow, text="닫기", style="Primary.TButton",
                   command=dlg.destroy).pack()

        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.update_idletasks()
        try:
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
            dlg.geometry("+%d+%d" % (rx + (rw - w) // 2, ry + (rh - h) // 3))
        except Exception:
            pass
        dlg.grab_set()
        dlg.focus_set()

    # ------------------------------------------------------------- logging
    def log_msg(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _set_busy(self, busy):
        self._busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.btn_detect_text, self.btn_detect_ocr, self.btn_mask,
                  self.btn_mask_manual, self.btn_batch, self.btn_extract,
                  self.btn_editsave, self.btn_reset,
                  self.btn_undo, self.btn_delsel):
            b.config(state=state)
        # stop is the inverse: only usable while a worker is running
        self.btn_stop.config(state="normal" if busy else "disabled")

    # --------------------------------------------------------- file list
    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="파일 선택",
            filetypes=[("PDF/이미지", "*.pdf *.png *.jpg *.jpeg *.bmp *.tif *.tiff"),
                       ("모든 파일", "*.*")],
        )
        for p in paths:
            if p not in self.files:
                self.files.append(p)
                self.file_list.insert("end", os.path.basename(p))
        if self.files and self.work_doc is None:
            self.file_list.selection_clear(0, "end")
            self.file_list.selection_set(0)
            self._load_preview(self.files[0])

    def remove_files(self):
        for i in reversed(self.file_list.curselection()):
            del self.files[i]
            self.file_list.delete(i)

    def _on_file_select(self, _e=None):
        sel = self.file_list.curselection()
        if not sel:
            return
        self._load_preview(self.files[sel[0]])

    def _current_file(self):
        sel = self.file_list.curselection()
        if sel:
            return self.files[sel[0]]
        return self.files[0] if self.files else None

    # --------------------------------------------------------- preview
    def _build_work_doc(self, path, edits=None):
        """Fresh open of *path* with the enabled edits applied.

        Thread-safe (no Tk access): shrink warnings go through the queue.
        """
        doc = open_document(path)
        enabled = [e for e in (edits if edits is not None else self.edits) if e.enabled]
        if enabled:
            for wid in apply_edits(doc, enabled):
                self._post("log", text="경고: 글자수정 e%d 텍스트가 크게 축소되었습니다" % wid)
        return doc

    def _load_preview(self, path):
        # Edits and manual regions belong to one file: keep them when
        # re-loading the same file (e.g. before every detection run), clear
        # them — and the undo stack — on a file switch.
        keep_state = path == self.current_path
        self._close_docs()
        if not keep_state:
            self.edits = []
            self.undo_stack = []
        try:
            self.work_doc = self._build_work_doc(path)
        except EncryptedPdfError as e:
            messagebox.showerror("오류", str(e))
            self.log_msg("오류: %s" % e)
            return
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("오류", "파일을 열 수 없습니다: %s" % e)
            return
        self.current_path = path
        self.page_count = self.work_doc.page_count
        self.cur_page = 0
        self.preview_mode = "original"
        self.sel_id = None
        if keep_state:
            # manual regions and user-adjusted boxes survive a re-detect;
            # auto findings are refreshed
            self.findings = [f for f in self.findings
                             if f.kind == "manual" or f.user_adjusted]
        else:
            self.findings = []
        self.result_doc = None
        self.result_path = None
        self.btn_toggle.config(state="disabled", text="결과 보기")
        self._refresh_tree()
        self.render_preview()

    def _merge_findings(self, auto_findings):
        """Merge fresh auto detections with preserved manual/adjusted boxes.

        Preserved findings (manual regions and user-adjusted boxes) keep
        their ids (the undo stack refers to them); auto findings are
        re-numbered above that range.  A fresh auto hit that lands on a
        user-adjusted box of the same kind is dropped — the box the user
        tuned wins over the re-detected one.
        """
        keep = [f for f in self.findings if f.kind == "manual" or f.user_adjusted]
        adjusted = [k for k in keep if k.user_adjusted]
        autos = []
        for f in auto_findings:
            clash = any(
                k.kind == f.kind and k.page == f.page
                and (iou(k.rect, f.rect) >= 0.4
                     or contains(k.rect, f.rect) or contains(f.rect, k.rect))
                for k in adjusted)
            if not clash:
                autos.append(f)
        base = max([f.id for f in keep], default=-1) + 1
        for i, f in enumerate(autos):
            f.id = base + i
        return autos + keep

    def _close_docs(self):
        for d in (self.work_doc, self.result_doc):
            try:
                if d is not None:
                    d.close()
            except Exception:
                pass
        self.work_doc = None
        self.result_doc = None

    def _active_doc(self):
        if self.preview_mode == "result" and self.result_doc is not None:
            return self.result_doc
        return self.work_doc

    def _rebuild_work_doc(self):
        """Rebuild the working copy after an edit was added/toggled."""
        if not self.current_path:
            return
        enabled = [e for e in self.edits if e.enabled]
        if len(enabled) >= 3:
            # several redaction+insert rounds: do it off the UI thread
            self.log_msg("편집 반영 중... (%d건)" % len(enabled))
            snapshot = list(self.edits)
            threading.Thread(
                target=self._rebuild_worker,
                args=(self.current_path, snapshot),
                daemon=True,
            ).start()
            return
        try:
            new_doc = self._build_work_doc(self.current_path)
        except Exception as e:  # noqa: BLE001
            self.log_msg("편집 반영 오류: %s" % e)
            return
        old = self.work_doc
        self.work_doc = new_doc
        try:
            if old is not None:
                old.close()
        except Exception:
            pass
        self.preview_mode = "original"
        self.render_preview()

    def _rebuild_worker(self, path, edits):
        try:
            doc = self._build_work_doc(path, edits)
            self._post("work_rebuilt", doc=doc)
        except Exception as e:  # noqa: BLE001
            self._post("log", text="편집 반영 오류: %s" % e)

    def render_preview(self):
        doc = self._active_doc()
        self.canvas.delete("all")
        if doc is None or self.page_count == 0:
            self.lbl_page.config(text="- / -")
            self._refresh_status()
            return
        self.cur_page = max(0, min(self.cur_page, self.page_count - 1))
        page = doc[self.cur_page]
        cw = max(self.canvas.winfo_width(), 100)
        pw = page.rect.width or 1
        fit_dpi = (max(cw - 4, 200) / pw) * 72.0
        dpi = max(36.0, min(300.0, fit_dpi * self.zoom))
        self.scale = dpi / 72.0
        try:
            pix = page.get_pixmap(dpi=int(dpi), alpha=False)
            self.tkimg = tk.PhotoImage(data=pix.tobytes("ppm"))
        except Exception as e:  # noqa: BLE001
            self.log_msg("미리보기 렌더 오류: %s" % e)
            return
        self.canvas.create_image(0, 0, anchor="nw", image=self.tkimg)
        # simple 1px page border for a "paper on desk" feel
        self.canvas.create_rectangle(0, 0, max(1, pix.width - 1),
                                     max(1, pix.height - 1),
                                     outline=PAGE_EDGE, width=1)
        self.canvas.config(scrollregion=(0, 0, pix.width, pix.height))
        self.lbl_page.config(text="%d / %d" % (self.cur_page + 1, self.page_count))
        self.lbl_zoom.config(text="%d%%" % int(round(self.zoom * 100)))
        self.lbl_mode.config(text="결과" if self.preview_mode == "result" else "원본")
        if self.preview_mode == "original":
            self._draw_overlays()
        self._refresh_status()

    def toggle_overlays(self):
        """Hide/show detection, manual and edit boxes on the preview.

        View-only: findings/edits stay enabled and are still applied.  The
        tree double-click locator highlight is intentionally unaffected.
        """
        self.show_overlays = not self.show_overlays
        self.btn_overlay.config(text="표시 숨기기" if self.show_overlays else "표시 보이기")
        self._style_toggle(self.btn_overlay, not self.show_overlays)
        self.render_preview()

    def _style_toggle(self, btn, on):
        btn.config(style="Toggled.TButton" if on else "Tool.TButton")

    def _draw_overlays(self):
        if not self.show_overlays:
            return
        for f in self.findings:
            if f.page != self.cur_page:
                continue
            x0, y0, x1, y1 = [v * self.scale for v in f.rect]
            if not f.enabled:
                self.canvas.create_rectangle(x0, y0, x1, y1, outline=COLOR["disabled"],
                                             dash=(3, 2), width=1)
            else:
                self.canvas.create_rectangle(x0, y0, x1, y1, outline=COLOR.get(f.kind, COLOR["rrn"]),
                                             width=2)
        for e in self.edits:
            if e.page != self.cur_page:
                continue
            x0, y0, x1, y1 = [v * self.scale for v in e.rect]
            if not e.enabled:
                self.canvas.create_rectangle(x0, y0, x1, y1, outline=COLOR["disabled"],
                                             dash=(3, 2), width=1)
            else:
                self.canvas.create_rectangle(x0, y0, x1, y1, outline=COLOR["edit"],
                                             width=2)
        # selection emphasis: thick border + 8 resize handles (v1.5)
        f = self._find_by_id(self.sel_id) if self.sel_id is not None else None
        if f is not None and f.page == self.cur_page:
            sr = tuple(v * self.scale for v in f.rect)
            self.canvas.create_rectangle(*sr, outline=NAVY, width=3)
            for hx, hy in self._handle_points(sr).values():
                self.canvas.create_rectangle(hx - 3, hy - 3, hx + 3, hy + 3,
                                             fill=NAVY, outline="#FFFFFF")

    # --------------------------------------------------------- zoom
    def change_zoom(self, factor):
        self.zoom = max(ZOOM_MIN, min(ZOOM_MAX, self.zoom * factor))
        self.render_preview()

    def zoom_fit(self):
        self.zoom = 1.0
        self.render_preview()

    def _on_ctrl_wheel(self, e):
        self.change_zoom(ZOOM_STEP if e.delta > 0 else 1.0 / ZOOM_STEP)
        return "break"

    def _on_wheel_v(self, e):
        self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
        return "break"

    def _on_wheel_h(self, e):
        self.canvas.xview_scroll(-1 if e.delta > 0 else 1, "units")
        return "break"

    def change_page(self, delta):
        self.cur_page += delta
        self.sel_id = None            # selection is per page view
        self._box_drag = None
        self.render_preview()

    def toggle_preview(self):
        if self.result_doc is None:
            return
        self.preview_mode = "result" if self.preview_mode == "original" else "original"
        self.btn_toggle.config(text="원본 보기" if self.preview_mode == "result" else "결과 보기")
        self.render_preview()

    # --------------------------------------------------- drag modes (manual/edit)
    def _ensure_original_view(self, why):
        """Drag modes work on the original view: switch back if needed."""
        if self.preview_mode == "result":
            self.preview_mode = "original"
            if self.result_doc is not None:
                self.btn_toggle.config(text="결과 보기")
            self.render_preview()
            self.log_msg("%s은 원본 보기에서만 가능해 원본 보기로 전환했습니다." % why)

    def toggle_manual(self):
        self.manual_mode = not self.manual_mode
        if self.manual_mode:
            self.edit_mode = False
            self._select_finding(None)     # drag modes take over the canvas
            self._ensure_original_view("수동 영역 추가")
        self._update_mode_buttons()

    def toggle_edit(self):
        self.edit_mode = not self.edit_mode
        if self.edit_mode:
            self.manual_mode = False
            self._select_finding(None)
            self._ensure_original_view("글자 수정")
        self._update_mode_buttons()

    def _update_mode_buttons(self):
        manual_text = "수동 영역 추가: %s" % ("켜짐" if self.manual_mode else "꺼짐")
        self.btn_manual.config(text=manual_text)
        self.btn_edit.config(text="글자 수정: %s" % ("켜짐" if self.edit_mode else "꺼짐"))
        self._style_toggle(self.btn_manual, self.manual_mode)
        self._style_toggle(self.btn_edit, self.edit_mode)
        # left-panel twin stays in sync (same state variable)
        self.btn_manual_panel.config(
            text=manual_text,
            style="CompactToggled.TButton" if self.manual_mode else "Compact.TButton")

    def _on_canvas_press(self, e):
        if self.manual_mode or self.edit_mode:
            self._drag_start = (self.canvas.canvasx(e.x), self.canvas.canvasy(e.y))
            color = COLOR["edit"] if self.edit_mode else COLOR["manual"]
            self._drag_rect = self.canvas.create_rectangle(
                self._drag_start[0], self._drag_start[1], self._drag_start[0], self._drag_start[1],
                outline=color, width=2)
            return
        # --- box selection / adjustment (idle, original view only) ---
        if self._busy or self.preview_mode != "original" or self.work_doc is None:
            return
        cx = self.canvas.canvasx(e.x)
        cy = self.canvas.canvasy(e.y)
        f = self._find_by_id(self.sel_id) if self.sel_id is not None else None
        if f is not None and f.page == self.cur_page:
            sr = tuple(v * self.scale for v in f.rect)
            handle = self._handle_at(sr, cx, cy)
            if handle is not None:
                self._box_drag = {"mode": handle, "start": (cx, cy),
                                  "orig": tuple(f.rect), "moved": False}
                return
            if sr[0] <= cx <= sr[2] and sr[1] <= cy <= sr[3]:
                self._box_drag = {"mode": "move", "start": (cx, cy),
                                  "orig": tuple(f.rect), "moved": False}
                return
        hit = self._finding_at(cx / self.scale, cy / self.scale)
        if hit is not None:
            self._select_finding(hit.id)
            # arm an immediate move so click-and-drag works in one gesture
            self._box_drag = {"mode": "move", "start": (cx, cy),
                              "orig": tuple(hit.rect), "moved": False}
        else:
            self._select_finding(None)

    def _on_canvas_drag(self, e):
        if self._box_drag is not None:
            cx = self.canvas.canvasx(e.x)
            cy = self.canvas.canvasy(e.y)
            d = self._box_drag
            if abs(cx - d["start"][0]) + abs(cy - d["start"][1]) > 3:
                d["moved"] = True
            if not d["moved"]:
                return
            nr = self._adjusted_rect(cx, cy)
            if self._box_prev_item is not None:
                self.canvas.delete(self._box_prev_item)
            sr = tuple(v * self.scale for v in nr)
            self._box_prev_item = self.canvas.create_rectangle(
                *sr, outline=NAVY, width=2, dash=(4, 2))
            return
        if not (self.manual_mode or self.edit_mode) or self._drag_start is None:
            return
        x = self.canvas.canvasx(e.x)
        y = self.canvas.canvasy(e.y)
        self.canvas.coords(self._drag_rect, self._drag_start[0], self._drag_start[1], x, y)

    def _on_canvas_release(self, e):
        if self._box_drag is not None:
            cx = self.canvas.canvasx(e.x)
            cy = self.canvas.canvasy(e.y)
            d = self._box_drag
            nr = self._adjusted_rect(cx, cy)
            self._box_drag = None
            if self._box_prev_item is not None:
                self.canvas.delete(self._box_prev_item)
                self._box_prev_item = None
            if not d["moved"]:
                self.render_preview()      # plain click: selection only
                return
            f = self._find_by_id(self.sel_id)
            if f is None:
                self.render_preview()
                return
            self.undo_stack.append(("adjust", (f.id, tuple(f.rect),
                                               f.rect_back6, f.user_adjusted)))
            f.rect = nr
            f.rect_back6 = None            # user intent overrides the estimate
            f.user_adjusted = True
            self.log_msg("박스 조정 (실행 취소 가능)")
            self._refresh_tree()
            if self.tree.exists(str(f.id)):
                self.tree.selection_set(str(f.id))
            self.render_preview()
            return
        if not (self.manual_mode or self.edit_mode) or self._drag_start is None:
            return
        x0, y0 = self._drag_start
        x1 = self.canvas.canvasx(e.x)
        y1 = self.canvas.canvasy(e.y)
        self._drag_start = None
        rx0, rx1 = sorted((x0 / self.scale, x1 / self.scale))
        ry0, ry1 = sorted((y0 / self.scale, y1 / self.scale))
        if rx1 - rx0 < 2 or ry1 - ry0 < 2:
            self.render_preview()
            return
        if self.manual_mode:
            fid = max([f.id for f in self.findings], default=-1) + 1
            self.findings.append(Finding(id=fid, page=self.cur_page, kind="manual",
                                         source="manual", text="수동 지정 영역",
                                         rect=(rx0, ry0, rx1, ry1)))
            self.undo_stack.append(("manual_add", fid))
            self._refresh_tree()
            self.render_preview()
        else:
            self._edit_from_rect((rx0, ry0, rx1, ry1))

    # ----------------------------------------- box selection / adjustment
    _HANDLE_CURSOR = {"nw": "size_nw_se", "se": "size_nw_se",
                      "ne": "size_ne_sw", "sw": "size_ne_sw",
                      "n": "size_ns", "s": "size_ns",
                      "e": "size_we", "w": "size_we"}

    @staticmethod
    def _handle_points(sr):
        """8 handle centres (canvas px) of a scaled rect: corners + edges."""
        x0, y0, x1, y1 = sr
        mx, my = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        return {"nw": (x0, y0), "n": (mx, y0), "ne": (x1, y0),
                "e": (x1, my), "se": (x1, y1), "s": (mx, y1),
                "sw": (x0, y1), "w": (x0, my)}

    def _handle_at(self, sr, cx, cy, r=5):
        """Handle key under canvas point (cx, cy), or None."""
        for key, (hx, hy) in self._handle_points(sr).items():
            if abs(cx - hx) <= r and abs(cy - hy) <= r:
                return key
        return None

    def _finding_at(self, px, py):
        """Smallest-area finding under page point (px, py) on this page."""
        best = None
        best_area = None
        for f in self.findings:
            if f.page != self.cur_page:
                continue
            x0, y0, x1, y1 = f.rect
            if x0 <= px <= x1 and y0 <= py <= y1:
                area = (x1 - x0) * (y1 - y0)
                if best_area is None or area < best_area:
                    best, best_area = f, area
        return best

    def _edit_at(self, px, py):
        """Smallest-area EditOp under page point (px, py) on this page."""
        best = None
        best_area = None
        for op in self.edits:
            if op.page != self.cur_page:
                continue
            x0, y0, x1, y1 = op.rect
            if x0 <= px <= x1 and y0 <= py <= y1:
                area = (x1 - x0) * (y1 - y0)
                if best_area is None or area < best_area:
                    best, best_area = op, area
        return best

    def _select_finding(self, fid):
        """Set the canvas selection and mirror it into the findings tree."""
        if fid == self.sel_id and fid is None:
            return
        self.sel_id = fid
        if fid is not None:
            iid = str(fid)
            if self.tree.exists(iid):
                self.tree.selection_set(iid)
                self.tree.see(iid)
        elif self.tree.selection():
            self.tree.selection_remove(*self.tree.selection())
        self.render_preview()

    def _on_tree_select(self, _e=None):
        """Tree row selection mirrors into the canvas box highlight."""
        sel = self.tree.selection()
        fid = None
        if sel and not sel[0].startswith("e"):
            try:
                fid = int(sel[0])
            except ValueError:
                fid = None
        if fid == self.sel_id:
            return
        self.sel_id = fid
        if self.preview_mode == "original":
            self.render_preview()

    def _adjusted_rect(self, cx, cy):
        """Rect (display pts) for the in-progress move/resize at (cx, cy).

        Enforces a 4x4pt minimum and clamps to the page (display) bounds.
        """
        d = self._box_drag
        ox0, oy0, ox1, oy1 = d["orig"]
        dx = (cx - d["start"][0]) / self.scale
        dy = (cy - d["start"][1]) / self.scale
        page = self.work_doc[self.cur_page]
        pw, ph = page.rect.width, page.rect.height
        m = d["mode"]
        if m == "move":
            w, h = ox1 - ox0, oy1 - oy0
            nx0 = min(max(ox0 + dx, 0.0), max(0.0, pw - w))
            ny0 = min(max(oy0 + dy, 0.0), max(0.0, ph - h))
            return (nx0, ny0, nx0 + w, ny0 + h)
        x0, y0, x1, y1 = ox0, oy0, ox1, oy1
        if "w" in m:
            x0 = min(max(0.0, ox0 + dx), x1 - 4.0)
        if "e" in m:
            x1 = max(min(pw, ox1 + dx), x0 + 4.0)
        if "n" in m:
            y0 = min(max(0.0, oy0 + dy), y1 - 4.0)
        if "s" in m:
            y1 = max(min(ph, oy1 + dy), y0 + 4.0)
        return (x0, y0, x1, y1)

    def _on_canvas_motion(self, e):
        """Cursor feedback over the selected box (idle mode only)."""
        if (self._busy or self.manual_mode or self.edit_mode
                or self.preview_mode != "original" or self._box_drag is not None):
            return
        cur = ""
        f = self._find_by_id(self.sel_id) if self.sel_id is not None else None
        if f is not None and f.page == self.cur_page:
            cx = self.canvas.canvasx(e.x)
            cy = self.canvas.canvasy(e.y)
            sr = tuple(v * self.scale for v in f.rect)
            h = self._handle_at(sr, cx, cy)
            if h is not None:
                cur = self._HANDLE_CURSOR[h]
            elif sr[0] <= cx <= sr[2] and sr[1] <= cy <= sr[3]:
                cur = "fleur"
        try:
            self.canvas.config(cursor=cur)
        except Exception:
            pass

    def _toggle_enabled(self, iid):
        """Flip the enabled state of a finding/edit row (tree + context menu)."""
        if iid.startswith("e"):
            op = self._edit_by_id(int(iid[1:]))
            if op is None:
                return
            op.enabled = not op.enabled
            self.tree.set(iid, "chk", CHECK_ON if op.enabled else CHECK_OFF)
            self._retag_row(iid, op.enabled)
            self._rebuild_work_doc()   # revert/apply in the working copy
            return
        f = self._find_by_id(int(iid))
        if f is not None:
            f.enabled = not f.enabled
            self.tree.set(iid, "chk", CHECK_ON if f.enabled else CHECK_OFF)
            self._retag_row(iid, f.enabled)
            if self.preview_mode == "original":
                self.render_preview()

    def _build_box_menu(self, iid):
        """Context menu for a box: 삭제 / 체크 토글 (selftest builds it too)."""
        if iid.startswith("e"):
            op = self._edit_by_id(int(iid[1:]))
            enabled = op.enabled if op is not None else True
        else:
            f = self._find_by_id(int(iid))
            enabled = f.enabled if f is not None else True

        menu = tk.Menu(self.root, tearoff=0, font=FONT)

        def do_delete():
            if self.tree.exists(iid):
                self.tree.selection_set(iid)
                self.on_delete_selected()

        menu.add_command(label="삭제", command=do_delete)
        menu.add_command(label=("체크 해제" if enabled else "체크"),
                         command=lambda: self._toggle_enabled(iid))
        return menu

    def _on_canvas_rclick(self, e):
        """Right-click on a box: context menu (delete / toggle check)."""
        if (self._busy or self.preview_mode != "original"
                or self.work_doc is None or self.manual_mode or self.edit_mode):
            return
        px = self.canvas.canvasx(e.x) / self.scale
        py = self.canvas.canvasy(e.y) / self.scale
        f = self._finding_at(px, py)
        if f is not None:
            self._select_finding(f.id)
            iid = str(f.id)
        else:
            op = self._edit_at(px, py)
            if op is None:
                return                     # empty area: no menu
            iid = "e%d" % op.id
        menu = self._build_box_menu(iid)
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()

    # --------------------------------------------------------- text edit
    def _edit_from_rect(self, rect):
        if self.work_doc is None or self._busy:
            self.render_preview()
            return
        if self.preview_mode != "original":
            self.log_msg("글자 수정은 원본(작업본) 보기에서만 가능합니다.")
            self.render_preview()
            return
        res = chars_in_rect(self.work_doc[self.cur_page], rect)
        if res is None:
            self.log_msg("선택 영역에 글자가 없습니다. (스캔 이미지 속 글자는 수정할 수 없습니다)")
            self.render_preview()
            return
        text, tight, size, color = res
        self._open_edit_dialog(text, tight, size, color)

    def _open_edit_dialog(self, text, tight, size, color):
        dlg = tk.Toplevel(self.root)
        dlg.title("글자 수정")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        dlg.configure(bg=CARD_BG)
        frm = tk.Frame(dlg, bg=CARD_BG)
        frm.pack(fill="both", expand=True, padx=12, pady=12)
        tk.Label(frm, text="원본 텍스트:", bg=CARD_BG, fg=NAVY,
                 font=FONT_BOLD).grid(row=0, column=0, sticky="w")
        tk.Label(frm, text=text, bg=CARD_BG, fg=TEXT, font=FONT,
                 wraplength=380).grid(row=0, column=1, sticky="w", padx=(6, 0))
        tk.Label(frm, text="새 텍스트:", bg=CARD_BG, fg=NAVY,
                 font=FONT_BOLD).grid(row=1, column=0, sticky="w", pady=(8, 0))
        var = tk.StringVar(value=text)
        ent = ttk.Entry(frm, textvariable=var, width=48)
        ent.grid(row=1, column=1, sticky="we", padx=(6, 0), pady=(8, 0))
        ent.selection_range(0, "end")
        ent.focus_set()

        page = self.cur_page

        def on_apply():
            new_text = var.get()
            dlg.destroy()
            if new_text == text:
                self.log_msg("변경 내용이 없어 취소되었습니다.")
                self.render_preview()
                return
            self._add_edit_op(page, tight, text, new_text, size, color)

        def on_cancel():
            dlg.destroy()
            self.render_preview()

        btns = tk.Frame(frm, bg=CARD_BG)
        btns.grid(row=2, column=0, columnspan=2, pady=(12, 0))
        ttk.Button(btns, text="적용", style="Primary.TButton",
                   command=on_apply).pack(side="left", padx=4)
        ttk.Button(btns, text="취소", command=on_cancel).pack(side="left", padx=4)
        ent.bind("<Return>", lambda _e: on_apply())
        dlg.bind("<Escape>", lambda _e: on_cancel())
        dlg.grab_set()

    def _add_edit_op(self, page, rect, old_text, new_text, size, color):
        eid = max([e.id for e in self.edits], default=-1) + 1
        self.edits.append(EditOp(id=eid, page=page, rect=tuple(rect),
                                 old_text=old_text, new_text=new_text,
                                 size=size, color=color))
        self.undo_stack.append(("edit_add", eid))
        self.log_msg("글자수정 추가: %s → %s" % (_ell(old_text), _ell(new_text)))
        self._refresh_tree()
        self._rebuild_work_doc()

    # ------------------------------------------------ undo / delete
    def on_undo(self, _e=None):
        """Ctrl+Z / [실행 취소]: revert the last manual-add, edit-add or delete."""
        if self._busy:
            return
        w = self.root.focus_get()
        if isinstance(w, (tk.Entry, ttk.Entry, tk.Text)) and w is not self.log:
            return  # don't hijack text-field editing
        if not self.undo_stack:
            self.log_msg("취소할 작업이 없습니다")
            return
        kind, payload = self.undo_stack.pop()
        if kind == "manual_add":
            f = self._find_by_id(payload)
            if f is not None:
                self.findings.remove(f)
            self.log_msg("실행 취소: 수동 영역 추가")
            self._refresh_tree()
            self.render_preview()
        elif kind == "edit_add":
            op = self._edit_by_id(payload)
            if op is not None:
                self.edits.remove(op)
            self.log_msg("실행 취소: 글자수정 추가")
            self._refresh_tree()
            self._rebuild_work_doc()
        elif kind == "delete_finding":
            f = payload
            if self._find_by_id(f.id) is not None:      # id clash after re-detect
                f.id = max([x.id for x in self.findings], default=-1) + 1
            self.findings.append(f)
            self.log_msg("실행 취소: 검출 항목 삭제 복원")
            self._refresh_tree()
            self.render_preview()
        elif kind == "delete_edit":
            op = payload
            if self._edit_by_id(op.id) is not None:
                op.id = max([x.id for x in self.edits], default=-1) + 1
            self.edits.append(op)
            self.log_msg("실행 취소: 글자수정 삭제 복원")
            self._refresh_tree()
            self._rebuild_work_doc()
        elif kind == "adjust":
            fid, old_rect, old_b6, old_flag = payload
            f = self._find_by_id(fid)
            if f is not None:
                f.rect = old_rect
                f.rect_back6 = old_b6
                f.user_adjusted = old_flag
            self.log_msg("실행 취소: 박스 조정 복원")
            self._refresh_tree()
            self.render_preview()

    def on_delete_selected(self):
        """[선택 삭제] / Delete key: remove the selected finding or edit."""
        if self._busy:
            return
        sel = self.tree.selection()
        if not sel:
            self.log_msg("삭제할 항목을 목록에서 선택하세요.")
            return
        rebuild = False
        removed = 0
        for iid in sel:
            if iid.startswith("e"):
                op = self._edit_by_id(int(iid[1:]))
                if op is not None:
                    self.edits.remove(op)
                    self.undo_stack.append(("delete_edit", op))
                    rebuild = True
                    removed += 1
            else:
                f = self._find_by_id(int(iid))
                if f is not None:
                    self.findings.remove(f)
                    self.undo_stack.append(("delete_finding", f))
                    if self.sel_id == f.id:
                        self.sel_id = None
                    removed += 1
        if not removed:
            return
        self.log_msg("항목 %d건 삭제 (실행 취소 가능)" % removed)
        self._refresh_tree()
        if rebuild:
            self._rebuild_work_doc()
        else:
            self.render_preview()

    # --------------------------------------------------------- tree
    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        row = 0
        for f in self.findings:
            tags = []
            if row % 2:
                tags.append("odd")
            if not f.enabled:
                tags.append("off")
            src = SOURCE_LABEL_KO.get(f.source, f.source)
            if f.user_adjusted:
                src += "(조정)"
            self.tree.insert("", "end", iid=str(f.id), tags=tuple(tags), values=(
                CHECK_ON if f.enabled else CHECK_OFF,
                f.page + 1,
                KIND_LABEL_KO.get(f.kind, f.kind),
                f.text,
                src,
            ))
            row += 1
        for e in self.edits:
            tags = []
            if row % 2:
                tags.append("odd")
            if not e.enabled:
                tags.append("off")
            self.tree.insert("", "end", iid="e%d" % e.id, tags=tuple(tags), values=(
                CHECK_ON if e.enabled else CHECK_OFF,
                e.page + 1,
                "글자수정",
                "%s→%s" % (_ell(e.old_text), _ell(e.new_text)),
                "수동",
            ))
            row += 1

    def _find_by_id(self, fid):
        for f in self.findings:
            if f.id == fid:
                return f
        return None

    def _edit_by_id(self, eid):
        for e in self.edits:
            if e.id == eid:
                return e
        return None

    def _on_tree_click(self, e):
        if self._busy:
            return
        row = self.tree.identify_row(e.y)
        if not row:
            return
        col = self.tree.identify_column(e.x)
        if col != "#1":  # checkbox column only
            return
        self._toggle_enabled(row)

    def _retag_row(self, row, enabled):
        tags = [t for t in self.tree.item(row, "tags") if t != "off"]
        if not enabled:
            tags.append("off")
        self.tree.item(row, tags=tuple(tags))

    def _on_tree_double(self, _e):
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        if iid.startswith("e"):
            op = self._edit_by_id(int(iid[1:]))
            if op is None:
                return
            page, rect = op.page, op.rect
        else:
            f = self._find_by_id(int(iid))
            if f is None:
                return
            page, rect = f.page, f.rect
        self.preview_mode = "original"
        self.cur_page = page
        self.render_preview()
        x0, y0, x1, y1 = [v * self.scale for v in rect]
        self.canvas.create_rectangle(x0 - 2, y0 - 2, x1 + 2, y1 + 2, outline="#ff8c00", width=3)
        try:
            self.canvas.yview_moveto(max(0.0, (y0 - 40) / max(1, self.canvas.bbox("all")[3])))
        except Exception:
            pass

    # --------------------------------------------------------- options
    def _opts(self, ocr=True):
        """Options snapshot; *ocr* comes from the pressed button (텍스트/사진),
        not a checkbox.  Masking paths ignore the flag; batch fixes it True."""
        return Options(
            scope=self.var_scope.get(),
            style=self.var_style.get(),
            address=self.var_addr.get(),
            ocr=ocr,
            png=self.var_png.get(),
            outdir=self.var_outdir.get().strip() or None,
        )

    def _browse_outdir(self):
        d = filedialog.askdirectory(title="출력 폴더 선택")
        if d:
            self.var_outdir.set(d)

    # --------------------------------------------------------- worker glue
    def _post(self, kind, **kw):
        kw["kind"] = kind
        self.msg_q.put(kw)

    def _progress_cb(self, stage=None, page=None, total=None, sub=None, **kw):
        if page is None:
            return
        if stage == "text":
            self._post("log", text="  검사 %d/%s 페이지" % (page + 1, total))
        elif stage == "ocr" and (sub in (0, None)):
            self._post("log", text="  OCR %d 페이지" % (page + 1))
        elif stage == "mask":
            self._post("log", text="  마스킹 %d/%s 페이지" % (page + 1, total))
        self._post("tick")

    def _poll_queue(self):
        try:
            while True:
                m = self.msg_q.get_nowait()
                k = m["kind"]
                if k == "log":
                    self.log_msg(m["text"])
                elif k == "tick":
                    self.progress.step(1)
                elif k == "detect_done":
                    self._on_detect_done(m["findings"])
                elif k == "mask_done":
                    self._on_mask_done(m["path"], m["count"], m.get("pngs", []), m.get("verify"))
                elif k == "batch_done":
                    self._on_batch_done(m["done"], m["masked"], m["total"],
                                        m.get("cancelled", False))
                elif k == "editsave_done":
                    self._on_editsave_done(m["path"], m["count"], m.get("pngs", []))
                elif k == "cancelled":
                    self._set_busy(False)
                    self.progress.stop()
                    self.progress.config(mode="determinate", value=0)
                    self._set_state("사용자 중지")
                    self.log_msg("사용자 중지: %s이(가) 취소되었습니다" % m["op"])
                elif k == "work_rebuilt":
                    old = self.work_doc
                    self.work_doc = m["doc"]
                    try:
                        if old is not None:
                            old.close()
                    except Exception:
                        pass
                    self.preview_mode = "original"
                    self.render_preview()
                    self.log_msg("편집 반영 완료")
                elif k == "error":
                    self._set_busy(False)
                    self.progress.stop()
                    self.progress.config(mode="determinate", value=0)
                    self._set_state("오류")
                    messagebox.showerror("오류", m["text"])
                    self.log_msg("오류: %s" % m["text"])
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    # --------------------------------------------------------- detect
    def on_detect(self, ocr=True):
        """[텍스트 검사 (빠름)] / [사진 포함 검사 (OCR)] — *ocr* per button."""
        if self._busy:
            return
        path = self._current_file()
        if not path:
            messagebox.showwarning("알림", "먼저 파일을 추가하세요.")
            return
        self._load_preview(path)   # manual regions/edits survive (same file)
        if self.work_doc is None:
            return
        opts = self._opts(ocr=ocr)
        edits = list(self.edits)
        self.cancel_event.clear()
        self._set_busy(True)
        self.progress.config(mode="indeterminate")
        self.progress.start(20)
        self._set_state("검사 중 (%s)" % ("사진 포함" if ocr else "텍스트"))
        self.log_msg("%s 검사 시작: %s"
                     % ("사진 포함" if ocr else "텍스트", os.path.basename(path)))
        threading.Thread(target=self._detect_worker, args=(path, opts, edits), daemon=True).start()

    def _detect_worker(self, path, opts, edits):
        try:
            # detection runs on the edited working copy (fresh open + edits)
            doc = self._build_work_doc(path, edits)
            try:
                findings = detect(doc, opts, progress=self._progress_cb,
                                  cancel=self.cancel_event)
            finally:
                doc.close()
            self._post("detect_done", findings=findings)
        except OperationCancelled:
            self._post("cancelled", op="검사")
        except EncryptedPdfError as e:
            self._post("error", text=str(e))
        except Exception:  # noqa: BLE001
            self._post("error", text=traceback.format_exc().splitlines()[-1])

    def _on_detect_done(self, findings):
        manual_kept = sum(1 for f in self.findings if f.kind == "manual")
        self.findings = self._merge_findings(findings)
        self._set_busy(False)
        self.progress.stop()
        self.progress.config(mode="determinate", value=0)
        self._refresh_tree()
        self.preview_mode = "original"
        # fresh detections must be visible - re-enable hidden overlays
        self.show_overlays = True
        self.btn_overlay.config(text="표시 숨기기")
        self._style_toggle(self.btn_overlay, False)
        self.render_preview()
        if manual_kept:
            self.log_msg("검사 완료: 자동 %d건 검출 · 수동 %d건 유지"
                         % (len(findings), manual_kept))
        else:
            self.log_msg("검사 완료: %d건 검출" % len(findings))
        self._set_state("검사 완료 · 검출 %d건" % len(self.findings))

    # --------------------------------------------------------- mask
    def on_mask(self):
        """[검출 항목 마스킹 저장] — masks the current list; never auto-detects."""
        if self._busy:
            return
        path = self._current_file()
        if not path:
            messagebox.showwarning("알림", "먼저 파일을 추가하세요.")
            return
        if not self.findings:
            messagebox.showinfo(
                "알림",
                "검출 목록이 비어 있습니다.\n"
                "먼저 자동 검사를 실행하거나 수동 영역을 추가하세요.")
            return
        self._start_mask(path)

    def on_mask_manual(self):
        """빠른 경로: 자동 검사 없이 '수동 지정 영역'만 즉시 마스킹·저장.

        OCR/텍스트 자동 검사를 전혀 실행하지 않는다(자동 검사 fallback 없음).
        수동 영역이 0건이면 저장하지 않고 안내만 표시한다.
        """
        if self._busy:
            return
        path = self._current_file()
        if not path:
            messagebox.showwarning("알림", "먼저 파일을 추가하세요.")
            return
        manual = [f for f in self.findings if f.kind == "manual" and f.enabled]
        if not manual:
            messagebox.showinfo(
                "알림",
                "지정된 수동 영역이 없습니다.\n"
                "미리보기에서 '수동 영역 추가'를 켠 뒤 드래그로 영역을 지정하세요.")
            return
        self._start_mask(path, findings=manual, label="수동 마스킹")

    def _release_result_doc(self):
        """Close the result preview handle.

        The handle points at a ``*_masked.pdf`` that a re-run may overwrite;
        on Windows an open handle locks the file and the save would fail.
        """
        if self.result_doc is not None:
            try:
                self.result_doc.close()
            except Exception:
                pass
            self.result_doc = None
            self.result_path = None
            self.preview_mode = "original"
            self.btn_toggle.config(state="disabled", text="결과 보기")

    def _start_mask(self, path, findings=None, label="마스킹"):
        self._release_result_doc()
        opts = self._opts()
        findings = self.findings if findings is None else findings
        edits = list(self.edits)
        self.cancel_event.clear()
        self._set_busy(True)
        self.progress.config(mode="indeterminate")
        self.progress.start(20)
        self._set_state("%s 중" % label)
        self.log_msg("%s 실행..." % label)
        threading.Thread(target=self._mask_worker, args=(path, opts, findings, edits),
                         daemon=True).start()

    def _mask_worker(self, path, opts, findings, edits):
        try:
            doc = open_document(path)
            try:
                out_path = output_path_for(path, opts)
                # apply_and_save applies the enabled edits first, then the masks
                count, pngs = apply_and_save(doc, findings, opts, out_path,
                                             edits=edits, progress=self._progress_cb,
                                             cancel=self.cancel_event)
            finally:
                doc.close()
            # keep a fresh handle for result preview
            self._result_tmp = fitz.open(out_path)
            # re-verify the saved copy for residual complete RRN text
            try:
                verify = verify_result(self._result_tmp, opts)
            except Exception:  # noqa: BLE001
                verify = None
            self._post("mask_done", path=out_path, count=count, pngs=pngs, verify=verify)
        except OperationCancelled:
            self._post("cancelled", op="마스킹")
        except EncryptedPdfError as e:
            self._post("error", text=str(e))
        except Exception:  # noqa: BLE001
            self._post("error", text=traceback.format_exc().splitlines()[-1])

    def _on_mask_done(self, path, count, pngs, verify=None):
        self.result_doc = getattr(self, "_result_tmp", None)
        self.result_path = path
        self._set_busy(False)
        self.progress.stop()
        self.progress.config(mode="determinate", value=0)
        self.btn_toggle.config(state="normal", text="원본 보기")
        self.preview_mode = "result"
        self.render_preview()
        self.log_msg("완료: %d건 마스킹" % count)
        self.log_msg("저장 위치: %s" % path)
        if pngs:
            self.log_msg("PNG %d장 저장" % len(pngs))

        residual = verify.get("residual", 0) if verify else 0
        pages = verify.get("pages", []) if verify else []
        if verify is None or residual < 0:
            self._set_state("완료 (재검증 미수행)")
            messagebox.showinfo("완료", "%d건 마스킹 완료\n%s" % (count, path))
        elif residual == 0:
            self.log_msg("재검증: 잔여 주민번호 텍스트 0건 — 통과")
            self._set_state("재검증 통과")
            messagebox.showinfo("완료", "%d건 마스킹 완료 (재검증 통과)\n%s" % (count, path))
        else:
            ps = ", ".join("p%d" % p for p in pages)
            self.log_msg("재검증: 잔여 주민번호 형식 %d건 (%s) — 확인 필요" % (residual, ps))
            self._set_state("재검증 경고 · 잔여 %d건" % residual)
            messagebox.showwarning(
                "재검증 경고",
                "%d건 마스킹을 완료했습니다.\n"
                "그러나 일부 페이지(%s)에 주민등록번호 형식이 남아 있습니다.\n"
                "검출 목록에서 체크 상태를 확인하세요.\n\n%s" % (count, ps, path))

    # --------------------------------------------------- save edits only
    def on_save_edits(self):
        """Save a copy with only the enabled text edits applied (no masking)."""
        if self._busy:
            return
        path = self._current_file()
        if not path:
            messagebox.showwarning("알림", "먼저 파일을 추가하세요.")
            return
        enabled = [e for e in self.edits if e.enabled]
        if not enabled:
            messagebox.showinfo("알림", "적용할 글자 수정이 없습니다")
            return
        self._release_result_doc()
        opts = self._opts()
        edits = list(self.edits)
        self._set_busy(True)
        self.progress.config(mode="indeterminate")
        self.progress.start(20)
        self._set_state("글자수정만 저장 중")
        self.log_msg("글자수정만 저장 실행...")
        threading.Thread(target=self._editsave_worker, args=(path, opts, edits),
                         daemon=True).start()

    def _editsave_worker(self, path, opts, edits):
        try:
            count, out_path, pngs = save_edits_only(path, edits, opts)
            self._result_tmp = fitz.open(out_path)
            self._post("editsave_done", path=out_path, count=count, pngs=pngs)
        except EncryptedPdfError as e:
            self._post("error", text=str(e))
        except Exception:  # noqa: BLE001
            self._post("error", text=traceback.format_exc().splitlines()[-1])

    def _on_editsave_done(self, path, count, pngs):
        self.result_doc = getattr(self, "_result_tmp", None)
        self.result_path = path
        self._set_busy(False)
        self.progress.stop()
        self.progress.config(mode="determinate", value=0)
        self.btn_toggle.config(state="normal", text="원본 보기")
        self.preview_mode = "result"
        self.render_preview()
        self._set_state("글자수정만 저장 완료")
        self.log_msg("글자 수정 %d건 저장: %s" % (count, path))
        if pngs:
            self.log_msg("PNG %d장 저장" % len(pngs))
        messagebox.showinfo("완료", "글자 수정 %d건 저장 완료\n%s" % (count, path))

    # --------------------------------------------------------- batch
    def on_batch(self):
        """[전체 파일 일괄 처리] — always detects with OCR (사진 포함 검사 고정)."""
        if self._busy:
            return
        if not self.files:
            messagebox.showwarning("알림", "먼저 파일을 추가하세요.")
            return
        self._release_result_doc()   # batch may overwrite the previewed output
        opts = self._opts(ocr=True)  # fixed: never skip images in batch mode
        files = list(self.files)
        edits_path = self.current_path      # edits belong to this file only
        edits = list(self.edits)
        self.cancel_event.clear()
        self._set_busy(True)
        self.progress.config(mode="indeterminate")
        self.progress.start(20)
        self._set_state("일괄 처리 중")
        self.log_msg("일괄 처리 시작: %d개 파일 (사진 포함 검사)" % len(files))
        threading.Thread(target=self._batch_worker, args=(files, opts, edits_path, edits),
                         daemon=True).start()

    def _batch_worker(self, files, opts, edits_path, edits):
        done = 0
        masked_total = 0
        cancelled = False
        for i, path in enumerate(files):
            self._post("log", text="[%d/%d] %s" % (i + 1, len(files), os.path.basename(path)))
            try:
                file_edits = edits if path == edits_path else None
                rep = run(path, opts, progress=self._progress_cb, edits=file_edits,
                          cancel=self.cancel_event)
            except OperationCancelled:
                self._post("log", text="  사용자 중지 — 남은 파일을 건너뜁니다")
                cancelled = True
                break
            except Exception as e:  # noqa: BLE001 - skip this file, keep going
                self._post("log", text="  건너뜀(오류): %s" % e)
                continue
            done += 1
            masked_total += rep["masked_count"]
            v = rep.get("verify") or {}
            self._post("log", text="  검출 %d건 / 마스킹 %d건 / 재검증 잔여 %s건 -> %s"
                       % (len(rep["findings"]), rep["masked_count"],
                          v.get("residual", 0), rep["output"]))
        self._post("batch_done", done=done, masked=masked_total, total=len(files),
                   cancelled=cancelled)

    def _on_batch_done(self, done, masked, total, cancelled=False):
        self._set_busy(False)
        self.progress.stop()
        self.progress.config(mode="determinate", value=0)
        if cancelled:
            self._set_state("일괄 처리 중지")
            self.log_msg("일괄 처리 중지: %d/%d개 파일 처리됨, %d건 마스킹"
                         % (done, total, masked))
            messagebox.showwarning("중지", "일괄 처리를 중지했습니다.\n%d/%d개 파일 처리, %d건 마스킹"
                                   % (done, total, masked))
            return
        self._set_state("일괄 처리 완료")
        self.log_msg("일괄 처리 완료: %d/%d개 파일, %d건 마스킹" % (done, total, masked))
        messagebox.showinfo("완료", "%d개 파일 완료, %d건 마스킹" % (done, masked))

    # --------------------------------------------------------- stop
    def on_stop(self):
        """[중지] — request cancellation at the next page / OCR-pass boundary."""
        if not self._busy:
            return
        self.cancel_event.set()
        self.btn_stop.config(state="disabled")
        self.log_msg("중지 요청... 페이지/검사 단계 경계에서 멈춥니다")
        self._set_state("중지 요청됨")

    # --------------------------------------------------- page extraction
    def on_extract(self):
        """[페이지 추출] — save selected pages of the current view as a PDF."""
        if self._busy:
            return
        doc = self._active_doc()
        if doc is None or self.page_count == 0:
            messagebox.showwarning("알림", "먼저 파일을 열어 주세요.")
            return
        dlg = self._build_extract_dialog()
        try:
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            dlg.update_idletasks()
            w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
            dlg.geometry("+%d+%d" % (rx + (rw - w) // 2, ry + (rh - h) // 3))
        except Exception:
            pass
        dlg.grab_set()
        dlg.focus_set()

    def _build_extract_dialog(self):
        """Construct the extraction dialog (no grab — selftest builds it too)."""
        doc = self._active_doc()
        total = doc.page_count if doc is not None else 0
        target = ("결과(마스킹본)"
                  if self.preview_mode == "result" and self.result_doc is not None
                  else "원본(글자수정 반영)")

        dlg = tk.Toplevel(self.root)
        dlg.title("페이지 추출")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        dlg.configure(bg=CARD_BG)
        frm = tk.Frame(dlg, bg=CARD_BG)
        frm.pack(fill="both", expand=True, padx=16, pady=14)

        tk.Label(frm, text="대상 문서: 현재 보기 — %s" % target, bg=CARD_BG,
                 fg=NAVY, font=FONT_BOLD, anchor="w").pack(fill="x")
        tk.Label(frm, text="추출할 페이지 (형식: 1,3-5,7 · 전체 %d페이지)" % total,
                 bg=CARD_BG, fg=TEXT_SUB, font=FONT_SUB,
                 anchor="w").pack(fill="x", pady=(6, 2))

        row = tk.Frame(frm, bg=CARD_BG)
        row.pack(fill="x")
        var = tk.StringVar(value=str(self.cur_page + 1) if total else "")
        ent = ttk.Entry(row, textvariable=var, width=24)
        ent.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="현재 페이지", style="Compact.TButton",
                   command=lambda: var.set(str(self.cur_page + 1))
                   ).pack(side="left", padx=(6, 0))
        ent.focus_set()
        ent.selection_range(0, "end")

        def on_save():
            try:
                pages = parse_page_ranges(var.get(), total)
            except ValueError as ex:
                messagebox.showerror("오류", str(ex), parent=dlg)
                return
            base = os.path.splitext(
                os.path.basename(self.current_path or "output"))[0]
            safe = "".join(ch for ch in var.get() if ch in "0123456789,-")
            path = filedialog.asksaveasfilename(
                parent=dlg, title="페이지 추출 저장",
                defaultextension=".pdf", filetypes=[("PDF", "*.pdf")],
                initialfile="%s_p%s.pdf" % (base, safe or "pages"))
            if not path:
                return
            try:
                newdoc = extract_pages(doc, pages)
                try:
                    newdoc.save(path, garbage=3, deflate=True)
                finally:
                    newdoc.close()
            except Exception as ex:  # noqa: BLE001
                messagebox.showerror("오류", "페이지 추출 실패: %s" % ex, parent=dlg)
                return
            dlg.destroy()
            self.log_msg("페이지 추출: %d페이지 → %s" % (len(pages), path))
            self._set_state("페이지 추출 완료")

        btns = tk.Frame(frm, bg=CARD_BG)
        btns.pack(anchor="e", pady=(12, 0))
        ttk.Button(btns, text="저장", style="Primary.TButton",
                   command=on_save).pack(side="left", padx=(0, 5))
        ttk.Button(btns, text="취소", command=dlg.destroy).pack(side="left")
        ent.bind("<Return>", lambda _e: on_save())
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        return dlg

    # --------------------------------------------------------- reset
    def on_reset(self):
        """Close every document handle and clear state (files become movable).

        Removes only the current file from the list; remaining files stay
        listed but nothing is auto-loaded until the user selects one.
        """
        if self._busy:
            return
        sel = self.file_list.curselection()
        idx = sel[0] if sel else (0 if self.files else None)

        self._release_result_doc()
        self._close_docs()
        self.findings = []
        self.edits = []
        self.current_path = None
        self.page_count = 0
        self.cur_page = 0
        self.preview_mode = "original"
        self.zoom = 1.0
        self.tkimg = None
        self.undo_stack = []
        self.sel_id = None
        self._box_drag = None
        self._refresh_tree()
        self.canvas.delete("all")
        self.lbl_page.config(text="- / -")
        self.btn_toggle.config(state="disabled", text="결과 보기")

        if idx is not None and idx < len(self.files):
            del self.files[idx]
            self.file_list.delete(idx)
        self.file_list.selection_clear(0, "end")
        self._refresh_status()
        self._set_state("대기")
        self.log_msg("초기화 완료")


# --------------------------------------------------------------------------
# module entry points
# --------------------------------------------------------------------------

def launch():
    root = tk.Tk()
    MarkIDApp(root)
    root.mainloop()


def selftest():
    """Construct the full GUI, lay it out, then destroy — no mainloop.

    Used by ``--selftest`` to verify widget construction (exit 0 on success).
    Covers the header bar, status bar and the new action buttons, and asserts
    the info-dialog builder exists (without opening a modal).
    """
    root = tk.Tk()
    try:
        app = MarkIDApp(root)
        root.update_idletasks()
        # exercise a couple of no-doc code paths that must stay exception-safe
        app.render_preview()
        app._refresh_tree()
        app._refresh_status()
        # v1.4 widgets / entry points must exist
        assert hasattr(app, "hdr"), "header bar missing"
        assert hasattr(app, "sb_file") and hasattr(app, "sb_page") \
            and hasattr(app, "sb_status"), "status bar missing"
        for name in ("btn_detect_text", "btn_detect_ocr", "btn_mask",
                     "btn_manual_panel", "btn_mask_manual", "btn_batch",
                     "btn_extract", "btn_editsave", "btn_reset",
                     "btn_stop", "btn_undo", "btn_delsel"):
            assert hasattr(app, name), "%s missing" % name
        assert str(app.btn_stop["state"]) == "disabled", "stop must start disabled"
        for name in ("show_info", "on_mask_manual", "on_stop", "on_undo",
                     "on_delete_selected", "on_extract"):
            assert callable(getattr(app, name, None)), "%s handler missing" % name
        assert hasattr(app, "cancel_event"), "cancel event missing"
        # v1.5 box editing: selection state + helpers + context menu path
        assert app.sel_id is None and app._box_drag is None, "selection must start empty"
        for name in ("_finding_at", "_edit_at", "_handle_at", "_handle_points",
                     "_adjusted_rect", "_select_finding", "_toggle_enabled",
                     "_build_box_menu", "_on_canvas_rclick", "_on_canvas_motion"):
            assert callable(getattr(app, name, None)), "%s helper missing" % name
        app.findings.append(Finding(id=0, page=0, kind="manual", source="manual",
                                    text="검사용", rect=(10, 10, 60, 30)))
        app._refresh_tree()
        menu = app._build_box_menu("0")
        assert menu.index("end") == 1, "context menu must have 2 entries"
        menu.destroy()
        app.findings.clear()
        app._refresh_tree()
        # extraction dialog must construct without a document
        dlg = app._build_extract_dialog()
        dlg.destroy()
        # undo on empty stack must be a safe no-op (logs a notice)
        app.on_undo()
    finally:
        root.destroy()
