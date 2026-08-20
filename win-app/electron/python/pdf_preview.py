#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf_preview.py —— 用 PyMuPDF 把 PDF 各页渲染为 PNG 预览图（替代 Linux 的 pdftoppm）。
输出 JSONL：{"t":"result","ok":true,"pages":["C:\\...page-001.png", ...]}
用法：python pdf_preview.py <pdf_path> [preview_dir]
"""
import json
import sys
import uuid
from pathlib import Path


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def main():
    if len(sys.argv) < 2:
        emit({"t": "result", "ok": False, "error": "缺少 PDF 路径参数"})
        sys.exit(1)
    pdf_path = sys.argv[1]
    try:
        import fitz  # PyMuPDF
    except ImportError:
        emit({"t": "result", "ok": False,
              "error": "PyMuPDF 未安装：请在设置中安装后端环境（pip install pymupdf）。"})
        sys.exit(1)

    pdf = Path(pdf_path)
    if not pdf.exists() or pdf.suffix.lower() != ".pdf":
        emit({"t": "result", "ok": False, "error": f"文件不存在或不是 PDF：{pdf_path}"})
        sys.exit(1)

    preview_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    if preview_dir is None:
        import tempfile
        preview_dir = Path(tempfile.gettempdir()) / "paddleocr_preview" / f"{pdf.stem}_{uuid.uuid4().hex[:6]}"

    # 可选 --max-pages N：只渲染前 N 页（大图预览仅需首页时避免整本渲染）
    max_pages = None
    rest = sys.argv[3:]
    if "--max-pages" in rest:
        i = rest.index("--max-pages")
        try:
            max_pages = max(1, int(rest[i + 1]))
        except (IndexError, ValueError):
            max_pages = None

    pages = []
    doc = None
    try:
        preview_dir.mkdir(parents=True, exist_ok=True)
        doc = fitz.open(pdf_path)
        total = len(doc)
        if total == 0:
            emit({"t": "result", "ok": False, "error": "PDF 无页面。"})
            sys.exit(1)
        count = min(max_pages, total) if max_pages else total
        for n in range(count):
            page = doc[n]
            pix = page.get_pixmap(dpi=120)
            out = preview_dir / f"page-{n + 1:03d}.png"
            pix.save(str(out))
            pages.append(str(out))
            if n % 5 == 0:
                emit({"t": "progress", "frac": (n + 1) / max(count, 1),
                      "desc": f"渲染 PDF 预览 {n + 1}/{count} 页"})
    except SystemExit:
        raise
    except Exception as e:
        emit({"t": "result", "ok": False, "error": f"PDF 渲染失败：{e}"})
        sys.exit(1)
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass
    emit({"t": "result", "ok": True, "pages": pages})


if __name__ == "__main__":
    main()
