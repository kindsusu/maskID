"""Command-line interface and process entry point.

    MarkID [--cli] --input a.pdf [b.pdf ...] [--outdir DIR]
           --scope full|back6 --style box|mosaic
           [--address] [--no-ocr] [--report r.json] [--png]
    MarkID --selftest        # build the GUI, verify widgets, exit 0

With no arguments the GUI launches.  Exit codes: 0 success, 2 input/error.
Console output is kept ASCII-safe (details go to the JSON report).
"""

import argparse
import json
import os
import sys

from . import __version__
from .engine import EncryptedPdfError, Options, run


def _ascii(s):
    return str(s).encode("ascii", "replace").decode("ascii")


def _out(msg):
    try:
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def build_parser():
    ap = argparse.ArgumentParser(
        prog="MarkID",
        description="계약서 주민등록번호 마스킹 도구 (offline).",
    )
    ap.add_argument("--cli", action="store_true", help="force command-line mode")
    ap.add_argument("--input", nargs="+", metavar="FILE", help="input PDF/image files")
    ap.add_argument("--outdir", metavar="DIR", help="output directory")
    ap.add_argument("--scope", choices=["full", "back6"], default="back6",
                    help="mask whole RRN or last 6 digits (default: back6)")
    ap.add_argument("--style", choices=["box", "mosaic"], default="box",
                    help="masking style (default: box)")
    ap.add_argument("--address", action="store_true", help="also mask addresses")
    ap.add_argument("--no-ocr", action="store_true", help="disable image OCR")
    ap.add_argument("--report", metavar="JSON", help="write findings report (UTF-8 JSON)")
    ap.add_argument("--png", action="store_true", help="also save page PNGs")
    ap.add_argument("--selftest", action="store_true", help="build GUI and exit 0")
    ap.add_argument("--version", action="store_true", help="print version and exit")
    return ap


def _progress(stage=None, page=None, total=None, sub=None, **kw):
    if page is None:
        return
    if stage == "text":
        _out("  scan page %d/%s" % (page + 1, total))
    elif stage == "ocr" and (sub in (0, None)):
        _out("  ocr  page %d" % (page + 1))
    elif stage == "mask":
        _out("  mask page %d/%s" % (page + 1, total))


def run_cli(args):
    inputs = args.input or []
    valid = [p for p in inputs if os.path.isfile(p)]
    for p in inputs:
        if p not in valid:
            _out("ERROR input not found: %s" % _ascii(p))
    if not valid:
        _out("ERROR no valid input files")
        return 2

    opts = Options(
        scope=args.scope, style=args.style, address=args.address,
        ocr=not args.no_ocr, png=args.png, outdir=args.outdir,
    )

    reports = []
    rc = 0
    n = len(valid)
    for i, path in enumerate(valid):
        _out("[%d/%d] %s" % (i + 1, n, _ascii(os.path.basename(path))))
        try:
            rep = run(path, opts, progress=_progress, file_index=i, file_total=n)
            reports.append(rep)
            _out("  done: %d masked -> %s" % (rep["masked_count"], _ascii(rep["output"])))
        except EncryptedPdfError:
            _out("  ERROR: encrypted PDF is not supported")
            reports.append({"input": os.path.abspath(path), "error": "encrypted"})
            rc = 2
        except Exception as e:  # noqa: BLE001 - report and continue other files
            _out("  ERROR: %s" % _ascii(e))
            reports.append({"input": os.path.abspath(path), "error": str(e)})
            rc = 2

    if args.report:
        try:
            with open(args.report, "w", encoding="utf-8") as f:
                json.dump({"version": __version__, "files": reports}, f,
                          ensure_ascii=False, indent=2)
            _out("report: %s" % _ascii(args.report))
        except Exception as e:  # noqa: BLE001
            _out("ERROR writing report: %s" % _ascii(e))
            rc = 2
    return rc


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.version:
        _out("MarkID %s" % __version__)
        return 0

    if args.selftest:
        try:
            from .gui import selftest
            selftest()
            _out("selftest OK")
            return 0
        except Exception as e:  # noqa: BLE001
            _out("selftest FAIL: %s" % _ascii(e))
            return 2

    if args.input:
        return run_cli(args)

    if args.cli:
        _out("ERROR: --cli requires --input")
        return 2

    # No input -> launch GUI.
    try:
        from .gui import launch
        launch()
        return 0
    except Exception as e:  # noqa: BLE001
        _out("GUI error: %s" % _ascii(e))
        return 2


if __name__ == "__main__":
    sys.exit(main())
