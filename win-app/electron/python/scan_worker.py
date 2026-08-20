#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scan_worker.py —— Windows 桌面版的解析/导出 worker（Electron 主进程经 venv python 调用）
======================================================================================
复用项目根目录的 wordrender.py（本地 DOCX 渲染）与现有 /layout-parsing 协议，
输出 JSONL 事件流到 stdout，每行一个 JSON：
  {"t":"progress","frac":0.05,"desc":"..."}
  {"t":"result","ok":true,"status":"...","md":"...","images":{rel:b64},"download":"...","pages":N,"elapsed":s}
  {"t":"result","ok":false,"error":"..."}

用法：python scan_worker.py parse <file_path> <request.json字符串>
"""
import base64
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

import requests

# ---------- 项目根（wordrender.py 所在目录） ----------
PADDLE_ROOT = Path(sys.argv[4]) if len(sys.argv) > 4 else Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PADDLE_ROOT))

# 解析结果输出目录（Electron 传入；打包版为用户数据目录下 outputs）
# 目录在 main()/main_render() 内通过 mkdir(parents=True) 按需创建，避免在模块导入期
# 抛异常导致 stdout 无 JSON 输出。
_OUTPUT_ROOT = Path(sys.argv[5]) if len(sys.argv) > 5 else PADDLE_ROOT / "outputs"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def progress(frac, desc):
    emit({"t": "progress", "frac": frac, "desc": desc})


def fail(msg):
    emit({"t": "result", "ok": False, "error": msg})
    sys.exit(1)


# ---------- 请求解析 ----------
def parse_args():
    """argv: parse <file_path> <request_json|-> [paddle_root] [output_root]；request 传 '-' 时从 stdin 读取 JSON"""
    if len(sys.argv) < 4 or sys.argv[1] != "parse":
        fail("用法：scan_worker.py parse <file_path> <request_json> [paddle_root] [output_root]")
    req_arg = sys.argv[3]
    if req_arg == "-":
        req = json.loads(sys.stdin.read())
    else:
        req = json.loads(req_arg)
    return req


# ---------- 图片 MIME 嗅探 ----------
def _image_data_uri(b64):
    if not b64:
        return ""
    head = b64[:32]
    if head.startswith("/9j/"):
        mime = "image/jpeg"
    elif head.startswith("iVBORw0KGgo"):
        mime = "image/png"
    elif head.startswith("R0lGOD"):
        mime = "image/gif"
    elif head.startswith("UklGR"):
        mime = "image/webp"
    else:
        mime = "image/png"
    return f"data:{mime};base64,{b64}"


# ---------- PDF 预览渲染（PyMuPDF，替代 Linux 的 pdftoppm） ----------
def render_pdf_pages(pdf_path):
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return []
    pdf = Path(pdf_path)
    if pdf.suffix.lower() != ".pdf" or not pdf.exists():
        return []
    preview_dir = _OUTPUT_ROOT / ".previews" / f"{pdf.stem}_{uuid.uuid4().hex[:6]}"
    preview_dir.mkdir(parents=True, exist_ok=True)
    out = []
    doc = None
    try:
        doc = fitz.open(pdf_path)
        for i, page in enumerate(doc):
            pix = page.get_pixmap(dpi=120)
            p = preview_dir / f"page-{i + 1:03d}.png"
            pix.save(str(p))
            out.append(str(p))
    except Exception:
        return []
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass
    return out


# ---------- chart 区域裁剪 ----------
def crop_chart_images(pages, source_path, file_type):
    crops = {}
    try:
        from PIL import Image
    except ImportError:
        return crops

    page_imgs = []
    preview_dir = None
    try:
        if file_type == 1:
            try:
                page_imgs = [Image.open(source_path)]
            except Exception:
                return crops
        else:
            preview_paths = render_pdf_pages(source_path)
            if preview_paths:
                preview_dir = Path(preview_paths[0]).parent
            for p in preview_paths:
                try:
                    page_imgs.append(Image.open(p))
                except Exception:
                    page_imgs.append(None)

        counter = 0
        for pi, page in enumerate(pages):
            pruned = page.get("prunedResult") or {}
            width = pruned.get("width") or 0
            height = pruned.get("height") or 0
            src = page_imgs[pi] if pi < len(page_imgs) else None
            if src is None:
                continue
            sw, sh = src.size
            for block in (pruned.get("parsing_res_list") or []):
                if block.get("block_label") != "chart":
                    continue
                bbox = block.get("block_bbox")
                if not bbox or len(bbox) < 4:
                    continue
                x1, y1, x2, y2 = bbox[:4]
                if width and height:
                    x1 = int(x1 / width * sw); y1 = int(y1 / height * sh)
                    x2 = int(x2 / width * sw); y2 = int(y2 / height * sh)
                x1 = max(0, min(int(x1), sw)); x2 = max(0, min(int(x2), sw))
                y1 = max(0, min(int(y1), sh)); y2 = max(0, min(int(y2), sh))
                if x2 <= x1 or y2 <= y1:
                    continue
                try:
                    crop = src.crop((x1, y1, x2, y2))
                    buf = io.BytesIO()
                    crop.convert("RGB").save(buf, format="PNG")
                except Exception:
                    continue
                counter += 1
                crops[f"chart_page{pi + 1:02d}_{counter:02d}.png"] = buf.getvalue()
        return crops
    finally:
        # 释放 PIL 句柄；PDF 预览临时目录用完即删，避免每次导出图表在 outputs/.previews 累积
        for img in page_imgs:
            try:
                if img is not None:
                    img.close()
            except Exception:
                pass
        if preview_dir is not None:
            try:
                shutil.rmtree(preview_dir, ignore_errors=True)
            except Exception:
                pass


# ---------- Markdown → HTML（内嵌 base64 图片 + MathML 公式） ----------
def _md_to_html(md_text, images):
    try:
        import markdown as _markdown
        from bs4 import BeautifulSoup
        from latex2mathml.converter import convert as _latex_to_mathml
        import wordrender
    except Exception:
        # 依赖缺失或导入失败：返回 None 让调用方明确报错，而不是静默缺失
        return None

    # 先把 LaTeX 公式替换成占位符，避免 markdown 库把 $...$ 当普通文本转义；
    # 公式复用 wordrender 的 _MATH_RE 与 \genfrac 预处理，转成 MathML 供浏览器原生渲染。
    math_blocks = []

    def _math_repl(m):
        raw = m.group(0)
        display = raw.startswith("$$") or raw.startswith("\\[")
        if raw.startswith("$$"):
            tex = raw[2:-2]
        elif raw.startswith("\\["):
            tex = raw[2:-2]
        elif raw.startswith("\\("):
            tex = raw[2:-2]
        else:
            tex = raw[1:-1]  # $...$
        try:
            mathml = _latex_to_mathml(
                wordrender._preprocess_latex(tex.strip()),
                display="block" if display else "inline",
            )
            token = f"MATHTOKEN{len(math_blocks)}X"
            math_blocks.append((token, mathml))
            return token
        except Exception:
            return raw  # 转换失败保留原文

    protected = wordrender._MATH_RE.sub(_math_repl, md_text)
    html = _markdown.markdown(protected, extensions=["tables", "fenced_code"])
    for token, mathml in math_blocks:
        html = html.replace(token, mathml)

    soup = BeautifulSoup(html, "html.parser")
    for img_tag in soup.find_all("img"):
        src = img_tag.get("src") or ""
        if src in images:
            img_tag["src"] = _image_data_uri(images[src])
    for table in soup.find_all("table"):
        table["border"] = "1"; table["cellpadding"] = "6"; table["cellspacing"] = "0"
        table["style"] = "border-collapse: collapse; width: 100%;"
    return (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n  <meta charset=\"UTF-8\">\n"
        "  <title>解析结果</title>\n  <style>\n"
        "    body { font-family: 'Times New Roman', '宋体', serif; max-width: 960px; margin: 40px auto; padding: 0 20px; line-height: 1.6; }\n"
        "    img { max-width: 100%; height: auto; display: block; margin: 12px auto; }\n"
        "    table { border-collapse: collapse; margin: 12px 0; }\n"
        "    th, td { border: 1px solid #ccc; padding: 6px 10px; text-align: center; }\n"
        "    th { background: #f5f5f5; }\n"
        "    math[display=\"block\"] { display: block; margin: 12px 0; }\n"
        "    math { font-size: 1.05em; }\n"
        "  </style>\n</head>\n<body>\n"
        f"{soup}\n</body>\n</html>"
    )


# ---------- DOCX（复用 wordrender） ----------
def _md_to_docx_python(md_text, images, out_path, page_prefix=""):
    try:
        import wordrender
    except ImportError:
        raise RuntimeError("wordrender 依赖不可用，无法生成 Word 文档。请检查后端环境。")
    base_dir = Path(tempfile.mkdtemp(prefix="paddleocr_wr_"))
    try:
        processed_md = md_text
        for rel_path, b64 in images.items():
            safe_rel = page_prefix + rel_path.replace("\\", "/")
            safe_rel = safe_rel.lstrip("/")
            target = base_dir / safe_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.write_bytes(base64.b64decode(b64))
            except Exception:
                continue
            processed_md = processed_md.replace(rel_path, safe_rel)
        summary = wordrender.write_docx(processed_md, out_path, base_dir)
        return f"(公式{summary.formulas_converted} 图片{summary.images_embedded})"
    finally:
        shutil.rmtree(base_dir, ignore_errors=True)


def _add_chart_crops(zf, chart_crops):
    for name, data in chart_crops.items():
        zf.writestr(f"charts/{name}", data)


# ---------- 导出打包辅助 ----------
# 各导出模式对应的压缩包后缀（用户要求：按导出方式区分后缀）
MODE_SUFFIX = {"docx": "Word", "html": "HTML", "json": "JSON", "zip_md": "Markdown"}


def _write_images_zip(zf, images, prefix="images"):
    """把识别结果图片写入 zip 的 <prefix>/ 目录下（保留原始相对路径）。"""
    for rel, b64 in images.items():
        name = rel.replace("\\", "/").lstrip("/")
        if name:
            zf.writestr(f"{prefix}/{name}", base64.b64decode(b64))


def _rewrite_md_image_refs(md, images, prefix="images/"):
    """把 md 中的图片引用改写为 zip 内 <prefix> 下的路径（图片已归入文件夹）。"""
    for rel in images:
        safe = rel.replace("\\", "/").lstrip("/")
        if safe:
            md = md.replace(rel, prefix + safe)
    return md


# ---------- 主流程 ----------
def build_payload(path, file_type, p, req):
    """与 web_ocr._parse_core 相同的请求构造。p 为渲染进程传入的参数 dict。"""
    payload = {
        "file": base64.b64encode(path.read_bytes()).decode("ascii"),
        "fileType": file_type,
        "useDocOrientationClassify": bool(p.get("useOrientation")),
        "useDocUnwarping": bool(p.get("useUnwarping")),
        "useSealRecognition": bool(p.get("useSeal")),
        "useChartRecognition": bool(p.get("useChart")),
        "useOcrForImageBlock": bool(p.get("useOcrForImageBlock")),
        "formatBlockContent": bool(p.get("formatBlockContent")),
        "useLayoutDetection": bool(p.get("useLayoutDetection", True)),
        "mergeLayoutBlocks": bool(p.get("mergeLayoutBlocks", True)),
        "returnMarkdownImages": True,
    }
    if p.get("maxPixels") and int(p["maxPixels"]) > 0:
        payload["maxPixels"] = int(p["maxPixels"])
    if not payload["useLayoutDetection"] and p.get("promptLabel"):
        payload["promptLabel"] = p["promptLabel"]
    if p.get("layoutThreshold") is not None:
        payload["layoutThreshold"] = float(p["layoutThreshold"])
    if p.get("minPixels"):
        payload["minPixels"] = int(p["minPixels"])
    if p.get("maxNewTokens"):
        payload["maxNewTokens"] = int(p["maxNewTokens"])
    if p.get("temperature") is not None:
        payload["temperature"] = float(p["temperature"])
    if p.get("topP") is not None:
        payload["topP"] = float(p["topP"])
    if p.get("repetitionPenalty") is not None:
        payload["repetitionPenalty"] = float(p["repetitionPenalty"])
    # 版面后处理高级参数（None 不输出）
    for src, dst in [
        ("layoutNms", "layoutNms"),
        ("layoutUnclipRatio", "layoutUnclipRatio"),
        ("layoutMergeBboxesMode", "layoutMergeBboxesMode"),
        ("layoutShapeMode", "layoutShapeMode"),
        ("vlmExtraArgs", "vlmExtraArgs"),
        ("markdownIgnoreLabels", "markdownIgnoreLabels"),
    ]:
        if p.get(src) is not None:
            payload[dst] = p[src]
    return payload


def http_post(url, payload, timeout, retry=3):
    last = None
    for i in range(retry):
        try:
            r = requests.post(url, json=payload, timeout=timeout)
            return r
        except requests.ConnectionError:
            last = requests.ConnectionError(url)
            time.sleep(2)
    raise last


def main_render():
    """render <file_path> - [paddle_root] [output_root]：用已识别的 md/images 直接导出，跳过识别。

    请求 JSON 从 stdin 读取：{md, images, prunedResults, mode}。仅用于「识别结果已缓存」的重新导出，
    避免每次点导出都重新跑一遍 /layout-parsing（VLM 识别）导致导出很慢。
    """
    if len(sys.argv) < 4:
        fail("用法：scan_worker.py render <file_path> - [paddle_root] [output_root]")
    file_path = sys.argv[2]
    req = json.loads(sys.stdin.read())
    mode = req.get("mode", "docx")
    full_md = (req.get("md") or "").strip()
    images = req.get("images") or {}
    pruned_results = req.get("prunedResults")

    path = Path(file_path)
    # 输出文件夹以被扫描的文件名命名（同名重复导出复用同一目录，便于按文件归档结果）
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", path.stem or "result")
    out_dir = _OUTPUT_ROOT / stem
    out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()

    # 仅 Word 模式才做本地渲染；其他模式直接打包，避免无谓耗时
    docx_path = None
    if mode == "docx":
        progress(0.25, "本地渲染 Word…")
        docx_path = out_dir / f"{stem}.docx"
        try:
            _md_to_docx_python(full_md, images, str(docx_path))
        except Exception as e:
            fail(f"生成 Word 失败：{e}")

    if mode == "docx" and docx_path:
        elapsed = time.time() - t0
        emit({"t": "result", "ok": True,
              "status": f"已导出 Word：耗时 {elapsed:.1f} 秒",
              "md": full_md, "images": images, "download": str(docx_path),
              "pages": 1, "elapsed": round(elapsed, 1)})
        return

    if mode in ("html", "json", "zip_md"):
        suffix = MODE_SUFFIX[mode]
        zip_path = out_dir / f"{stem}_{suffix}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            if mode == "html":
                html_content = _md_to_html(full_md, images)
                if html_content:
                    zf.writestr(f"{stem}.html", html_content)
                else:
                    fail("HTML 导出失败：缺少依赖（markdown/bs4/latex2mathml/wordrender）。")
            elif mode == "json":
                if not pruned_results:
                    fail("JSON 导出需要缓存的结构化结果（prunedResults），请先重新识别一次。")
                json_text = json.dumps(pruned_results, ensure_ascii=False, indent=2)
                zf.writestr(f"{stem}.json", json_text)
            elif mode == "zip_md":
                zf.writestr(f"{stem}.md", _rewrite_md_image_refs(full_md, images))
            if images:
                _write_images_zip(zf, images)
        elapsed = time.time() - t0
        emit({"t": "result", "ok": True,
              "status": f"已导出 {suffix} 结果包：耗时 {elapsed:.1f} 秒",
              "md": full_md, "images": images, "download": str(zip_path),
              "pages": 1, "elapsed": round(elapsed, 1)})
        return

    fail(f"不支持的导出模式：{mode}")


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "render":
        main_render()
        return
    req = parse_args()
    file_path = req["filePath"]
    mode = req.get("mode", "scan")
    per_page = bool(req.get("perPage"))
    export_chart = bool(req.get("exportChart"))
    p = req.get("params") or {}
    s = req.get("settings") or {}
    api_url = s.get("apiUrl") or "http://127.0.0.1:8080/layout-parsing"
    timeout = int(s.get("timeout") or 600)

    if not file_path:
        fail("请先选择文件")
    path = Path(file_path)
    ext = path.suffix.lower()
    if ext == ".pdf":
        file_type = 0
    elif ext in IMAGE_EXTS:
        file_type = 1
    else:
        fail(f"不支持的文件类型：{ext or '(无扩展名)'}")

    t0 = time.time()

    # ---------- 输出目录规划 ----------
    # 需求：识别（扫描）本身不向 OCR 结果目录写入任何文件，仅当点击导出按钮时才落盘；
    # 输出文件夹以「被扫描的文件名」命名（同名重复导出复用同一目录，skip_existing 时跳过）。
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", path.stem or "result")
    is_export = mode in ("docx", "html", "json", "zip_md")
    out_dir = None
    if is_export:
        # 开启「保持来源子目录结构」时按源文件所在文件夹归类（同源文件复用同一目录）
        keep_src_dir = bool(p.get("keep_source_dir"))
        skip_existing = bool(p.get("skip_existing"))
        if keep_src_dir:
            src_parent = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", path.parent.name) or "_"
            out_dir = _OUTPUT_ROOT / src_parent / stem
        else:
            out_dir = _OUTPUT_ROOT / stem
        if skip_existing and out_dir.exists() and any(out_dir.glob("*.zip")):
            hit = next(out_dir.glob("*.zip"), None)
            emit({"t": "result", "ok": True, "status": f"已存在结果，跳过导出：{hit.name}",
                  "md": "", "images": {}, "download": str(hit), "pages": 1,
                  "elapsed": round(time.time() - t0, 1)})
            return
        out_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 调用后端 ----------
    progress(0.05, "读取并编码文件…")
    payload = build_payload(path, file_type, p, req)
    progress(0.2, "服务器解析中（大图 / 多页 PDF 可能需要几分钟）…")
    try:
        resp = http_post(api_url, payload, timeout)
    except requests.ConnectionError:
        fail(f"无法连接 OCR 服务：{api_url}\n请确认后端服务已启动（设置 → OCR 服务）。")
    if resp.status_code != 200:
        err_text = resp.text[:500]
        if "model settings are invalid" in err_text:
            fail("服务返回模型设置无效：服务端未加载文档预处理模型，请确认产线配置正常。")
        fail(f"服务返回 HTTP {resp.status_code}：{err_text}")
    data = resp.json()
    if data.get("errorCode") != 0:
        fail(f"解析失败：{data.get('errorMsg')}")

    pages = data["result"]["layoutParsingResults"]
    # JSON 导出需要的结构化结果（各页 prunedResult 列表），随 result 一起返回并缓存，
    # 之后点 JSON 导出就不用重新跑 VLM 识别。
    pruned_results = [pg.get("prunedResult") for pg in pages]
    # 仅多页 PDF 才需要跨页合并；单页走单文件分支，避免 /restructure-pages 对图片 block 重复输出 <img>
    merge_mode = (file_type == 0) and not per_page and len(pages) > 1

    chart_crops = {}
    if export_chart:
        chart_crops = crop_chart_images(pages, path, file_type)

    # ---------- 分支 A：PDF 合并为单个 Word ----------
    if merge_mode:
        progress(0.6, "合并多页结果…")
        rp_url = api_url.rsplit("/", 1)[0] + "/restructure-pages"
        rp_payload = {
            "pages": [
                {
                    "prunedResult": pg.get("prunedResult"),
                    "markdownImages": (pg.get("markdown") or {}).get("images"),
                }
                for pg in pages
            ],
            "mergeTables": True,
            "relevelTitles": True,
            "concatenatePages": True,
        }
        try:
            rp = http_post(rp_url, rp_payload, timeout)
            rp_data = rp.json()
        except Exception:
            fail("调用 /restructure-pages 合并服务失败，请查看服务端日志。")
        if rp.status_code != 200 or rp_data.get("errorCode") != 0:
            fail(f"合并失败：{rp_data.get('errorMsg', rp.text[:300])}")

        merged = rp_data["result"]["layoutParsingResults"][0]
        full_md = ((merged.get("markdown") or {}).get("text") or "").strip().replace("\\n", "\n")
        merged_images = (merged.get("markdown") or {}).get("images") or {}

        # 纯识别模式：不向 OCR 结果目录写入任何文件，仅返回合并结果供前端预览/缓存，
        # 用户点击导出按钮时才生成文件。
        if mode == "scan":
            elapsed = time.time() - t0
            emit({"t": "result", "ok": True,
                  "status": f"识别完成：共 {len(pages)} 页，耗时 {elapsed:.1f} 秒（结果尚未保存，请点击导出按钮输出）",
                  "md": full_md, "images": merged_images,
                  "pages": len(pages), "elapsed": round(elapsed, 1), "prunedResults": pruned_results})
            return

        # 仅 Word 模式才渲染 docx；html/json/zip_md 模式不渲染，避免无关的 Word 失败拖垮导出
        need_docx = mode == "docx"
        docx_path = out_dir / f"{stem}.docx"
        if need_docx:
            progress(0.8, "本地渲染 Word…")
            try:
                _md_to_docx_python(full_md, merged_images, str(docx_path))
            except Exception as e:
                if mode == "docx":
                    fail(f"生成 Word 失败：{e}")
                docx_path = None
                progress(0.85, f"Word 生成失败（{e}），已跳过 Word 导出")

        json_text = json.dumps([pg.get("prunedResult") for pg in pages], ensure_ascii=False, indent=2)

        elapsed = time.time() - t0
        if mode == "docx":
            if export_chart and chart_crops:
                zip_path = out_dir / f"{stem}_Word及图表.zip"
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    zf.writestr(f"{stem}.docx", docx_path.read_bytes())
                    _add_chart_crops(zf, chart_crops)
                emit({"t": "result", "ok": True,
                      "status": f"已导出 Word 及图表：{len(pages)} 页，耗时 {elapsed:.1f} 秒",
                      "md": full_md, "images": merged_images, "download": str(zip_path),
                      "pages": len(pages), "elapsed": round(elapsed, 1), "prunedResults": pruned_results})
                return
            emit({"t": "result", "ok": True,
                  "status": f"已导出 Word：{len(pages)} 页，耗时 {elapsed:.1f} 秒",
                  "md": full_md, "images": merged_images, "download": str(docx_path),
                  "pages": len(pages), "elapsed": round(elapsed, 1), "prunedResults": pruned_results})
            return
        if mode in ("html", "json", "zip_md"):
            suffix = MODE_SUFFIX[mode]
            zip_path = out_dir / f"{stem}_{suffix}.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                if mode == "html":
                    html_content = _md_to_html(full_md, merged_images)
                    if html_content:
                        zf.writestr(f"{stem}.html", html_content)
                    else:
                        fail("HTML 导出失败：缺少依赖（markdown/bs4/latex2mathml/wordrender）。")
                elif mode == "json":
                    zf.writestr(f"{stem}.json", json_text)
                elif mode == "zip_md":
                    zf.writestr(f"{stem}.md", _rewrite_md_image_refs(full_md, merged_images))
                if merged_images:
                    _write_images_zip(zf, merged_images)
                _add_chart_crops(zf, chart_crops)
            emit({"t": "result", "ok": True,
                  "status": f"已导出 {suffix} 结果包：共 {len(pages)} 页，耗时 {elapsed:.1f} 秒",
                  "md": full_md, "images": merged_images, "download": str(zip_path),
                  "pages": len(pages), "elapsed": round(elapsed, 1), "prunedResults": pruned_results})
            return
        # 扫描模式：zip（docx + md + json + imgs）
        zip_path = out_dir / f"{stem}_解析结果.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"{stem}.md", full_md)
            if docx_path:
                zf.writestr(f"{stem}.docx", docx_path.read_bytes())
            zf.writestr(f"{stem}.json", json_text)
            for rel, b64 in merged_images.items():
                zf.writestr(rel, base64.b64decode(b64))
            _add_chart_crops(zf, chart_crops)
        emit({"t": "result", "ok": True,
              "status": f"解析并合并完成：共 {len(pages)} 页 → 单个 Word，耗时 {elapsed:.1f} 秒",
              "md": full_md, "images": merged_images, "download": str(zip_path),
              "pages": len(pages), "elapsed": round(elapsed, 1), "prunedResults": pruned_results})
        return

    # ---------- 分支 B：单张图片 / PDF 每页 ----------
    progress(0.8, "整理结果…")
    md_parts, images = [], {}
    for pg in pages:
        md = pg.get("markdown") or {}
        md_parts.append((md.get("text") or "").strip().replace("\\n", "\n"))
        images.update(md.get("images") or {})
    full_md = "\n\n".join(md_parts) if pages else ""
    if not full_md.strip():
        fail("未识别到文字内容：请确认图片清晰且包含文字，或查看服务端日志。")
    json_text = json.dumps([pg.get("prunedResult") for pg in pages], ensure_ascii=False, indent=2)
    elapsed = time.time() - t0

    # 纯识别模式：不向 OCR 结果目录写入任何文件，仅返回结果供前端预览/缓存，
    # 用户点击导出按钮时才生成文件。
    if mode == "scan":
        emit({"t": "result", "ok": True,
              "status": f"识别完成：共 {len(pages)} 页，耗时 {elapsed:.1f} 秒（结果尚未保存，请点击导出按钮输出）",
              "md": full_md, "images": images,
              "pages": len(pages), "elapsed": round(elapsed, 1), "prunedResults": pruned_results})
        return

    if per_page and len(pages) > 1:
        progress(0.9, "按页拆分结果…")
        suffix = MODE_SUFFIX.get(mode, "")
        zip_path = out_dir / (f"{stem}_{suffix}每页.zip" if suffix else f"{stem}_每页结果.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, pg in enumerate(pages, 1):
                md = pg.get("markdown") or {}
                text = (md.get("text") or "").strip().replace("\\n", "\n")
                imgs = md.get("images") or {}
                pruned = pg.get("prunedResult")
                tag = f"page_{i:02d}"
                for rel, b64 in imgs.items():
                    zf.writestr(f"{tag}/images/{rel}", base64.b64decode(b64))
                if mode == "docx":
                    docx_path = out_dir / f"{tag}_{stem}.docx"
                    try:
                        _md_to_docx_python(text, imgs, str(docx_path), page_prefix=f"{tag}/")
                        zf.writestr(f"{tag}/{stem}_{i:02d}.docx", docx_path.read_bytes())
                    except Exception as e:
                        progress(0.9, f"第 {i} 页 Word 生成失败（{e}），已跳过该页 docx")
                elif mode == "html":
                    html_content = _md_to_html(text, imgs)
                    if html_content:
                        zf.writestr(f"{tag}/{stem}_{i:02d}.html", html_content)
                    else:
                        fail("HTML 导出失败：缺少依赖（markdown/bs4/latex2mathml/wordrender）。")
                elif mode == "json":
                    zf.writestr(f"{tag}/{stem}_{i:02d}.json", json.dumps(pruned, ensure_ascii=False, indent=2))
                elif mode == "zip_md":
                    zf.writestr(f"{tag}/{stem}_{i:02d}.md", _rewrite_md_image_refs(text, imgs))
            _add_chart_crops(zf, chart_crops)
        emit({"t": "result", "ok": True,
              "status": f"已按页导出 {suffix}：共 {len(pages)} 页，耗时 {elapsed:.1f} 秒",
              "md": full_md, "images": images, "download": str(zip_path),
              "pages": len(pages), "elapsed": round(elapsed, 1), "prunedResults": pruned_results})
        return

    # 单文件：docx / 各格式 zip
    docx_path = out_dir / f"{stem}.docx"
    if mode == "docx":
        try:
            _md_to_docx_python(full_md, images, str(docx_path))
        except Exception as e:
            fail(f"生成 Word 失败：{e}")

    if mode == "docx":
        if export_chart and chart_crops:
            zip_path = out_dir / f"{stem}_Word及图表.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(f"{stem}.docx", docx_path.read_bytes())
                _add_chart_crops(zf, chart_crops)
            emit({"t": "result", "ok": True,
                  "status": f"已导出 Word 及图表：1 页，耗时 {elapsed:.1f} 秒",
                  "md": full_md, "images": images, "download": str(zip_path),
                  "pages": len(pages), "elapsed": round(elapsed, 1), "prunedResults": pruned_results})
            return
        emit({"t": "result", "ok": True,
              "status": f"已导出 Word：1 页，耗时 {elapsed:.1f} 秒",
              "md": full_md, "images": images, "download": str(docx_path),
              "pages": len(pages), "elapsed": round(elapsed, 1), "prunedResults": pruned_results})
        return
    if mode in ("html", "json", "zip_md"):
        suffix = MODE_SUFFIX[mode]
        zip_path = out_dir / f"{stem}_{suffix}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            if mode == "html":
                html_content = _md_to_html(full_md, images)
                if html_content:
                    zf.writestr(f"{stem}.html", html_content)
                else:
                    fail("HTML 导出失败：缺少依赖（markdown/bs4/latex2mathml/wordrender）。")
            elif mode == "json":
                zf.writestr(f"{stem}.json", json_text)
            elif mode == "zip_md":
                zf.writestr(f"{stem}.md", _rewrite_md_image_refs(full_md, images))
            if images:
                _write_images_zip(zf, images)
            _add_chart_crops(zf, chart_crops)
        emit({"t": "result", "ok": True,
              "status": f"已导出 {suffix} 结果包：1 页，耗时 {elapsed:.1f} 秒",
              "md": full_md, "images": images, "download": str(zip_path),
              "pages": len(pages), "elapsed": round(elapsed, 1), "prunedResults": pruned_results})
        return
    # 扫描模式
    zip_path = out_dir / f"{stem}_解析结果.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{stem}.md", full_md)
        if docx_path and docx_path.exists():
            zf.writestr(f"{stem}.docx", docx_path.read_bytes())
        zf.writestr(f"{stem}.json", json_text)
        for rel, b64 in images.items():
            zf.writestr(rel, base64.b64decode(b64))
        _add_chart_crops(zf, chart_crops)
    status = f"解析完成：共 {len(pages)} 页，耗时 {elapsed:.1f} 秒"
    if not docx_path:
        status += "（Word 生成失败，已仅导出 Markdown/JSON）"
    emit({"t": "result", "ok": True,
          "status": status,
          "md": full_md, "images": images, "download": str(zip_path),
          "pages": len(pages), "elapsed": round(elapsed, 1), "prunedResults": pruned_results})


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        fail(f"{type(e).__name__}: {e}")
