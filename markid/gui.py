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
from .engine import (
    EncryptedPdfError,
    Options,
    apply_and_save,
    detect,
    open_document,
    output_path_for,
    run,
)
from .findings import KIND_LABEL_KO, SOURCE_LABEL_KO, Finding

FONT = ("맑은 고딕", 9)
FONT_BOLD = ("맑은 고딕", 9, "bold")

COLOR = {
    "rrn": "#e11d1d",
    "rrn_partial": "#e11d1d",
    "address": "#1d5fe1",
    "manual": "#1a9e3a",
    "disabled": "#9aa0a6",
}
CHECK_ON = "☑"   # ☑
CHECK_OFF = "☐"  # ☐


class MarkIDApp:
    def __init__(self, root):
        self.root = root
        root.title("MarkID - 계약서 주민등록번호 마스킹 도구 v%s" % __version__)
        root.geometry("1280x840")
        root.minsize(1100, 700)

        self.files = []
        self.orig_doc = None        # original preview document
        self.result_doc = None      # masked result document (in memory)
        self.result_path = None
        self.findings = []
        self.cur_page = 0
        self.page_count = 0
        self.preview_mode = "original"
        self.scale = 1.0
        self.tkimg = None
        self.manual_mode = False
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
        top = ttk.Frame(parent)
        top.pack(fill="x")
        ttk.Button(top, text="◀", width=3, command=lambda: self.change_page(-1)).pack(side="left")
        self.lbl_page = ttk.Label(top, text="- / -", width=10, anchor="center")
        self.lbl_page.pack(side="left")
        ttk.Button(top, text="▶", width=3, command=lambda: self.change_page(1)).pack(side="left")
        self.btn_toggle = ttk.Button(top, text="결과 보기", command=self.toggle_preview, state="disabled")
        self.btn_toggle.pack(side="left", padx=8)
        self.btn_manual = ttk.Button(top, text="수동 영역 추가: 꺼짐", command=self.toggle_manual)
        self.btn_manual.pack(side="left")
        self.lbl_mode = ttk.Label(top, text="원본", foreground="#333")
        self.lbl_mode.pack(side="left", padx=8)

        cvf = ttk.Frame(parent)
        cvf.pack(fill="both", expand=True, pady=4)
        self.canvas = tk.Canvas(cvf, background="#5a5a5a", highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        vs = ttk.Scrollbar(cvf, orient="vertical", command=self.canvas.yview)
        vs.pack(side="left", fill="y")
        hs = ttk.Scrollbar(parent, orient="horizontal", command=self.canvas.xview)
        hs.pack(fill="x")
        self.canvas.config(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.canvas.bind("<Configure>", lambda e: self.render_preview())
        self.canvas.bind("<ButtonPress-1>", self._on_canvas_press)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)

        # -- findings table --
        ttk.Label(parent, text="검출 목록 (체크 해제 시 제외)", font=FONT_BOLD).pack(anchor="w")
        tf = ttk.Frame(parent)
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
        if self.files and self.orig_doc is None:
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
    def _load_preview(self, path):
        self._close_docs()
        try:
            self.orig_doc = open_document(path)
        except EncryptedPdfError as e:
            messagebox.showerror("오류", str(e))
            self.log_msg("오류: %s" % e)
            return
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("오류", "파일을 열 수 없습니다: %s" % e)
            return
        self.page_count = self.orig_doc.page_count
        self.cur_page = 0
        self.preview_mode = "original"
        self.findings = []
        self.result_doc = None
        self.result_path = None
        self.btn_toggle.config(state="disabled", text="결과 보기")
        self._refresh_tree()
        self.render_preview()

    def _close_docs(self):
        for d in (self.orig_doc, self.result_doc):
            try:
                if d is not None:
                    d.close()
            except Exception:
                pass
        self.orig_doc = None
        self.result_doc = None

    def _active_doc(self):
        if self.preview_mode == "result" and self.result_doc is not None:
            return self.result_doc
        return self.orig_doc

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
        ph = page.rect.height or 1
        self.scale = min(max(cw - 4, 200) / pw, 2.0)
        dpi = max(36, min(200, self.scale * 72))
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
        self.lbl_mode.config(text="결과" if self.preview_mode == "result" else "원본")
        if self.preview_mode == "original":
            self._draw_overlays()

    def _draw_overlays(self):
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

    def change_page(self, delta):
        self.cur_page += delta
        self.render_preview()

    def toggle_preview(self):
        if self.result_doc is None:
            return
        self.preview_mode = "result" if self.preview_mode == "original" else "original"
        self.btn_toggle.config(text="원본 보기" if self.preview_mode == "result" else "결과 보기")
        self.render_preview()

    # --------------------------------------------------------- manual add
    def toggle_manual(self):
        self.manual_mode = not self.manual_mode
        self.btn_manual.config(text="수동 영역 추가: %s" % ("켜짐" if self.manual_mode else "꺼짐"))

    def _on_canvas_press(self, e):
        if not self.manual_mode:
            return
        self._drag_start = (self.canvas.canvasx(e.x), self.canvas.canvasy(e.y))
        self._drag_rect = self.canvas.create_rectangle(
            self._drag_start[0], self._drag_start[1], self._drag_start[0], self._drag_start[1],
            outline=COLOR["manual"], width=2)

    def _on_canvas_drag(self, e):
        if not self.manual_mode or self._drag_start is None:
            return
        x = self.canvas.canvasx(e.x)
        y = self.canvas.canvasy(e.y)
        self.canvas.coords(self._drag_rect, self._drag_start[0], self._drag_start[1], x, y)

    def _on_canvas_release(self, e):
        if not self.manual_mode or self._drag_start is None:
            return
        x0, y0 = self._drag_start
        x1 = self.canvas.canvasx(e.x)
        y1 = self.canvas.canvasy(e.y)
        self._drag_start = None
        rx0, rx1 = sorted((x0 / self.scale, x1 / self.scale))
        ry0, ry1 = sorted((y0 / self.scale, y1 / self.scale))
        if rx1 - rx0 < 2 or ry1 - ry0 < 2:
            return
        fid = max([f.id for f in self.findings], default=-1) + 1
        self.findings.append(Finding(id=fid, page=self.cur_page, kind="manual",
                                     source="manual", text="수동 지정 영역",
                                     rect=(rx0, ry0, rx1, ry1)))
        self._refresh_tree()
        self.render_preview()

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

    def _find_by_id(self, fid):
        for f in self.findings:
            if f.id == fid:
                return f
        return None

    def _on_tree_click(self, e):
        row = self.tree.identify_row(e.y)
        if not row:
            return
        col = self.tree.identify_column(e.x)
        if col == "#1":  # checkbox column
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
        f = self._find_by_id(int(sel[0]))
        if f is None:
            return
        self.preview_mode = "original"
        self.cur_page = f.page
        self.render_preview()
        x0, y0, x1, y1 = [v * self.scale for v in f.rect]
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
        if self.orig_doc is None:
            self._pending_mask = False
            return
        opts = self._opts()
        self._set_busy(True)
        self.progress.config(mode="indeterminate")
        self.progress.start(20)
        self.log_msg("검사 시작: %s" % os.path.basename(path))
        threading.Thread(target=self._detect_worker, args=(path, opts), daemon=True).start()

    def _detect_worker(self, path, opts):
        try:
            doc = open_document(path)
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
        self._set_busy(True)
        self.progress.config(mode="indeterminate")
        self.progress.start(20)
        self.log_msg("마스킹 실행...")
        threading.Thread(target=self._mask_worker, args=(path, opts, findings), daemon=True).start()

    def _mask_worker(self, path, opts, findings):
        try:
            doc = open_document(path)
            out_path = output_path_for(path, opts)
            count, pngs = apply_and_save(doc, findings, opts, out_path, progress=self._progress_cb)
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
        self._set_busy(True)
        self.progress.config(mode="indeterminate")
        self.progress.start(20)
        self.log_msg("일괄 처리 시작: %d개 파일" % len(files))
        threading.Thread(target=self._batch_worker, args=(files, opts), daemon=True).start()

    def _batch_worker(self, files, opts):
        done = 0
        masked_total = 0
        for i, path in enumerate(files):
            self._post("log", text="[%d/%d] %s" % (i + 1, len(files), os.path.basename(path)))
            try:
                rep = run(path, opts, progress=self._progress_cb)
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
        self.page_count = 0
        self.cur_page = 0
        self.preview_mode = "original"
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
