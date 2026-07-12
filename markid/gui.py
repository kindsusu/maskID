# -*- coding: utf-8 -*-
"""Tkinter GUI (all user-facing text in Korean).

Layout follows SPEC 7: a left control panel (file list + options + actions),
and a right area with a preview canvas (original / result toggle, page nav,
detection overlays) above a findings table with per-row checkboxes.

Detection and masking run on a background thread; the worker communicates with
the Tk main loop through a thread-safe queue polled via ``after``.
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
    Options,
    apply_and_save,
    detect,
    open_document,
    output_path_for,
    run,
    save_edits_only,
)
from .findings import KIND_LABEL_KO, SOURCE_LABEL_KO, Finding

FONT = ("맑은 고딕", 9)
FONT_BOLD = ("맑은 고딕", 9, "bold")

COLOR = {
    "rrn": "#e11d1d",
    "rrn_partial": "#e11d1d",
    "address": "#1d5fe1",
    "manual": "#1a9e3a",
    "edit": "#e58f00",
    "disabled": "#9aa0a6",
}

ZOOM_MIN = 0.25
ZOOM_MAX = 4.0
ZOOM_STEP = 1.2


def _ell(s, n=12):
    return s if len(s) <= n else s[:n] + "…"
CHECK_ON = "☑"   # ☑
CHECK_OFF = "☐"  # ☐


class MarkIDApp:
    def __init__(self, root):
        self.root = root
        root.title("MarkID - 계약서 주민등록번호 마스킹 도구 v%s" % __version__)
        root.geometry("1280x840")
        root.minsize(1100, 700)

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
        self._pending_mask = False   # set when ② was pressed with no findings
        self.msg_q = queue.Queue()

        self._build()
        self.root.after(100, self._poll_queue)

    # ------------------------------------------------------------------ UI
    def _build(self):
        style = ttk.Style()
        try:
            style.configure(".", font=FONT)
            style.configure("Treeview", font=FONT, rowheight=22)
            style.configure("Treeview.Heading", font=FONT_BOLD)
        except Exception:
            pass

        outer = ttk.Frame(self.root, padding=6)
        outer.pack(fill="both", expand=True)

        left = ttk.Frame(outer, width=340)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        right = ttk.Frame(outer)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))

        self._build_left(left)
        self._build_right(right)

    def _build_left(self, parent):
        # -- file list --
        ttk.Label(parent, text="대상 파일", font=FONT_BOLD).pack(anchor="w")
        fl = ttk.Frame(parent)
        fl.pack(fill="x", pady=(2, 4))
        self.file_list = tk.Listbox(fl, height=6, font=FONT, selectmode="extended")
        self.file_list.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(fl, command=self.file_list.yview)
        sb.pack(side="left", fill="y")
        self.file_list.config(yscrollcommand=sb.set)
        self.file_list.bind("<<ListboxSelect>>", self._on_file_select)
        fb = ttk.Frame(parent)
        fb.pack(fill="x")
        ttk.Button(fb, text="파일 추가", command=self.add_files).pack(side="left")
        ttk.Button(fb, text="제거", command=self.remove_files).pack(side="left", padx=4)

        ttk.Separator(parent).pack(fill="x", pady=6)

        # -- options --
        ttk.Label(parent, text="마스킹 범위", font=FONT_BOLD).pack(anchor="w")
        self.var_scope = tk.StringVar(value="back6")
        ttk.Radiobutton(parent, text="주민등록번호 전체", variable=self.var_scope,
                        value="full").pack(anchor="w")
        ttk.Radiobutton(parent, text="뒷 6자리만 (예: 900101-2●●●●●●)",
                        variable=self.var_scope, value="back6").pack(anchor="w")

        self.var_addr = tk.BooleanVar(value=False)
        self.var_ocr = tk.BooleanVar(value=True)
        ttk.Checkbutton(parent, text="주소도 마스킹", variable=self.var_addr).pack(anchor="w", pady=(6, 0))
        ttk.Checkbutton(parent, text="이미지 OCR 검사 (신분증 사진 등)",
                        variable=self.var_ocr).pack(anchor="w")

        ttk.Label(parent, text="마스킹 방식", font=FONT_BOLD).pack(anchor="w", pady=(6, 0))
        self.var_style = tk.StringVar(value="box")
        ttk.Radiobutton(parent, text="검은색 박스", variable=self.var_style, value="box").pack(anchor="w")
        ttk.Radiobutton(parent, text="모자이크", variable=self.var_style, value="mosaic").pack(anchor="w")

        ttk.Label(parent, text="출력 폴더", font=FONT_BOLD).pack(anchor="w", pady=(6, 0))
        of = ttk.Frame(parent)
        of.pack(fill="x")
        self.var_outdir = tk.StringVar(value="")
        ttk.Entry(of, textvariable=self.var_outdir).pack(side="left", fill="x", expand=True)
        ttk.Button(of, text="찾아보기", command=self._browse_outdir).pack(side="left", padx=4)
        ttk.Label(parent, text="(비우면 원본 폴더\\masked)", foreground="#666").pack(anchor="w")

        self.var_png = tk.BooleanVar(value=False)
        ttk.Checkbutton(parent, text="페이지 PNG로도 저장", variable=self.var_png).pack(anchor="w", pady=(2, 0))

        ttk.Separator(parent).pack(fill="x", pady=6)

        # -- actions --
        self.btn_detect = ttk.Button(parent, text="① 검사 (미리보기)", command=self.on_detect)
        self.btn_detect.pack(fill="x")
        self.btn_mask = ttk.Button(parent, text="② 마스킹 실행", command=self.on_mask)
        self.btn_mask.pack(fill="x", pady=4)
        self.btn_editsave = ttk.Button(parent, text="수정만 저장 (마스킹 없음)",
                                       command=self.on_save_edits)
        self.btn_editsave.pack(fill="x", pady=(0, 4))
        self.btn_batch = ttk.Button(parent, text="③ 전체 파일 일괄 처리", command=self.on_batch)
        self.btn_batch.pack(fill="x")
        self.btn_reset = ttk.Button(parent, text="초기화 (파일 닫기)", command=self.on_reset)
        self.btn_reset.pack(fill="x", pady=(4, 0))

        self.progress = ttk.Progressbar(parent, mode="determinate")
        self.progress.pack(fill="x", pady=(4, 2))

        ttk.Label(parent, text="진행 상황", font=FONT_BOLD).pack(anchor="w")
        lf = ttk.Frame(parent)
        lf.pack(fill="both", expand=True)
        self.log = tk.Text(lf, height=8, font=("맑은 고딕", 8), wrap="word", state="disabled")
        self.log.pack(side="left", fill="both", expand=True)
        lsb = ttk.Scrollbar(lf, command=self.log.yview)
        lsb.pack(side="left", fill="y")
        self.log.config(yscrollcommand=lsb.set)

    def _build_right(self, parent):
        # Vertical paned window: top = preview, bottom = findings list.
        # (tk.PanedWindow is used because it supports per-pane minsize,
        # which ttk.Panedwindow does not; initial 3:1 ratio set via sash.)
        self.paned = tk.PanedWindow(
            parent, orient="vertical", sashwidth=6, sashrelief="raised",
            opaqueresize=True, background="#d0d0d0",
        )
        self.paned.pack(fill="both", expand=True)

        topf = ttk.Frame(self.paned)
        botf = ttk.Frame(self.paned)
        self.paned.add(topf, minsize=120, stretch="always")
        self.paned.add(botf, minsize=120, stretch="never")
        self._sash_placed = False
        self.paned.bind("<Configure>", self._init_sash)

        # -- toolbar --
        top = ttk.Frame(topf)
        top.pack(fill="x")
        ttk.Button(top, text="◀", width=3, command=lambda: self.change_page(-1)).pack(side="left")
        self.lbl_page = ttk.Label(top, text="- / -", width=10, anchor="center")
        self.lbl_page.pack(side="left")
        ttk.Button(top, text="▶", width=3, command=lambda: self.change_page(1)).pack(side="left")
        self.btn_toggle = ttk.Button(top, text="결과 보기", command=self.toggle_preview, state="disabled")
        self.btn_toggle.pack(side="left", padx=8)
        self.btn_manual = ttk.Button(top, text="수동 영역 추가: 꺼짐", command=self.toggle_manual)
        self.btn_manual.pack(side="left")
        self.btn_edit = ttk.Button(top, text="글자 수정: 꺼짐", command=self.toggle_edit)
        self.btn_edit.pack(side="left", padx=(4, 0))
        ttk.Button(top, text="－", width=3, command=lambda: self.change_zoom(1.0 / ZOOM_STEP)).pack(side="left", padx=(10, 0))
        self.lbl_zoom = ttk.Label(top, text="100%", width=6, anchor="center")
        self.lbl_zoom.pack(side="left")
        ttk.Button(top, text="＋", width=3, command=lambda: self.change_zoom(ZOOM_STEP)).pack(side="left")
        ttk.Button(top, text="창맞춤", command=self.zoom_fit).pack(side="left", padx=(2, 0))
        self.btn_overlay = ttk.Button(top, text="표시 숨기기", command=self.toggle_overlays)
        self.btn_overlay.pack(side="left", padx=(6, 0))
        self.lbl_mode = ttk.Label(top, text="원본", foreground="#333")
        self.lbl_mode.pack(side="left", padx=8)

        # -- preview canvas --
        cvf = ttk.Frame(topf)
        cvf.pack(fill="both", expand=True, pady=4)
        self.canvas = tk.Canvas(cvf, background="#5a5a5a", highlightthickness=0)
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

        # -- findings table --
        ttk.Label(botf, text="검출 목록 (체크 해제 시 제외)", font=FONT_BOLD).pack(anchor="w")
        tf = ttk.Frame(botf)
        tf.pack(fill="both", expand=True)
        cols = ("chk", "page", "kind", "text", "source")
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", height=8, selectmode="browse")
        for c, t, w in (("chk", "☑", 34), ("page", "페이지", 54),
                        ("kind", "종류", 120), ("text", "내용 미리보기", 240),
                        ("source", "출처", 70)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="center" if c in ("chk", "page", "source") else "w")
        self.tree.pack(side="left", fill="both", expand=True)
        tsb = ttk.Scrollbar(tf, command=self.tree.yview)
        tsb.pack(side="left", fill="y")
        self.tree.config(yscrollcommand=tsb.set)
        self.tree.bind("<ButtonRelease-1>", self._on_tree_click)
        self.tree.bind("<Double-1>", self._on_tree_double)

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

    # ------------------------------------------------------------- logging
    def log_msg(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _set_busy(self, busy):
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.btn_detect.config(state=state)
        self.btn_mask.config(state=state)
        self.btn_editsave.config(state=state)
        self.btn_batch.config(state=state)
        self.btn_reset.config(state=state)

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
        # Edits belong to one file: keep them when re-loading the same file
        # (e.g. before every detection run), clear them on a file switch.
        keep_edits = path == self.current_path
        self._close_docs()
        if not keep_edits:
            self.edits = []
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
        self.findings = []
        self.result_doc = None
        self.result_path = None
        self.btn_toggle.config(state="disabled", text="결과 보기")
        self._refresh_tree()
        self.render_preview()

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
        self.canvas.config(scrollregion=(0, 0, pix.width, pix.height))
        self.lbl_page.config(text="%d / %d" % (self.cur_page + 1, self.page_count))
        self.lbl_zoom.config(text="%d%%" % int(round(self.zoom * 100)))
        self.lbl_mode.config(text="결과" if self.preview_mode == "result" else "원본")
        if self.preview_mode == "original":
            self._draw_overlays()

    def toggle_overlays(self):
        """Hide/show detection, manual and edit boxes on the preview.

        View-only: findings/edits stay enabled and are still applied.  The
        tree double-click locator highlight is intentionally unaffected.
        """
        self.show_overlays = not self.show_overlays
        self.btn_overlay.config(text="표시 숨기기" if self.show_overlays else "표시 보이기")
        self.render_preview()

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
                self.canvas.create_rectangle(x0, y0, x1, y1, outline=COLOR.get(f.kind, "#e11d1d"),
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

    def change_page(self, delta):
        self.cur_page += delta
        self.render_preview()

    def toggle_preview(self):
        if self.result_doc is None:
            return
        self.preview_mode = "result" if self.preview_mode == "original" else "original"
        self.btn_toggle.config(text="원본 보기" if self.preview_mode == "result" else "결과 보기")
        self.render_preview()

    # --------------------------------------------------- drag modes (manual/edit)
    def toggle_manual(self):
        self.manual_mode = not self.manual_mode
        if self.manual_mode:
            self.edit_mode = False
        self._update_mode_buttons()

    def toggle_edit(self):
        self.edit_mode = not self.edit_mode
        if self.edit_mode:
            self.manual_mode = False
        self._update_mode_buttons()

    def _update_mode_buttons(self):
        self.btn_manual.config(text="수동 영역 추가: %s" % ("켜짐" if self.manual_mode else "꺼짐"))
        self.btn_edit.config(text="글자 수정: %s" % ("켜짐" if self.edit_mode else "꺼짐"))

    def _on_canvas_press(self, e):
        if not (self.manual_mode or self.edit_mode):
            return
        self._drag_start = (self.canvas.canvasx(e.x), self.canvas.canvasy(e.y))
        color = COLOR["edit"] if self.edit_mode else COLOR["manual"]
        self._drag_rect = self.canvas.create_rectangle(
            self._drag_start[0], self._drag_start[1], self._drag_start[0], self._drag_start[1],
            outline=color, width=2)

    def _on_canvas_drag(self, e):
        if not (self.manual_mode or self.edit_mode) or self._drag_start is None:
            return
        x = self.canvas.canvasx(e.x)
        y = self.canvas.canvasy(e.y)
        self.canvas.coords(self._drag_rect, self._drag_start[0], self._drag_start[1], x, y)

    def _on_canvas_release(self, e):
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
            self._refresh_tree()
            self.render_preview()
        else:
            self._edit_from_rect((rx0, ry0, rx1, ry1))

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
        frm = ttk.Frame(dlg, padding=10)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="원본 텍스트:", font=FONT_BOLD).grid(row=0, column=0, sticky="w")
        ttk.Label(frm, text=text, wraplength=380).grid(row=0, column=1, sticky="w", padx=(6, 0))
        ttk.Label(frm, text="새 텍스트:", font=FONT_BOLD).grid(row=1, column=0, sticky="w", pady=(8, 0))
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

        btns = ttk.Frame(frm)
        btns.grid(row=2, column=0, columnspan=2, pady=(12, 0))
        ttk.Button(btns, text="적용", command=on_apply).pack(side="left", padx=4)
        ttk.Button(btns, text="취소", command=on_cancel).pack(side="left", padx=4)
        ent.bind("<Return>", lambda _e: on_apply())
        dlg.bind("<Escape>", lambda _e: on_cancel())
        dlg.grab_set()

    def _add_edit_op(self, page, rect, old_text, new_text, size, color):
        eid = max([e.id for e in self.edits], default=-1) + 1
        self.edits.append(EditOp(id=eid, page=page, rect=tuple(rect),
                                 old_text=old_text, new_text=new_text,
                                 size=size, color=color))
        self.log_msg("글자수정 추가: %s → %s" % (_ell(old_text), _ell(new_text)))
        self._refresh_tree()
        self._rebuild_work_doc()

    # --------------------------------------------------------- tree
    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for f in self.findings:
            self.tree.insert("", "end", iid=str(f.id), values=(
                CHECK_ON if f.enabled else CHECK_OFF,
                f.page + 1,
                KIND_LABEL_KO.get(f.kind, f.kind),
                f.text,
                SOURCE_LABEL_KO.get(f.source, f.source),
            ))
        for e in self.edits:
            self.tree.insert("", "end", iid="e%d" % e.id, values=(
                CHECK_ON if e.enabled else CHECK_OFF,
                e.page + 1,
                "글자수정",
                "%s→%s" % (_ell(e.old_text), _ell(e.new_text)),
                "수동",
            ))

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
        if row.startswith("e"):
            op = self._edit_by_id(int(row[1:]))
            if op is not None:
                op.enabled = not op.enabled
                self.tree.set(row, "chk", CHECK_ON if op.enabled else CHECK_OFF)
                self._rebuild_work_doc()   # revert/apply in the working copy
            return
        f = self._find_by_id(int(row))
        if f is not None:
            f.enabled = not f.enabled
            self.tree.set(row, "chk", CHECK_ON if f.enabled else CHECK_OFF)
            if self.preview_mode == "original":
                self.render_preview()

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
    def _opts(self):
        return Options(
            scope=self.var_scope.get(),
            style=self.var_style.get(),
            address=self.var_addr.get(),
            ocr=self.var_ocr.get(),
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
                    self._on_mask_done(m["path"], m["count"], m.get("pngs", []))
                elif k == "batch_done":
                    self._on_batch_done(m["done"], m["masked"], m["total"])
                elif k == "editsave_done":
                    self._on_editsave_done(m["path"], m["count"], m.get("pngs", []))
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
                    self._pending_mask = False
                    self._set_busy(False)
                    self.progress.stop()
                    self.progress.config(mode="determinate", value=0)
                    messagebox.showerror("오류", m["text"])
                    self.log_msg("오류: %s" % m["text"])
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    # --------------------------------------------------------- detect
    def on_detect(self):
        if self._busy:
            return
        path = self._current_file()
        if not path:
            self._pending_mask = False
            messagebox.showwarning("알림", "먼저 파일을 추가하세요.")
            return
        self._load_preview(path)
        if self.work_doc is None:
            self._pending_mask = False
            return
        opts = self._opts()
        edits = list(self.edits)
        self._set_busy(True)
        self.progress.config(mode="indeterminate")
        self.progress.start(20)
        self.log_msg("검사 시작: %s" % os.path.basename(path))
        threading.Thread(target=self._detect_worker, args=(path, opts, edits), daemon=True).start()

    def _detect_worker(self, path, opts, edits):
        try:
            # detection runs on the edited working copy (fresh open + edits)
            doc = self._build_work_doc(path, edits)
            try:
                findings = detect(doc, opts, progress=self._progress_cb)
            finally:
                doc.close()
            self._post("detect_done", findings=findings)
        except EncryptedPdfError as e:
            self._post("error", text=str(e))
        except Exception:  # noqa: BLE001
            self._post("error", text=traceback.format_exc().splitlines()[-1])

    def _on_detect_done(self, findings):
        self.findings = findings
        self._set_busy(False)
        self.progress.stop()
        self.progress.config(mode="determinate", value=0)
        self._refresh_tree()
        self.preview_mode = "original"
        # fresh detections must be visible - re-enable hidden overlays
        self.show_overlays = True
        self.btn_overlay.config(text="표시 숨기기")
        self.render_preview()
        self.log_msg("검사 완료: %d건 검출" % len(findings))
        # ② was pressed without findings: continue straight into masking.
        if self._pending_mask:
            self._pending_mask = False
            path = self._current_file()
            if not findings:
                self.log_msg("검출된 항목이 없어 마스킹을 건너뜁니다.")
            elif path:
                self._start_mask(path)

    # --------------------------------------------------------- mask
    def on_mask(self):
        if self._busy:
            return
        path = self._current_file()
        if not path:
            messagebox.showwarning("알림", "먼저 파일을 추가하세요.")
            return
        if not self.findings:
            # SPEC 7: detection runs first, then masking continues automatically.
            self.log_msg("검출 목록이 없어 검사 후 자동으로 마스킹을 진행합니다.")
            self._pending_mask = True
            self.on_detect()
            return
        self._start_mask(path)

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

    def _start_mask(self, path):
        self._release_result_doc()
        opts = self._opts()
        findings = self.findings
        edits = list(self.edits)
        self._set_busy(True)
        self.progress.config(mode="indeterminate")
        self.progress.start(20)
        self.log_msg("마스킹 실행...")
        threading.Thread(target=self._mask_worker, args=(path, opts, findings, edits),
                         daemon=True).start()

    def _mask_worker(self, path, opts, findings, edits):
        try:
            doc = open_document(path)
            out_path = output_path_for(path, opts)
            # apply_and_save applies the enabled edits first, then the masks
            count, pngs = apply_and_save(doc, findings, opts, out_path,
                                         edits=edits, progress=self._progress_cb)
            # keep a fresh handle for result preview
            self._result_tmp = fitz.open(out_path)
            self._post("mask_done", path=out_path, count=count, pngs=pngs)
            doc.close()
        except EncryptedPdfError as e:
            self._post("error", text=str(e))
        except Exception:  # noqa: BLE001
            self._post("error", text=traceback.format_exc().splitlines()[-1])

    def _on_mask_done(self, path, count, pngs):
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
        messagebox.showinfo("완료", "%d건 마스킹 완료\n%s" % (count, path))

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
        self.log_msg("수정만 저장 실행...")
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
        self.log_msg("글자 수정 %d건 저장: %s" % (count, path))
        if pngs:
            self.log_msg("PNG %d장 저장" % len(pngs))
        messagebox.showinfo("완료", "글자 수정 %d건 저장 완료\n%s" % (count, path))

    # --------------------------------------------------------- batch
    def on_batch(self):
        """③: process every file in the list with the current options."""
        if self._busy:
            return
        if not self.files:
            messagebox.showwarning("알림", "먼저 파일을 추가하세요.")
            return
        self._release_result_doc()   # batch may overwrite the previewed output
        opts = self._opts()
        files = list(self.files)
        edits_path = self.current_path      # edits belong to this file only
        edits = list(self.edits)
        self._set_busy(True)
        self.progress.config(mode="indeterminate")
        self.progress.start(20)
        self.log_msg("일괄 처리 시작: %d개 파일" % len(files))
        threading.Thread(target=self._batch_worker, args=(files, opts, edits_path, edits),
                         daemon=True).start()

    def _batch_worker(self, files, opts, edits_path, edits):
        done = 0
        masked_total = 0
        for i, path in enumerate(files):
            self._post("log", text="[%d/%d] %s" % (i + 1, len(files), os.path.basename(path)))
            try:
                file_edits = edits if path == edits_path else None
                rep = run(path, opts, progress=self._progress_cb, edits=file_edits)
            except Exception as e:  # noqa: BLE001 - skip this file, keep going
                self._post("log", text="  건너뜀(오류): %s" % e)
                continue
            done += 1
            masked_total += rep["masked_count"]
            self._post("log", text="  검출 %d건 / 마스킹 %d건 -> %s"
                       % (len(rep["findings"]), rep["masked_count"], rep["output"]))
        self._post("batch_done", done=done, masked=masked_total, total=len(files))

    def _on_batch_done(self, done, masked, total):
        self._set_busy(False)
        self.progress.stop()
        self.progress.config(mode="determinate", value=0)
        self.log_msg("일괄 처리 완료: %d/%d개 파일, %d건 마스킹" % (done, total, masked))
        messagebox.showinfo("완료", "%d개 파일 완료, %d건 마스킹" % (done, masked))

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
        self._pending_mask = False
        self._refresh_tree()
        self.canvas.delete("all")
        self.lbl_page.config(text="- / -")
        self.btn_toggle.config(state="disabled", text="결과 보기")

        if idx is not None and idx < len(self.files):
            del self.files[idx]
            self.file_list.delete(idx)
        self.file_list.selection_clear(0, "end")
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
    """
    root = tk.Tk()
    try:
        app = MarkIDApp(root)
        root.update_idletasks()
        # exercise a couple of no-doc code paths that must stay exception-safe
        app.render_preview()
        app._refresh_tree()
    finally:
        root.destroy()
