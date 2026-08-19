#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
merge_to_pdf.py —— 将一组图片页面合并为 PDF（免 OCR，扫描后直接导出）
输出 JSONL 事件流：{"t":"result","ok":true,"file":out.pdf,"count":N}
用法：python merge_to_pdf.py --out out.pdf [--dpi 300] page1.png page2.jpg ...
"""
import argparse
import json
import sys


def main():
    class _JsonArgParser(argparse.ArgumentParser):
        # argparse 参数错误默认打 usage 到 stderr 并 exit(2)，stdout 无 JSON。
        # 这里改成输出 JSON 错误，让主进程拿到可读错误。
        def error(self, message):
            print(json.dumps({"t": "result", "ok": False, "error": f"参数错误：{message}"}))
            sys.exit(2)

    ap = _JsonArgParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("pages", nargs="+")
    args = ap.parse_args()

    if args.dpi <= 0:
        print(json.dumps({"t": "result", "ok": False, "error": "--dpi 必须为正数。"}))
        sys.exit(1)

    try:
        import pymupdf  # PyMuPDF 新 API
    except ImportError:
        try:
            import fitz as pymupdf
        except ImportError:
            print(json.dumps({"t": "result", "ok": False,
                              "error": "生成 PDF 需要 PyMuPDF，请安装后端依赖（pip install pymupdf）。"}))
            sys.exit(1)

    scale = 72.0 / args.dpi
    try:
        doc = pymupdf.open()
        for p in args.pages:
            pix = pymupdf.Pixmap(p)
            w = max(1, pix.width * scale)
            h = max(1, pix.height * scale)
            page = doc.new_page(width=w, height=h)
            page.insert_image(pymupdf.Rect(0, 0, w, h), pixmap=pix)
            pix = None
        doc.save(args.out, deflate=True, garbage=3)
        doc.close()
        print(json.dumps({"t": "result", "ok": True, "file": args.out,
                          "count": len(args.pages)}))
    except Exception as e:
        print(json.dumps({"t": "result", "ok": False,
                          "error": f"合并 PDF 失败：{type(e).__name__}: {e}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
