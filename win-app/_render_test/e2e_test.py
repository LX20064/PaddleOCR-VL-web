#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""e2e 全链路测试：模拟应用自动模式（parse 识别 → render 4 种格式导出）。
用打包版 python + 打包版 scan_worker.py + 运行中的 OCR 服务（127.0.0.1:8080）。"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(r"c:\Users\lx\Downloads\PaddleOCR-VL")
WIN = ROOT / "win-app"
PY = WIN / "offline" / "python" / "python.exe"
WORKER = WIN / "electron" / "python" / "scan_worker.py"
OUT = WIN / "_render_test" / "e2e_out"
PDF = ROOT / "formula_demo.pdf"

OUT.mkdir(parents=True, exist_ok=True)
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


def run_worker(mode, payload, echo_lines=False):
    # scan_worker 协议：识别链路 argv[1] 固定 "parse"（真实模式在 req.mode）；渲染链路固定 "render"
    sub_mode = "parse" if mode == "scan" else "render"
    args = [str(PY), str(WORKER), sub_mode, str(PDF), "-", str(ROOT), str(OUT)]
    proc = subprocess.run(
        args,
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        env=ENV,
    )
    lines = []
    last = None
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        lines.append(line)
        if echo_lines:
            print(f"  [out] {line[:300]}", flush=True)
        try:
            last = json.loads(line)
        except Exception:
            pass
    return last, proc.stderr.decode("utf-8", "replace"), lines


req_base = {
    "filePath": str(PDF),
    "perPage": False,
    "exportChart": False,
    "settings": {"apiUrl": "http://127.0.0.1:8080/layout-parsing", "timeout": 600, "pdfRenderDpi": 288},
    "params": {
        "use_layout_mode": True,
        "use_merge_blocks": True,
        "use_seal": False,
        "use_chart": False,
        "use_orientation": False,
        "use_unwarping": False,
        "use_ocr_image_block": False,
        "use_format_block": False,
        "pdf_per_page": False,
        "export_chart": False,
        "max_pixels": 0,
        "min_pixels": 0,
        "max_new_tokens": 0,
        "repetition_penalty": 1.0,
        "keep_source_dir": True,
        "skip_existing": False,
    },
}

t0 = time.time()
print("== 1/2 scan（完整识别，模拟应用 parse 链路）==", flush=True)
res, err, lines = run_worker("scan", {**req_base, "mode": "scan"}, echo_lines=True)
if not res or not res.get("ok"):
    print("PARSE_FAIL", (res or {}).get("error", ""))
    print("---- ALL STDOUT ----")
    for ln in lines:
        print(ln[:400])
    print("---- STDERR ----")
    print(err[:4000])
    sys.exit(1)
print(f"parse ok: {res.get('status')} elapsed={res.get('elapsed')}", flush=True)
print(f"  download={res.get('download')}", flush=True)

md = res.get("md") or ""
images = res.get("images") or {}
pruned = res.get("prunedResults")

for mode in ("docx", "html", "json", "zip_md"):
    print(f"== 2/2 render {mode} ==", flush=True)
    req = {
        **req_base,
        "mode": mode,
        "md": md,
        "images": images,
        "prunedResults": pruned if mode == "json" else None,
    }
    r2, e2, _l2 = run_worker("render", req)
    if not r2 or not r2.get("ok"):
        print(f"RENDER_FAIL {mode}:", (r2 or {}), e2[:1500])
        sys.exit(1)
    print(f"render {mode} ok: {r2.get('status')}", flush=True)
    print(f"  download={r2.get('download')}", flush=True)

print(f"ALL_OK total={time.time() - t0:.1f}s out={OUT}", flush=True)
