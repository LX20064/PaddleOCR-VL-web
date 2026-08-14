#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PaddleOCR-VL 文档解析网页界面（Web UI + MCP 双模式）
======================================================
前端：Gradio（本脚本，默认 7860 端口）
后端：paddlex --serve 启动的 PaddleOCR-VL 服务（默认 127.0.0.1:8080）

功能：
  - 上传图片（jpg/png/bmp/tiff/webp）或 PDF，返回 Markdown 预览
  - PDF 自动合并为单个 .docx 直接下载（默认开启，可在管理后台改默认）
  - 可选按页导出 Word(.docx)、导出结构化 JSON；含附加内容自动打包 .zip
  - 【临时产线覆盖】扫描页面可临时覆盖产线参数，仅对当前这一次扫描生效
  - 请求排队（队列）；实时把进度与排队计数写入 logs/status.json 供管理后台展示
  - 服务软开关：总开关 / 网页子开关 / MCP 子开关（管理后台控制，即时生效）
  - 内置 MCP 服务器（受 MCP 子开关单独控制）：
    http://<IP>:7860/gradio_api/mcp/ ，工具 paddleocr_vl 与官方 PaddleOCR MCP
    协议兼容（签名 input_data / output_mode / file_type / return_images /
    runtime_params，官方 22 键 runtime_params 全部接受）。
    Agent 调用时文件可传 HTTP(S) URL（公网/内网/本机均可，由本进程下载）、
    服务器本机绝对路径或 base64 data URI。

开关与默认值来源：web_config.json（由管理后台 admin.py 维护）。
环境变量（可选）：
  PADDLEOCR_API_URL / PADDLEOCR_API_TIMEOUT   优先于配置文件
  GRADIO_SERVER_NAME / GRADIO_SERVER_PORT     默认 0.0.0.0:7860
  GRADIO_MCP_SERVER=False                     彻底关闭内置 MCP 服务器
"""

import base64
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path

# 完全离线本地部署：关闭 Gradio 遥测与版本检查，避免启动时访问外网
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

import gradio as gr
import markdown as _markdown
import requests
from bs4 import BeautifulSoup

from web_common import (
    get_api_url,
    get_restructure_url,
    get_switches,
    get_timeout,
    load_config,
    status_update,
)

# 结果输出根目录：使用项目内持久目录而非 /tmp，
# 避免系统清理临时文件导致生成的 docx/zip 在下载前被删除
_OUTPUT_ROOT = Path(__file__).resolve().parent / "outputs"
_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)


def _clean_old_outputs(max_age_days: int | None = None):
    """清理超过保留天数的历史输出目录，避免持久目录无限累积。

    保留天数默认取管理后台「默认设置 → 缓存文件设置」中的配置。
    """
    if max_age_days is None:
        max_age_days = int(load_config()["defaults"].get("cache_keep_days", 3))
    try:
        cutoff = time.time() - max_age_days * 86400
        # .previews 下的任务预览目录也按 mtime 清理：仅遍历 outputs 一级目录时，
        # .previews 因常被写入而 mtime 常新，其内部旧预览目录永远不会被清理。
        for root in (_OUTPUT_ROOT, _OUTPUT_ROOT / ".previews"):
            if not root.is_dir():
                continue
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                try:
                    mtime = child.stat().st_mtime
                except OSError:
                    continue
                if mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
    except OSError:
        pass

def _image_data_uri(b64: str) -> str:
    """根据 base64 图片内容嗅探 MIME 类型，返回 data URI。

    后端返回的 markdown.images 可能是 JPEG（以 /9j/ 开头）而非 PNG，
    若一律写成 data:image/png 会导致浏览器无法解码渲染。
    """
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


def _render_pdf_pages(pdf_path: str) -> list[str]:
    """用 pdftoppm 渲染 PDF 各页为 PNG 预览图，返回路径列表（供「原图」标签页展示）。

    PDF 本身不是图片，后端 API 只返回文档内的裁剪图（印章/表格/图表），
    不含完整页面渲染图；因此这里用系统 poppler 的 pdftoppm 渲染页面。
    失败或非 PDF 时返回空列表（此时「原图」标签页保持空白属预期）。
    """
    import subprocess
    pdf = Path(pdf_path)
    if pdf.suffix.lower() != ".pdf" or not pdf.exists():
        return []
    preview_dir = _OUTPUT_ROOT / ".previews" / f"{pdf.stem}_{uuid.uuid4().hex[:6]}"
    preview_dir.mkdir(parents=True, exist_ok=True)
    prefix = preview_dir / "page"
    try:
        subprocess.run(
            ["pdftoppm", "-png", "-r", "120", str(pdf), str(prefix)],
            check=True, capture_output=True, timeout=180,
        )
    except Exception:
        return []
    pages = sorted(preview_dir.glob("page-*.png"))
    return [str(p) for p in pages]


def _crop_chart_images(pages, source_path, file_type):
    """从原始图片 / PDF 渲染图中裁剪 chart 区域，返回 {文件名: PNG bytes}。

    chart 块在 prunedResult.parsing_res_list 中以 block_label=="chart" 出现，
    只有坐标没有独立图片；这里用 Pillow 按 block_bbox 归一化坐标裁剪。
    """
    crops = {}
    try:
        from PIL import Image
    except Exception:
        return crops

    page_imgs = []
    if file_type == 1:
        try:
            page_imgs = [Image.open(source_path)]
        except Exception:
            return crops
    else:
        for p in _render_pdf_pages(source_path):
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
                x1 = int(x1 / width * sw)
                y1 = int(y1 / height * sh)
                x2 = int(x2 / width * sw)
                y2 = int(y2 / height * sh)
            x1 = max(0, min(int(x1), sw))
            x2 = max(0, min(int(x2), sw))
            y1 = max(0, min(int(y1), sh))
            y2 = max(0, min(int(y2), sh))
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


def _add_chart_crops(zf, chart_crops):
    """把裁剪出的图表图片写入 zip 的 charts/ 目录。"""
    for name, data in chart_crops.items():
        zf.writestr(f"charts/{name}", data)


def _find_image_b64(src: str, images: dict):
    """在 images 字典中查找与 src 对应的 base64 图片数据。

    支持精确匹配、后缀/前缀匹配、文件名匹配，以兼容：
    - 每页拆分模式下 images 键被加上 page_XX/ 前缀（如 page_01/imgs/0.jpg）
    - 合并模式下 /restructure-pages 可能修改键名
    """
    if not src or not images:
        return None
    if src in images:
        return images[src]
    src_name = Path(src).name
    for k, v in images.items():
        if k.endswith(src) or src.endswith(k) or Path(k).name == src_name:
            return v
    return None


def _inline_md_images(md_text: str, images: dict) -> str:
    """把 Markdown 中 <img src="相对路径"> 的 src 替换为 base64 data URI。

    用于 gr.Markdown「Markdown」页签：原始文本里的图片是相对路径（如 imgs/x.jpg），
    网页端无法解析该相对地址，必须内嵌为 data URI 才能显示。
    """
    if not md_text or not images:
        return md_text

    def _repl(m):
        src = m.group(1)
        b64 = _find_image_b64(src, images)
        if b64 is not None:
            return f'src="{_image_data_uri(b64)}"'
        return m.group(0)

    return re.sub(r'src="([^"]+)"', _repl, md_text)


def _md_to_preview_html(md_text: str, images: dict) -> str:
    """将 Markdown 渲染为 HTML 片段，图片内嵌为 base64 data URI（供识别结果预览）。"""
    if not md_text:
        return ""
    html = _markdown.markdown(md_text, extensions=["tables", "fenced_code"])
    soup = BeautifulSoup(html, "html.parser")
    for img_tag in soup.find_all("img"):
        src = img_tag.get("src") or ""
        b64 = _find_image_b64(src, images)
        if b64 is not None:
            img_tag["src"] = _image_data_uri(b64)
    for table in soup.find_all("table"):
        table["border"] = "1"
        table["cellpadding"] = "6"
        table["cellspacing"] = "0"
        table["style"] = "border-collapse: collapse; width: 100%;"
    return str(soup)


# ---- wordrender 纯 Python 渲染（离线，无需 pandoc 二进制） ----
try:
    import wordrender

    _WORDRENDER_AVAILABLE = wordrender.dependencies_available()
except Exception:
    _WORDRENDER_AVAILABLE = False


def _md_to_docx_python(
    md_text: str, images: dict, out_path: str, page_prefix: str = ""
) -> str:
    """用 wordrender 纯 Python 方案把 Markdown 渲染为 DOCX。

    wordrender 按 base_dir 解析相对路径图片，因此需把图片按
    Markdown 中引用的相对路径写入临时目录，再交给 write_docx。
    """
    base_dir = Path(tempfile.mkdtemp(prefix="paddleocr_wr_"))
    try:
        processed_md = md_text
        for rel_path, b64 in images.items():
            # 保持相对路径结构，仅做页前缀去重与非法字符清洗
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

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}

# 临时覆盖项的"关闭"占位值（仍用于版面分析 / 跨栏合并）
LAYOUT_MODE_OFF = "本次关闭版面分析"
MERGE_BLOCKS_OFF = "本次关闭跨栏合并"

# 从配置文件读取功能开关默认值，作为页面临时开关的初始值
_cfg_defaults = load_config()["defaults"]


# ==================== 服务开关门控 ====================

def _ensure_enabled(channel: str) -> None:
    """channel: 'web'（网页）或 'mcp'。开关关闭时拒绝请求。"""
    sw = get_switches()
    if not sw["scan_service"]:
        raise gr.Error(
            "⛔ 扫描服务未开启：请管理员登录管理后台（7861 端口）"
            "打开「总扫描服务」开关后再使用。"
        )
    if channel == "web" and not sw["web_ui"]:
        raise gr.Error("⛔ 网页服务已被管理员关闭（MCP 入口可能仍可用）。")
    if channel == "mcp" and not sw["mcp"]:
        raise gr.Error("⛔ MCP 服务已被管理员关闭（网页入口可能仍可用）。")


# ==================== 核心解析逻辑 ====================

def _parse_core(
    file_path,
    max_pixels,
    # ---- 临时产线覆盖（二态开关，初始为后端默认值） ----
    ov_seal, ov_chart, ov_orientation, ov_unwarping,
    ov_ocr_image_block, ov_format_block,
    ov_layout_mode, ov_prompt_label, ov_merge_blocks,
    ov_layout_threshold, ov_min_pixels, ov_max_new_tokens,
    ov_temperature, ov_top_p, ov_repetition_penalty,
    export_mode=None,
    per_page=False,
    export_chart=False,
):  # None=扫描, "docx"=直接下载单个docx, "html"=zip+html, "json"=zip+json, "zip_md"=zip+md
    """核心解析逻辑（生成器）。

    export_mode:
      None  → 扫描模式，默认生成 DOCX + MD + JSON，有额外内容时打包 zip
      "docx" → 导出 Word 模式，直接返回单个 .docx 文件
      "html" → 导出 HTML 模式，返回 zip（MD + HTML + imgs/）
      "json" → 导出 JSON 模式，返回 zip（MD + JSON + imgs/）
      "zip_md" → 导出 Markdown 原始文件，返回 zip（MD + imgs/）
    """

    def _prog(frac, desc):
        status_update({"progress": frac, "desc": desc})

    def _status_yield(frac, desc):
        # 第二项携带进度条 HTML，批量入口据此实时刷新进度
        return (gr.update(value=f"⏳ {desc}"),
                gr.update(value=_progress_html(frac)), gr.update())

    if not file_path:
        raise gr.Error("请先上传图片或 PDF 文件")

    path = Path(file_path)
    ext = path.suffix.lower()
    if ext == ".pdf":
        file_type = 0
    elif ext in IMAGE_EXTS:
        file_type = 1
    else:
        raise gr.Error(f"不支持的文件类型：{ext or '(无扩展名)'}，请上传图片或 PDF")

    api_url = get_api_url()
    timeout = get_timeout()

    # PDF 默认合并为单个 Word；勾选「每页输出单独文件」时按页拆分
    merge_mode = (file_type == 0) and not per_page

    # Word 引擎：纯使用 wordrender（纯 Python 离线渲染）
    if not _WORDRENDER_AVAILABLE:
        raise gr.Error("wordrender 依赖不可用，无法生成 Word 文档。请安装 wordrender。")
    use_wordrender = True

    # ---------- 构造请求 ----------
    _prog(0.05, "读取并编码文件…")
    yield _status_yield(0.05, "读取并编码文件…")
    file_b64 = base64.b64encode(path.read_bytes()).decode("ascii")

    payload = {
        "file": file_b64,
        "fileType": file_type,
        # 功能开关直接采用临时二态开关的值
        "useDocOrientationClassify": ov_orientation,
        "useDocUnwarping": ov_unwarping,
        "useSealRecognition": ov_seal,
        "useChartRecognition": ov_chart,
        "useOcrForImageBlock": ov_ocr_image_block,
        "formatBlockContent": ov_format_block,
        "useLayoutDetection": ov_layout_mode,
        "mergeLayoutBlocks": ov_merge_blocks,
        # 官方 /layout-parsing schema 字段（默认即为 True）：显式声明，确保
        # 「渲染」预览所需的 markdown.images 图片 base64 能随结果返回
        "returnMarkdownImages": True,
    }
    if max_pixels and int(max_pixels) > 0:
        payload["maxPixels"] = int(max_pixels)
    # wordrender 本地生成模式：始终本地生成 docx，不从服务端请求 docx
    local_docx = True

    # 单图识别 prompt_label
    if not ov_layout_mode and ov_prompt_label:
        payload["promptLabel"] = ov_prompt_label

    # 数值型临时覆盖
    if ov_layout_threshold is not None:
        if not (0 <= float(ov_layout_threshold) <= 1):
            raise gr.Error("版面检测阈值须在 0~1 之间")
        payload["layoutThreshold"] = float(ov_layout_threshold)
    if ov_min_pixels:
        payload["minPixels"] = int(ov_min_pixels)
    if ov_max_new_tokens:
        payload["maxNewTokens"] = int(ov_max_new_tokens)
    if ov_temperature is not None:
        if float(ov_temperature) < 0:
            raise gr.Error("temperature 不能为负数")
        payload["temperature"] = float(ov_temperature)
    if ov_top_p is not None:
        if not (0 < float(ov_top_p) <= 1):
            raise gr.Error("top_p 须在 (0, 1] 之间")
        payload["topP"] = float(ov_top_p)
    if ov_repetition_penalty is not None:
        payload["repetitionPenalty"] = float(ov_repetition_penalty)
    # ---------- 调用后端服务 ----------
    _prog(0.2, "服务器解析中（大图 / 多页 PDF 可能需要几分钟）…")
    yield _status_yield(0.2, "服务器解析中（大图 / 多页 PDF 可能需要几分钟）…")
    t0 = time.time()
    try:
        resp = _http_post(api_url, payload, timeout)
    except requests.ConnectionError:
        raise gr.Error(
            f"无法连接 OCR 服务：{api_url}\n"
            "请确认 paddlex --serve 已在服务器上启动（参考 README 第 4 步）。"
        )
    except requests.Timeout:
        raise gr.Error(
            "请求超时：可能是文档页数过多或服务器繁忙。\n"
            "可减小服务端 max_num_input_imgs 页数上限，或在管理后台调大超时时间。"
        )

    if resp.status_code != 200:
        err_text = resp.text[:500]
        if "model settings are invalid" in err_text:
            raise gr.Error(
                "服务返回模型设置无效（HTTP 500）。\n"
                "服务端未加载文档预处理模型（方向分类 / 文本图像矫正依赖该模型）。\n"
                "请确认服务端 PaddleOCR-VL.yaml 中 use_doc_preprocessor 为 True 并已重启 API 服务，"
                "或取消勾选「文档方向分类」和「文本图像矫正」后重试。"
            )
        raise gr.Error(f"服务返回 HTTP {resp.status_code}：{err_text}")
    data = resp.json()
    if data.get("errorCode") != 0:
        raise gr.Error(f"解析失败：{data.get('errorMsg')}")

    pages = data["result"]["layoutParsingResults"]
    stem = path.stem or "result"
    # 每个任务一个独立子目录，避免同名文件互相覆盖；目录建在项目内而非 /tmp
    out_dir = _OUTPUT_ROOT / f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    out_dir.mkdir(parents=True, exist_ok=True)
    _clean_old_outputs()

    # 勾选「导出图表区域为图片」时，从原图/PDF 渲染图裁剪 chart 区域
    chart_crops = {}
    if export_chart:
        chart_crops = _crop_chart_images(pages, path, file_type)

    # ========== 分支 A：PDF 自动合并为单个 Word ==========
    if merge_mode:
        _prog(0.6, "合并多页结果…")
        yield _status_yield(0.6, "合并多页结果…")
        rp_payload = {
            "pages": [
                {
                    "prunedResult": p.get("prunedResult"),
                    "markdownImages": (p.get("markdown") or {}).get("images"),
                }
                for p in pages
            ],
            "mergeTables": True,
            "relevelTitles": True,
            "concatenatePages": True,
        }

        try:
            rp = _http_post(get_restructure_url(), rp_payload, timeout)
        except (requests.ConnectionError, requests.Timeout):
            raise gr.Error("调用 /restructure-pages 合并服务失败，请查看服务端日志。")
        rp_data = rp.json()
        if rp.status_code != 200 or rp_data.get("errorCode") != 0:
            raise gr.Error(f"合并 Word 失败：{rp_data.get('errorMsg', rp.text[:300])}")

        merged = rp_data["result"]["layoutParsingResults"][0]
        full_md = ((merged.get("markdown") or {}).get("text") or "").strip().replace("\\n", "\n")
        merged_images = (merged.get("markdown") or {}).get("images") or {}

        # 生成 DOCX
        _prog(0.8, "wordrender 本地渲染 Word…")
        yield _status_yield(0.8, "wordrender 本地渲染 Word…")
        docx_path = out_dir / f"{stem}.docx"
        source_label = "wordrender 本地" + _md_to_docx_python(full_md, merged_images, str(docx_path))

        # —— 扫码模式：生成 MD + JSON + DOCX，写入磁盘供重复下载 ——
        md_path = out_dir / f"{stem}.md"
        md_path.write_text(full_md, encoding="utf-8")
        json_text = json.dumps(
            [p.get("prunedResult") for p in pages], ensure_ascii=False, indent=2
        )
        json_path = out_dir / f"{stem}.json"
        json_path.write_text(json_text, encoding="utf-8")

        # HTML（仅在需要时生成）
        html_content = None
        if export_mode == "html":
            _prog(0.9, "生成结构化 HTML…")
            yield _status_yield(0.9, "生成结构化 HTML…")
            html_content = _markdown.markdown(full_md, extensions=["tables", "fenced_code"])
            soup = BeautifulSoup(html_content, "html.parser")
            for img_tag in soup.find_all("img"):
                src = img_tag.get("src") or ""
                if src in merged_images:
                    img_tag["src"] = f"data:image/png;base64,{merged_images[src]}"
            for table in soup.find_all("table"):
                table["border"] = "1"; table["cellpadding"] = "6"; table["cellspacing"] = "0"
                table["style"] = "border-collapse: collapse; width: 100%;"
            html_content = (
                "<!DOCTYPE html>\n"
                '<html lang="zh-CN">\n<head>\n  <meta charset="UTF-8">\n'
                "  <title>解析结果</title>\n  <style>\n"
                "    body { font-family: 'Times New Roman', '宋体', serif; max-width: 960px; margin: 40px auto; padding: 0 20px; line-height: 1.6; }\n"
                "    img { max-width: 100%; height: auto; display: block; margin: 12px auto; }\n"
                "    table { border-collapse: collapse; margin: 12px 0; }\n"
                "    th, td { border: 1px solid #ccc; padding: 6px 10px; text-align: center; }\n"
                "    th { background: #f5f5f5; }\n"
                "  </style>\n</head>\n<body>\n"
                f"{soup}\n</body>\n</html>"
            )

        # 封装输出
        elapsed = time.time() - t0
        if export_mode == "docx":
            if export_chart and chart_crops:
                zip_path = out_dir / f"{stem}_Word及图表.zip"
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    zf.writestr(f"{stem}.docx", docx_path.read_bytes())
                    _add_chart_crops(zf, chart_crops)
                status = f"✅ 已导出 Word 及图表：{len(pages)} 页，耗时 {elapsed:.1f} 秒"
                yield status, full_md or "(无文本内容)", str(zip_path), merged_images
                return
            status = f"✅ 已导出 Word（{source_label}）：{len(pages)} 页，耗时 {elapsed:.1f} 秒"
            yield status, full_md or "(无文本内容)", str(docx_path), merged_images
            return
        elif export_mode in ("html", "json", "zip_md"):
            zip_path = out_dir / f"{stem}_解析结果.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(f"{stem}.md", full_md)
                # 图片写入 imgs/ 子目录
                for rel_path, b64 in merged_images.items():
                    zf.writestr(rel_path, base64.b64decode(b64))
                if export_mode == "html" and html_content:
                    zf.writestr(f"{stem}.html", html_content)
                elif export_mode == "json" and json_text:
                    zf.writestr(f"{stem}.json", json_text)
                _add_chart_crops(zf, chart_crops)
            labels = {"html": "HTML", "json": "JSON", "zip_md": "Markdown"}
            status = f"✅ 已导出 {labels[export_mode]} 结果包：共 {len(pages)} 页，耗时 {elapsed:.1f} 秒"
            yield status, full_md or "(无文本内容)", str(zip_path), merged_images
            return
        else:
            # 扫描模式：返回 zip（含 DOCX + MD + JSON + imgs/）
            zip_path = out_dir / f"{stem}_解析结果.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(f"{stem}.md", full_md)
                zf.writestr(f"{stem}.docx", docx_path.read_bytes())
                zf.writestr(f"{stem}.json", json_text)
                for rel_path, b64 in merged_images.items():
                    zf.writestr(rel_path, base64.b64decode(b64))
                _add_chart_crops(zf, chart_crops)
            status = f"✅ 解析并合并完成：共 {len(pages)} 页 → 单个 Word（{source_label}），耗时 {elapsed:.1f} 秒"
            yield status, full_md or "(无文本内容)", str(zip_path), merged_images
            return

    # ========== 分支 C：PDF 每页输出单独文件 ==========
    if per_page and len(pages) > 1:
        _prog(0.7, "按页拆分结果…")
        yield _status_yield(0.7, "按页拆分结果…")
        zip_path = out_dir / f"{stem}_每页结果.zip"
        all_images = {}
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, page in enumerate(pages, 1):
                md = page.get("markdown") or {}
                text = (md.get("text") or "").strip().replace("\\n", "\n")
                imgs = md.get("images") or {}
                pruned = page.get("prunedResult")
                tag = f"page_{i:02d}"
                # 图片写入该页 imgs/ 子目录，避免不同页同名图片冲突
                for rel, b64 in imgs.items():
                    all_images[f"{tag}/{rel}"] = b64
                    zf.writestr(f"{tag}/{rel}", base64.b64decode(b64))
                zf.writestr(f"{tag}/{stem}_{i:02d}.md", text)
                if export_mode == "docx":
                    docx_path = out_dir / f"{tag}_{stem}.docx"
                    _md_to_docx_python(text, imgs, str(docx_path), page_prefix=f"{tag}/")
                    zf.writestr(f"{tag}/{stem}_{i:02d}.docx", docx_path.read_bytes())
                elif export_mode == "html":
                    html_content = _markdown.markdown(text, extensions=["tables", "fenced_code"])
                    soup = BeautifulSoup(html_content, "html.parser")
                    for img_tag in soup.find_all("img"):
                        src = img_tag.get("src") or ""
                        if src in imgs:
                            img_tag["src"] = f"data:image/png;base64,{imgs[src]}"
                    for table in soup.find_all("table"):
                        table["border"] = "1"; table["cellpadding"] = "6"; table["cellspacing"] = "0"
                        table["style"] = "border-collapse: collapse; width: 100%;"
                    html_full = (
                        "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n"
                        "  <meta charset=\"UTF-8\">\n  <title>解析结果</title>\n"
                        "  <style>\n"
                        "    body { font-family: 'Times New Roman', '宋体', serif; max-width: 960px; margin: 40px auto; padding: 0 20px; line-height: 1.6; }\n"
                        "    img { max-width: 100%; height: auto; display: block; margin: 12px auto; }\n"
                        "    table { border-collapse: collapse; margin: 12px 0; }\n"
                        "    th, td { border: 1px solid #ccc; padding: 6px 10px; text-align: center; }\n"
                        "    th { background: #f5f5f5; }\n"
                        "  </style>\n</head>\n<body>\n"
                        f"{soup}\n</body>\n</html>"
                    )
                    zf.writestr(f"{tag}/{stem}_{i:02d}.html", html_full)
                elif export_mode == "json":
                    zf.writestr(f"{tag}/{stem}_{i:02d}.json",
                                json.dumps(pruned, ensure_ascii=False, indent=2))
            _add_chart_crops(zf, chart_crops)
        full_md = "\n\n---\n\n".join(
            ((p.get("markdown") or {}).get("text") or "").strip().replace("\\n", "\n")
            for p in pages
        )
        elapsed = time.time() - t0
        if export_mode == "docx":
            status = f"✅ 已按页导出 Word：共 {len(pages)} 页（每页一个 .docx），耗时 {elapsed:.1f} 秒"
        elif export_mode in ("html", "json", "zip_md"):
            labels = {"html": "HTML", "json": "JSON", "zip_md": "Markdown"}
            status = f"✅ 已按页导出 {labels[export_mode]}：共 {len(pages)} 页，耗时 {elapsed:.1f} 秒"
        else:
            status = f"✅ 已按页拆分：共 {len(pages)} 页（每页一个文件），耗时 {elapsed:.1f} 秒"
        yield status, full_md, str(zip_path), all_images
        return

    # ========== 分支 B：单张图片模式 ==========
    _prog(0.8, "整理结果…")
    yield _status_yield(0.8, "整理结果…")
    md_parts = []
    images = {}
    for i, page in enumerate(pages):
        md = page.get("markdown") or {}
        text = (md.get("text") or "").strip().replace("\\n", "\n")
        md_parts.append(text)
        for rel_path, b64 in (md.get("images") or {}).items():
            images[rel_path] = b64

    full_md = "\n\n".join(md_parts) if pages else ""
    if not full_md.strip():
        raise gr.Error("服务未返回有效的 Markdown 内容，请查看服务端日志。")

    # 生成 DOCX（wordrender 纯 Python 离线渲染）
    docx_path = out_dir / f"{stem}.docx"
    source_label = "wordrender 本地" + _md_to_docx_python(full_md, images, str(docx_path))

    # —— 写入 MD + JSON 到磁盘供重复下载 ——
    md_path = out_dir / f"{stem}.md"
    md_path.write_text(full_md, encoding="utf-8")
    json_text = json.dumps(
        [p.get("prunedResult") for p in pages], ensure_ascii=False, indent=2
    )
    json_path = out_dir / f"{stem}.json"
    json_path.write_text(json_text, encoding="utf-8")

    # 封装输出
    elapsed = time.time() - t0
    if export_mode == "docx" and docx_path:
        if export_chart and chart_crops:
            zip_path = out_dir / f"{stem}_Word及图表.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(f"{stem}.docx", docx_path.read_bytes())
                _add_chart_crops(zf, chart_crops)
            status = f"✅ 已导出 Word 及图表：1 页，耗时 {elapsed:.1f} 秒"
            yield status, full_md or "(无文本内容)", str(zip_path), images
            return
        status = f"✅ 已导出 Word（{source_label}）：1 页，耗时 {elapsed:.1f} 秒"
        yield status, full_md or "(无文本内容)", str(docx_path), images
        return
    elif export_mode in ("html", "json", "zip_md"):
        zip_path = out_dir / f"{stem}_解析结果.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"{stem}.md", full_md)
            for rel_path, b64 in images.items():
                zf.writestr(rel_path, base64.b64decode(b64))
            if export_mode == "html":
                # 生成 HTML（单图版）
                html_content = _markdown.markdown(full_md, extensions=["tables", "fenced_code"])
                soup = BeautifulSoup(html_content, "html.parser")
                for img_tag in soup.find_all("img"):
                    src = img_tag.get("src") or ""
                    if src in images:
                        img_tag["src"] = f"data:image/png;base64,{images[src]}"
                for table in soup.find_all("table"):
                    table["border"] = "1"; table["cellpadding"] = "6"; table["cellspacing"] = "0"
                    table["style"] = "border-collapse: collapse; width: 100%;"
                html_full = (
                    "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n"
                    "  <meta charset=\"UTF-8\">\n  <title>解析结果</title>\n"
                    "  <style>\n"
                    "    body { font-family: 'Times New Roman', '宋体', serif; max-width: 960px; margin: 40px auto; padding: 0 20px; line-height: 1.6; }\n"
                    "    img { max-width: 100%; height: auto; display: block; margin: 12px auto; }\n"
                    "    table { border-collapse: collapse; margin: 12px 0; }\n"
                    "    th, td { border: 1px solid #ccc; padding: 6px 10px; text-align: center; }\n"
                    "    th { background: #f5f5f5; }\n"
                    "  </style>\n</head>\n<body>\n"
                    f"{soup}\n</body>\n</html>"
                )
                zf.writestr(f"{stem}.html", html_full)
            elif export_mode == "json":
                zf.writestr(f"{stem}.json", json_text)
            _add_chart_crops(zf, chart_crops)
        labels = {"html": "HTML", "json": "JSON", "zip_md": "Markdown"}
        status = f"✅ 已导出 {labels[export_mode]} 结果包：1 页，耗时 {elapsed:.1f} 秒"
        yield status, full_md or "(无文本内容)", str(zip_path), images
        return
    else:
        # 扫描模式：返回 zip（含 DOCX + MD + JSON + imgs/）
        zip_path = out_dir / f"{stem}_解析结果.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"{stem}.md", full_md)
            if docx_path and docx_path.exists():
                zf.writestr(f"{stem}.docx", docx_path.read_bytes())
            zf.writestr(f"{stem}.json", json_text)
            for rel_path, b64 in images.items():
                zf.writestr(rel_path, base64.b64decode(b64))
            _add_chart_crops(zf, chart_crops)
        status = f"✅ 解析完成：共 {len(pages)} 页，耗时 {elapsed:.1f} 秒"
        yield status, full_md, str(zip_path), images


# ==================== 入口包装（门控 + 状态上报） ====================

def _err_text(e):
    """提取异常的可读消息：gr.Error.__str__ 返回 repr(message)（带单引号），故取其 message。"""
    return e.message if isinstance(e, gr.Error) else str(e)


# ==================== 停止支持 ====================
# 全局停止标志：批量处理 / 重新导出 / MCP 每个入口开始时清零，
# 仅「停止」按钮（_stop_batch）置位。进行中的 HTTP 请求由 _http_post 轮询检测。
_stop_event = threading.Event()


class _UserStopped(Exception):
    """用户在处理过程中点击了「停止」。"""


def _http_post(url, payload, timeout):
    """可被「停止」打断的 requests.post：请求在守护线程中执行，主线程每 0.5s
    检查一次停止标志；点击停止后立即放弃等待并抛 _UserStopped。
    注意：服务端已接收的解析任务无法取消，会自行跑完（其响应被丢弃，
    守护线程随之结束），但客户端与批量循环得以及时退出。"""
    box = {}

    def _call():
        try:
            box["resp"] = requests.post(url, json=payload, timeout=timeout)
        except Exception as e:  # 原样转交主线程，保持既有异常分类（连接/超时等）
            box["err"] = e

    t = threading.Thread(target=_call, daemon=True)
    t.start()
    while t.is_alive():
        if _stop_event.is_set():
            raise _UserStopped()
        t.join(0.5)
    if "err" in box:
        raise box["err"]
    return box["resp"]


def _guarded_entry(channel, *args):
    """统一入口（生成器）：开关门控 → 状态计数 → 核心解析 → 状态收尾。
    发生异常时直接返回错误信息填充输出组件，避免 Gradio 在输出区显示「错误」红标。
    不依赖 gr.Progress，避免多输出组件产生多个进度条。"""
    file_name = Path(args[0]).name if args and args[0] else "(未选择文件)"

    try:
        _ensure_enabled(channel)
    except Exception as e:
        yield "⛔ 服务未开启", f"**解析未启动**：{_err_text(e)}", None, {}
        return

    # submitted 与 started 必须成对增长，否则 waiting=max(0, submitted-started) 恒为 0。
    # 批量任务的 submitted 由 _batch_run 按文件数统一 bump，此处不再重复；
    # MCP 调用不经批量，故在此同步 bump。
    bumps = {"started": 1}
    if channel == "mcp":
        bumps["submitted"] = 1
    print(f"[PaddleOCR-VL] 开始处理（{channel}）：{file_name}", flush=True)
    status_update(
        {"state": "busy", "file": file_name, "progress": 0.0, "desc": "开始处理"},
        bumps=bumps,
    )
    try:
        yield from _parse_core(*args)
    except _UserStopped:
        print(f"[PaddleOCR-VL] 用户停止（{channel}）：{file_name}", flush=True)
        status_update(
            {"state": "idle", "progress": 1.0, "desc": "最近任务：用户手动停止"},
            bumps={"done": 1, "failed": 1},
        )
        yield ("⏹ 已停止：用户手动停止",
               "**已停止**：用户手动停止（服务端已发出的解析会自行跑完）", None, {})
        return
    except Exception as e:
        print(f"[PaddleOCR-VL] 任务失败（{channel}）：{file_name} — {e}", flush=True)
        status_update(
            {"state": "idle", "progress": 1.0, "desc": f"最近任务失败：{_err_text(e)}"},
            bumps={"done": 1, "failed": 1},
        )
        err_md = f"**解析失败**：{_err_text(e)}"
        yield f"❌ 失败：{_err_text(e)}", err_md, None, {}
        return
    status_update(
        {"state": "idle", "progress": 1.0, "desc": f"最近完成：{file_name}"},
        bumps={"done": 1},
    )
    print(f"[PaddleOCR-VL] 完成（{channel}）：{file_name}", flush=True)


# ==================== 参数顺序转换（UI → 后端） ====================
# UI 组件的统一顺序（16 个核心参数），与 _parse_core 一一对应：
#   0 max_pixels(显存占用比)
#   1 seal | 2 chart | 3 orientation | 4 unwarping | 5 ocr_image_block | 6 format_block
#   7 layout_mode | 8 prompt_label | 9 merge_blocks
#   10 layout_threshold | 11 min_pixels | 12 max_new_tokens | 13 temperature
#   14 top_p | 15 repetition_penalty
#   导出模式（export_mode）不再由前端参数控制，而是各导出按钮直接传参
_OPT_NONE = "留空"
_VRAM_NONE = "不限制"


def _opt_int(v, none_marker=_OPT_NONE):
    return None if v in (None, none_marker, "") else int(float(v))


def _opt_float(v, none_marker=_OPT_NONE):
    return None if v in (None, none_marker, "") else float(v)


def _to_canonical(ui):
    """把 UI 顺序的 16 个核心参数转换为 _parse_core 需要的顺序。"""
    (vram, seal, chart, orient, unwarp,
     ocrblk, fmtblk, layout, prompt, merge, thr, minpix, ctx,
     temp, topp, rep) = ui
    return (
        _opt_int(vram, _VRAM_NONE) or 0,
        bool(seal), bool(chart), bool(orient), bool(unwarp),
        bool(ocrblk), bool(fmtblk), bool(layout),
        "" if prompt in (None, _OPT_NONE) else prompt,
        bool(merge),
        _opt_float(thr), _opt_int(minpix), _opt_int(ctx),
        _opt_float(temp), _opt_float(topp), _opt_float(rep),
    )


# ==================== 官方 PaddleOCR MCP 协议兼容层 ====================
# 官方 paddleocr_mcp（FastMCP）的 paddleocr_vl 工具：
#   paddleocr_vl(input_data, output_mode="simple", file_type=None,
#                return_images=True, runtime_params=None)
#   - input_data: 公网 URL / 服务器本机绝对路径 / base64 data URI
#   - output_mode: "simple"（纯 Markdown）| "detailed"（追加 "Pages: N"）
#   - file_type: 0=PDF，1=图片，None=自动判断
#   - return_images: True 时图片随结果返回（本项目以 data URI 内嵌进 Markdown）
#   - runtime_params: 官方 22 键（snake_case）产线参数，未给出的键取官方默认值
# 本实现保持工具名 / 参数名 / 返回格式与官方一致；Gradio 内置 MCP 无法像官方
# FastMCP 那样把 <img> 拆成独立的 ImageContent，故图片以 base64 data URI 内嵌
# 进 Markdown 文本（MCP 客户端渲染 Markdown 时可直接显示）。
_OFFICIAL_RUNTIME_KEYS = (
    "use_doc_orientation_classify", "use_doc_unwarping",
    "use_layout_detection", "use_chart_recognition", "use_seal_recognition",
    "use_ocr_for_image_block", "layout_threshold", "layout_nms",
    "layout_unclip_ratio", "layout_merge_bboxes_mode", "layout_shape_mode",
    "prompt_label", "format_block_content", "repetition_penalty",
    "temperature", "top_p", "min_pixels", "max_pixels", "max_new_tokens",
    "vlm_extra_args", "merge_layout_blocks", "markdown_ignore_labels",
)
# 官方未显式设置默认值的键由服务端决定（layout_nms / layout_unclip_ratio /
# layout_merge_bboxes_mode / layout_shape_mode / vlm_extra_args /
# markdown_ignore_labels 本项目未实现，一律忽略）；以下键按官方 params.py
# 与服务端惯例取默认。
_OFFICIAL_DEFAULT_PARAMS = {
    "use_doc_orientation_classify": False,
    "use_doc_unwarping": False,
    "use_chart_recognition": True,
    "use_seal_recognition": True,
    "use_layout_detection": True,
    "merge_layout_blocks": True,
}


def _mcp_runtime_to_params16(runtime_params):
    """官方 22 键 runtime_params（snake_case）→ 项目 16 参数元组（_parse_core 顺序）。

    runtime_params 支持 dict 或 JSON 字符串；未给出的键回落官方默认值（对齐官方
    行为），官方独有而本项目未实现的 6 个键（layout_nms 等）直接忽略。
    """
    rp = _OFFICIAL_DEFAULT_PARAMS.copy()
    if isinstance(runtime_params, str) and runtime_params.strip():
        try:
            runtime_params = json.loads(runtime_params)
        except (json.JSONDecodeError, TypeError):
            runtime_params = None
    if isinstance(runtime_params, dict):
        rp.update(
            {k: v for k, v in runtime_params.items() if k in _OFFICIAL_RUNTIME_KEYS}
        )
    return (
        int(rp.get("max_pixels", 0) or 0),                     # max_pixels
        bool(rp.get("use_seal_recognition", False)),           # ov_seal
        bool(rp.get("use_chart_recognition", True)),           # ov_chart
        bool(rp.get("use_doc_orientation_classify", False)),   # ov_orientation
        bool(rp.get("use_doc_unwarping", False)),              # ov_unwarping
        bool(rp.get("use_ocr_for_image_block", False)),        # ov_ocr_image_block
        bool(rp.get("format_block_content", False)),           # ov_format_block
        bool(rp.get("use_layout_detection", True)),            # ov_layout_mode
        (rp.get("prompt_label") or "").strip(),                # ov_prompt_label
        bool(rp.get("merge_layout_blocks", True)),             # ov_merge_blocks
        rp.get("layout_threshold"),                            # ov_layout_threshold
        rp.get("min_pixels"),                                  # ov_min_pixels
        rp.get("max_new_tokens"),                              # ov_max_new_tokens
        rp.get("temperature"),                                 # ov_temperature
        rp.get("top_p"),                                       # ov_top_p
        rp.get("repetition_penalty"),                          # ov_repetition_penalty
    )


def _mcp_input_to_file(input_data, file_type):
    """官方兼容的输入归一化 → (file_b64, file_type)。

    始终返回 base64 字符串（后端 /layout-parsing 仅接受 base64 形式的 file）：
      - HTTP(S) URL（公网/内网/本机均可）：由本进程下载后编码，不受 Gradio
        SSRF 防护限制（官方 self-hosted 的 file 同样支持 URL 形式）；
      - 服务器本机绝对路径：直接读取编码；
      - base64 data URI：提取 payload 部分；
      - 纯 base64：按文件头嗅探类型后原样返回。
    file_type 为 None 时按扩展名 / 内容嗅探自动判断（0=PDF，1=图片）。
    """
    s = str(input_data or "").strip()
    if not s:
        raise gr.Error("input_data 不能为空：请提供 HTTP(S) URL / 本机路径 / base64 data URI。")
    if s.startswith(("http://", "https://")):
        from urllib.parse import urlparse
        ext = os.path.splitext(urlparse(s).path)[1].lower()
        ft = 0 if ext == ".pdf" else (1 if ext in IMAGE_EXTS else None)
        if file_type is not None:
            ft = int(file_type)
        if ft is None:
            raise gr.Error("无法判断文件类型：请显式传 file_type（0=PDF，1=图片）。")
        try:
            r = requests.get(s, timeout=get_timeout())
        except requests.RequestException as e:
            raise gr.Error(f"下载失败：{s}\n原因：{e}")
        if r.status_code != 200:
            raise gr.Error(f"下载失败：HTTP {r.status_code}（{s}）")
        b64 = base64.b64encode(r.content).decode("ascii")
        return b64, ft
    if s.startswith("data:"):
        m = re.match(r"data:([^;,]+)", s)
        mime = m.group(1).lower() if m else ""
        b64 = s.split(",", 1)[1] if "," in s else ""
        ft = 0 if "pdf" in mime else 1
        if file_type is not None:
            ft = int(file_type)
        return b64, ft
    if os.path.isfile(s):
        ext = os.path.splitext(s)[1].lower()
        ft = 0 if ext == ".pdf" else (1 if ext in IMAGE_EXTS else None)
        if file_type is not None:
            ft = int(file_type)
        if ft is None:
            raise gr.Error(f"不支持的文件类型：{ext or '(无扩展名)'}，请上传图片或 PDF。")
        b64 = base64.b64encode(Path(s).read_bytes()).decode("ascii")
        return b64, ft
    # 纯 base64 兜底（无前缀）：按文件头嗅探
    ft = None
    try:
        head = base64.b64decode(s[:128]) if s else b""
        if head.startswith(b"%PDF"):
            ft = 0
        elif head.startswith((b"\xff\xd8", b"\x89PNG", b"GIF8", b"BM",
                              b"II*\x00", b"MM\x00*", b"RIFF")):
            ft = 1
    except Exception:
        pass
    if file_type is not None:
        ft = int(file_type)
    if ft is None:
        raise gr.Error("无法判断文件类型：请提供 base64 data URI 或显式传 file_type（0=PDF，1=图片）。")
    return s, ft


def _embed_mcp_images(markdown, images):
    """把 Markdown 中 <img src="相对路径"> 替换为 base64 data URI。

    官方实现把图片以独立 ImageContent 返回；Gradio 内置 MCP 无法交错输出文本与
    图片，故采用 data URI 内嵌，客户端渲染 Markdown 时可直接显示图片。
    """
    for rel_path, b64 in images.items():
        markdown = markdown.replace(f'src="{rel_path}"', f'src="{_image_data_uri(b64)}"')
    return markdown


def _parse_mcp(input_data, file_type, return_images, runtime_params):
    """官方协议的解析流程：门控 → 输入归一化 → 调用产线 API → 官方格式结果。

    返回 (markdown_text, page_count, images)：
      - markdown_text: 各页 Markdown 以 "\\n" 拼接（与官方一致，PDF 不做合并）
      - page_count: 页数（detailed 模式据此追加 "Pages: N"）
      - images: {相对路径: base64}，供 return_images=True 时内嵌 data URI
    """
    _ensure_enabled("mcp")
    _file_tag = str(input_data)[:60] or "(MCP)"
    print(f"[PaddleOCR-VL] MCP 开始处理：{_file_tag}", flush=True)
    status_update(
        {"state": "busy", "file": _file_tag, "progress": 0.0, "desc": "MCP 解析开始"},
        bumps={"submitted": 1, "started": 1},
    )
    try:
        payload_file, ft = _mcp_input_to_file(input_data, file_type)
        (max_pixels, seal, chart, orient, unwarp, ocrblk, fmtblk, layout,
         prompt, merge, thr, minpix, ctx, temp, topp, rep) = _mcp_runtime_to_params16(
            runtime_params
        )

        payload = {
            "file": payload_file,
            "fileType": ft,
            "useDocOrientationClassify": bool(orient),
            "useDocUnwarping": bool(unwarp),
            "useSealRecognition": bool(seal),
            "useChartRecognition": bool(chart),
            "useOcrForImageBlock": bool(ocrblk),
            "formatBlockContent": bool(fmtblk),
            "useLayoutDetection": bool(layout),
            "mergeLayoutBlocks": bool(merge),
            # 需要 markdown.images 图片 base64 随结果返回（渲染 / 内嵌依赖）
            "returnMarkdownImages": bool(return_images),
        }
        if max_pixels and int(max_pixels) > 0:
            payload["maxPixels"] = int(max_pixels)
        if not layout and prompt:
            payload["promptLabel"] = prompt
        if thr is not None:
            if not (0 <= float(thr) <= 1):
                raise gr.Error("layout_threshold 须在 0~1 之间")
            payload["layoutThreshold"] = float(thr)
        if minpix:
            payload["minPixels"] = int(minpix)
        if ctx:
            payload["maxNewTokens"] = int(ctx)
        if temp is not None:
            if float(temp) < 0:
                raise gr.Error("temperature 不能为负数")
            payload["temperature"] = float(temp)
        if topp is not None:
            if not (0 < float(topp) <= 1):
                raise gr.Error("top_p 须在 (0, 1] 之间")
            payload["topP"] = float(topp)
        if rep is not None:
            payload["repetitionPenalty"] = float(rep)

        api_url = get_api_url()
        try:
            resp = _http_post(api_url, payload, get_timeout())
        except requests.ConnectionError:
            raise gr.Error(
                f"无法连接 OCR 服务：{api_url}\n请确认 paddlex --serve 已在服务器上启动。"
            )
        except requests.Timeout:
            raise gr.Error("请求超时：可能是文档页数过多或服务器繁忙。")
        if resp.status_code != 200:
            err_text = resp.text[:500]
            if "model settings are invalid" in err_text:
                raise gr.Error(
                    "服务返回模型设置无效（HTTP 500）。\n服务端未加载文档预处理模型"
                    "（方向分类 / 文本图像矫正依赖该模型）。\n请确认服务端 "
                    "PaddleOCR-VL.yaml 中 use_doc_preprocessor 为 True 并已重启 API 服务，"
                    "或关闭 use_doc_orientation_classify / use_doc_unwarping 后重试。"
                )
            raise gr.Error(f"服务返回 HTTP {resp.status_code}：{err_text}")
        data = resp.json()
        if data.get("errorCode") != 0:
            raise gr.Error(f"解析失败：{data.get('errorMsg')}")

        # 与官方 http_result_parsers 一致：逐页取 markdown.text，以 "\n" 拼接（PDF 不合并）
        pages = data["result"]["layoutParsingResults"]
        md_parts, images = [], {}
        for p in pages:
            md = p.get("markdown") or {}
            md_parts.append((md.get("text") or "").strip().replace("\\n", "\n"))
            images.update(md.get("images") or {})
        markdown = "\n".join(md_parts)
        if not markdown.strip():
            markdown = "No document content detected"  # 与官方一致：返回提示文本而非报错
        status_update(
            {"state": "idle", "progress": 1.0, "desc": f"最近完成：{_file_tag}"},
            bumps={"done": 1},
        )
        print(f"[PaddleOCR-VL] MCP 完成：{_file_tag}", flush=True)
        return markdown, len(pages), images
    except Exception:
        status_update(
            {"state": "idle", "progress": 1.0, "desc": f"最近任务失败：{_file_tag}"},
            bumps={"done": 1, "failed": 1},
        )
        raise


def paddleocr_vl(input_data=None, file_path=None, output_mode="simple",
                 file_type=None, return_images=True, runtime_params=None):
    """使用 PaddleOCR-VL 对文档图像或 PDF 进行版面解析，返回 Markdown 文本。

    与官方 PaddleOCR MCP 的 paddleocr_vl 工具保持兼容（工具名、参数名、返回格式）。

    Args:
        input_data: 待解析的文档图像或 PDF 文件，支持三种形式（与官方一致）：
            1) HTTP(S) URL——公网 / 内网 / 本机地址均可（由本服务下载后解析，
               不受 Gradio SSRF 防护限制）；
            2) 服务器本机绝对路径，如 /home/user/scan.png；
            3) base64 data URI（data:image/png;base64,...）。
        file_path: 旧版参数名，与 input_data 等价（向后兼容，二选一即可）。
        output_mode: "simple" 仅返回 Markdown 文本；"detailed" 额外追加
            "Pages: N"（N 为解析得到的页数）。
        file_type: 0=PDF，1=图片；None 时按文件自动判断。
        return_images: 为 True 时 Markdown 中的图片以 base64 data URI 内嵌返回。
        runtime_params: 官方产线参数（snake_case 键，支持全部 22 个键），
            传 JSON 对象或 JSON 字符串，例如
            {"use_doc_orientation_classify": true, "layout_threshold": 0.5}。
            未给出的键取官方默认值。

    Returns:
        Markdown 文本；detailed 模式下末尾追加 "Pages: N"（N 为解析得到的页数）。
    """
    # MCP 入口用独立解析流程（对齐官方返回格式），不经网页的 _parse_core 生成器
    # 注：Gradio 内置 MCP 在排队（queue）模式下会把多输出组件打包成单个文本，
    # 故这里把 Markdown 与 "Pages: N" 合并为一个字符串返回（最终文本与官方
    # [markdown, "Pages: N"] 两个文本段拼接后的内容一致）。
    _stop_event.clear()   # MCP 调用与网页「停止」按钮无关，起始即清除残留标志
    src = input_data if input_data not in (None, "") else file_path
    markdown, page_count, images = _parse_mcp(
        src, file_type, return_images, runtime_params
    )
    if return_images and images:
        markdown = _embed_mcp_images(markdown, images)
    if output_mode == "detailed":
        markdown = f"{markdown}\n\nPages: {page_count}"
    return markdown


# ==================== 批量识别 / 界面辅助 ====================

def _get_gpu_text() -> str:
    """查询 GPU 型号与显存（顶部导航展示），失败时返回提示文本。"""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            text=True, timeout=8,
        ).strip()
        lines = [l for l in out.splitlines() if l.strip()]
        if not lines:
            return "GPU 信息不可用"
        name = lines[0].split(",")[0].strip()
        mem = lines[0].split(",")[-1].strip()
        m = re.search(r"(\d+)\s*(GiB|MiB)", mem)
        if m:
            gb = int(m.group(1)) // 1024 if m.group(2) == "MiB" else int(m.group(1))
            mem = f"{gb} GB"
        return f"{name} · {mem}"
    except Exception as e:
        print(f"[PaddleOCR-VL] _get_gpu_text failed: {type(e).__name__}: {e}", file=sys.stderr)
        return "GPU 信息不可用"


# ==================== 线性 SVG 图标 ====================

def _svg(inner: str, size: int = 16) -> str:
    """内联线性 SVG（stroke=currentColor，随文字颜色变化），用于顶部导航/标题栏。"""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round">{inner}</svg>'
    )


# Gradio 会把 icon= 的值当作相对路径并用应用目录拼接前缀，
# data URI 会被破坏成 "<应用目录>/data:image/..." 而无法加载。
# 因此把 SVG 写入应用目录内的真实文件，再传相对文件名给 icon= 参数。
_ICON_DIR = Path(__file__).resolve().parent / ".scan_icons"
_icon_files = {}


def _icon_url(inner: str, size: int = 16) -> str:
    """把线性 SVG 写入 .scan_icons/*.svg，返回相对路径文件名供 icon= 使用。

    页面上由 JS 将该 SVG 作为蒙版（mask）+ currentColor 背景着色，
    使图标随按钮文字颜色 / 悬停态变化。
    """
    if inner not in _icon_files:
        _ICON_DIR.mkdir(exist_ok=True)
        name = f"{len(_icon_files)}.svg"
        (_ICON_DIR / name).write_text(_svg(inner, size=size), encoding="utf-8")
        _icon_files[inner] = name
    return str(_ICON_DIR.relative_to(Path(__file__).resolve().parent) / _icon_files[inner])


# 图标内路径（lucide 风格线性图标）
_IP_SCAN = ('<path d="M3 7V5a2 2 0 0 1 2-2h2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/>'
            '<path d="M21 17v2a2 2 0 0 1-2 2h-2"/><path d="M7 21H5a2 2 0 0 1-2-2v-2"/>'
            '<line x1="7" y1="12" x2="17" y2="12"/>')
_IP_SETTINGS = ('<line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/>'
                '<line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/>'
                '<line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/>'
                '<line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/>'
                '<line x1="17" y1="16" x2="23" y2="16"/>')
_IP_FILE_PLUS = ('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'
                 '<polyline points="14 2 14 8 20 8"/><line x1="12" y1="18" x2="12" y2="12"/>'
                 '<line x1="9" y1="15" x2="15" y2="15"/>')
_IP_FOLDER_PLUS = ('<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>'
                   '<line x1="12" y1="11" x2="12" y2="17"/><line x1="9" y1="14" x2="15" y2="14"/>')
_IP_TRASH = ('<polyline points="3 6 5 6 21 6"/>'
             '<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'
             '<line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/>')
_IP_X = '<circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/>'
_IP_DOWNLOAD = ('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
                '<polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>')
_IP_FILE_TEXT = ('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'
                 '<polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/>'
                 '<line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/>')
_IP_CODE = '<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>'
_IP_COPY = ('<rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>'
            '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>')
_IP_MAXIMIZE = ('<path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/>'
                '<path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/>')
_IP_TERMINAL = ('<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>')
_IP_FOLDER = '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>'
_IP_FOLDER_OPEN = ('<path d="m6 14 1.5-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.54 6a2 2 0 0 1-1.95 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2a2 2 0 0 0 1.67.9H18a2 2 0 0 1 2 2v2"/>')
_IP_MORE = ('<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>')
_IP_BRACKET = ('<path d="M16 3h3v18h-3"/><path d="M8 21H5V3h3"/>')


BATCH_ALLOWED_EXTS = {".pdf"} | IMAGE_EXTS


def _norm_uploaded(paths, recursive):
    """过滤上传文件：仅保留支持的扩展名；非递归时丢弃子目录内文件。"""
    if not paths:
        return []
    paths = [p for p in paths if p and Path(p).suffix.lower() in BATCH_ALLOWED_EXTS]
    if recursive or len(paths) <= 1:
        return paths
    try:
        common = os.path.commonpath(paths)
    except ValueError:
        return paths
    out = []
    for p in paths:
        try:
            rel = Path(p).relative_to(common)
        except ValueError:
            rel = Path(p)
        if len(rel.parts) <= 1:
            out.append(p)
    return out or paths


def _new_queue_record(path: str) -> dict:
    return {"path": path, "name": Path(path).name, "pages": 0,
            "status": "等待", "elapsed": "", "time": 0.0, "download": None}


def _queue_table(queue):
    """任务队列表格：自渲染 HTML（等宽表头 + 文件名溢出省略 + 行状态配色）。

    返回完整 HTML 字符串；空队列返回空字符串。"""
    if not queue:
        return ""
    rows = []
    for i, r in enumerate(queue):
        status = str(r.get("status") or "")
        if "处理中" in status:
            cls = "st-run"
        elif "完成" in status:
            cls = "st-ok"
        elif "失败" in status or "已停止" in status:
            cls = "st-err"
        else:
            cls = "st-wait"
        name = html.escape(str(r.get("name") or ""))
        pages = html.escape(str(r.get("pages") or ""))
        elapsed = html.escape(str(r.get("elapsed") or ""))
        rows.append(
            f'<tr data-row="{i}" class="{cls}">'
            f'<td class="c-idx">{i + 1}</td>'
            f'<td class="c-name" title="{name}">{name}</td>'
            f'<td class="c-pages">{pages}</td>'
            f'<td class="c-status">{html.escape(status)}</td>'
            f'<td class="c-elapsed">{elapsed}</td>'
            f"</tr>"
        )
    return (
        '<table class="scan-qtable"><colgroup>'
        '<col class="w-idx"><col class="w-name"><col class="w-pages">'
        '<col class="w-status"><col class="w-elapsed"></colgroup>'
        '<thead><tr><th>#</th><th>文件</th><th>页数</th><th>状态</th><th>耗时</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
    )


def _file_count_md(queue):
    return f"共 **{len(queue)}** 个文件"


def _stats_html(queue):
    total = len(queue)
    done = sum(1 for r in queue if r["status"] == "完成")
    failed = sum(1 for r in queue if r["status"] in ("失败", "已停止"))
    secs = sum(float(r.get("time") or 0) for r in queue)
    pages = sum(int(r.get("pages") or 0) for r in queue)
    per_page = secs / pages if pages else 0.0

    def card(label, value, sub="", value_class=""):
        sub_html = f'<div class="scan-stat-sub">{sub}</div>' if sub else ""
        vcls = f"scan-stat-value {value_class}" if value_class else "scan-stat-value"
        return (f'<div class="scan-stat-card"><div class="scan-stat-label">{label}</div>'
                f'<div class="{vcls}">{value}</div>{sub_html}</div>')

    return ('<div class="scan-stat-grid">'
            + card("文件数", total)
            + card("完成数", done, "成功", "scan-stat-ok")
            + card("失败数", failed, "", "scan-stat-err")
            + card("耗时", f"{secs:.1f}s", f"共 {pages} 页 · 平均 {per_page:.2f}s/页")
            + "</div>")


def _progress_html(fraction):
    pct = max(0, min(100, int(fraction * 100)))
    return (f'<div class="scan-progress"><div class="scan-progress-fill" '
            f'style="width:{pct}%"></div></div>')


def _queue_updates(queue):
    """队列变化后的统一更新：表格 / 空态拖拽区 / 计数 / 统计面板。"""
    table = _queue_table(queue)
    return (
        gr.update(value=table) if table else gr.update(value=None),
        # 只切换拖拽空态的显示/隐藏，绝不 value=None 清空已拖拽文件的图标
        gr.update(visible=not bool(table)),
        gr.update(value=_file_count_md(queue)),
        gr.update(value=_stats_html(queue)),
    )


def _add_files(files, queue, recursive):
    """添加文件/文件夹到队列（含扩展名与子目录过滤）。"""
    queue = list(queue or [])
    paths = _norm_uploaded(list(files or []), bool(recursive))
    existing = {r["path"] for r in queue}
    for p in paths:
        if p not in existing:
            queue.append(_new_queue_record(p))
    _persist_state(queue=queue)
    return [queue] + list(_queue_updates(queue))


def _remove_selected(queue, sel):
    queue = list(queue or [])
    sel = int(sel if sel is not None else -1)
    if 0 <= sel < len(queue):
        queue.pop(sel)
    _persist_state(queue=queue)
    return [queue] + list(_queue_updates(queue)) + [gr.update(value=-1)]


def _clear_queue():
    """清空队列：表格/空态/计数/统计全部重置，并清空右侧识别结果区与状态栏。"""
    _persist_state(queue=[], last=None, cache={})
    return (
        [],  # queue_state
    ) + _queue_updates([]) + (
        gr.update(value=-1),          # sel_row
        gr.update(value="暂无内容"),   # md_out
        gr.update(value=""),          # render_out
        gr.update(value=""),          # md_code
        gr.update(value=None),        # img_out
        None,                         # last_file
        gr.update(value="✅ 就绪"),    # status_bar
    )


def _on_row_result_select(evt: gr.EventData, queue, cache):
    """选中任务队列某行：返回选中行号 + 右侧展示该文件解析结果，
    并把 last_file 切到该行（导出按钮跟随当前查看的文件，所见即所导）。

    evt.row 为前端点击 HTML 表格行时经 trigger('click', {row}) 传入的行号（0 起始）。"""
    try:
        row = int(evt.row if evt is not None else -1)
    except (TypeError, ValueError, AttributeError):
        row = -1
    if row < 0 or not queue or row >= len(queue):
        return -1, gr.update(), gr.update(), gr.update(), gr.update(), gr.update()
    info = (cache or {}).get(queue[row].get("path"))
    if not info or not info.get("md"):
        return row, gr.update(), gr.update(), gr.update(), gr.update(), gr.update()
    md = info["md"]
    images = info.get("images") or {}
    imgs = info.get("img") or []
    if isinstance(imgs, str):
        imgs = [imgs]
    # 始终从 md + images 重新生成预览，避免读到旧的错误 MIME 缓存
    render_html = _md_to_preview_html(md, images)
    return (
        row,
        gr.update(value=_inline_md_images(md, images)),
        gr.update(value=render_html),
        gr.update(value=md),
        (gr.update(value=imgs) if imgs else gr.update()),
        queue[row]["path"],    # last_file 跟随选中行
    )


def _mutex_pdf_mode(checked, other_checked):
    """「PDF 每页输出单独文件」与「PDF 自动合并为单个 Word」互斥。"""
    return gr.update(value=False) if checked and other_checked else gr.update()


def _batch_run(queue, *all_ui):
    """批量识别：按队列顺序逐个解析，实时更新任务表/统计/日志/结果。"""
    n_core = 16
    ui_core = all_ui[:n_core]
    per_page = bool(all_ui[n_core]) if len(all_ui) > n_core else False
    export_chart = bool(all_ui[n_core + 1]) if len(all_ui) > n_core + 1 else False
    files = list(queue) if isinstance(queue, (list, tuple)) else []
    logs = []

    def ts():
        return time.strftime("%H:%M:%S")

    def pack(df=None, drop=None, fcount=None, stats=None, prog=None, pdesc=None,
             log=None, md=None, render=None, code=None, img=None, ob=None,
             sb=None, q=None, lastf=None, rc=None, mds=None):
        return (
            df if df is not None else gr.update(),
            drop if drop is not None else gr.update(),
            fcount if fcount is not None else gr.update(),
            stats if stats is not None else gr.update(),
            prog if prog is not None else gr.update(),
            pdesc if pdesc is not None else gr.update(),
            log if log is not None else gr.update(),
            md if md is not None else gr.update(),
            render if render is not None else gr.update(),
            code if code is not None else gr.update(),
            img if img is not None else gr.update(),
            ob if ob is not None else gr.update(),
            sb if sb is not None else gr.update(),
            q if q is not None else gr.update(),
            lastf if lastf is not None else gr.update(),
            rc if rc is not None else gr.update(),
            mds if mds is not None else gr.update(),
        )

    if not files:
        logs.append(f"[{ts()}] ⚠️ 任务队列为空，请先添加文件或文件夹")
        yield pack(log="\n".join(logs),
                   sb="⚠️ 任务队列为空，请先添加文件",
                   q=files, lastf=None)
        return

    # 提交计数：一次批量任务提交 N 个文件（排队数 = submitted - started）
    status_update(bumps={"submitted": len(files)})
    _stop_event.clear()   # 新批次开始，清除上一次可能残留的停止标志

    settings = _to_canonical(ui_core)
    for rec in files:
        rec.update({"status": "等待", "elapsed": "", "pages": 0,
                    "time": 0.0, "download": None})
    logs.append(f"[{ts()}] ▶️ 开始批量处理，共 {len(files)} 个文件")

    t_global = time.time()
    result_cache = {}          # {path: {"md":..., "img":...}} 供左侧点击联动右侧
    last_download = None
    last_file = None
    last_md = ""
    combined = []
    done = failed = 0
    total_secs = 0.0
    total_pages = 0

    for i, rec in enumerate(files):
        name = rec["name"]
        if _stop_event.is_set():
            # 上一文件被停止后不再开始新文件（未处理的保持「等待」，由 _stop_batch 标记）
            logs.append(f"[{ts()}] ⏹ 已停止，跳过剩余 {len(files) - i} 个文件")
            break
        rec["status"] = "处理中"
        logs.append(f"[{ts()}] → [{i + 1}/{len(files)}] 开始：{name}")
        yield pack(
            df=_queue_table(files) or None,
            fcount=gr.update(value=_file_count_md(files)),
            stats=gr.update(value=_stats_html(files)),
            prog=gr.update(value=_progress_html(i / len(files))),
            pdesc=gr.update(value=f"正在处理 {i + 1}/{len(files)}：{name}"),
            log="\n".join(logs),
            sb=gr.update(value=f"⏳ 批量处理中 {i + 1}/{len(files)}：{name}"),
            q=files, lastf=last_file,
        )
        t0 = time.time()
        try:
            gen = _guarded_entry("web", rec["path"], *settings, None, per_page, export_chart)
            last_item = None
            for item in gen:
                if isinstance(item[0], dict) and item[0].get("__type__") == "update":
                    val = item[0].get("value")
                    if isinstance(val, str):
                        logs.append(f"    {val}")
                    # 转发阶段进度：进度条（item[1]）+ 描述（item[0] 的 ⏳ 文本）
                    prog_md = (item[1] or {}).get("value") if isinstance(item[1], dict) else None
                    desc = (str(val)[2:] if isinstance(val, str) and val.startswith("⏳ ")
                            else (str(val) if isinstance(val, str) else ""))
                    yield pack(
                        df=_queue_table(files) or None,
                        stats=gr.update(value=_stats_html(files)),
                        prog=gr.update(value=prog_md) if prog_md else gr.update(),
                        pdesc=gr.update(value=desc) if desc else gr.update(),
                        log="\n".join(logs),
                        sb=gr.update(value=val) if isinstance(val, str) else gr.update(),
                        q=files, lastf=last_file,
                    )
                else:
                    last_item = item
            status_str, md_text, dl_path = last_item[:3]
            result_images = last_item[3] if len(last_item) > 3 else {}
            elapsed = time.time() - t0
            total_secs += elapsed
            # _guarded_entry 内部捕获异常后以 ❌/⛔/⏹ 状态元组正常 yield（不抛出），
            # 必须据此判定失败/停止，否则失败文件会被误标「完成」并污染结果缓存
            if str(status_str).startswith(("❌", "⛔", "⏹")):
                stopped = str(status_str).startswith("⏹")
                rec.update({"status": "已停止" if stopped else "失败",
                            "elapsed": f"{elapsed:.1f}s", "time": elapsed})
                failed += 1
                _persist_state(queue=files)
                reason = str(status_str).split("：", 1)[-1]
                if stopped:
                    logs.append(f"[{ts()}] ⏹ [{i + 1}/{len(files)}] 已停止：{name}")
                else:
                    logs.append(f"[{ts()}] ✖ [{i + 1}/{len(files)}] 失败：{name} → {reason}")
            else:
                m_pages = re.search(r"共 (\d+) 页", str(status_str))
                pages = int(m_pages.group(1)) if m_pages else 1
                rec.update({"status": "完成", "elapsed": f"{elapsed:.1f}s",
                            "pages": pages, "time": elapsed, "download": dl_path})
                total_pages += pages
                last_download = dl_path
                last_file = rec["path"]
                last_md = md_text
                # 原图预览：图片用上传原图；PDF 用 pdftoppm 渲染出的页面 PNG
                if Path(rec["path"]).suffix.lower() in IMAGE_EXTS:
                    preview_imgs = [rec["path"]]
                else:
                    preview_imgs = _render_pdf_pages(rec["path"])
                result_cache[rec["path"]] = {
                    "md": md_text,
                    "img": preview_imgs,
                    "images": result_images,
                    "html": _md_to_preview_html(md_text, result_images),
                }
                _persist_state(queue=files, last=last_file, cache=result_cache)
                combined.append(f"\n\n---\n\n## 📄 {name}\n\n{md_text}")
                done += 1
                logs.append(f"[{ts()}] ✔ [{i + 1}/{len(files)}] 完成：{name}"
                            f"（{pages} 页，{elapsed:.1f}s）")
        except gr.Error as e:
            elapsed = time.time() - t0
            rec.update({"status": "失败", "elapsed": f"{elapsed:.1f}s", "time": elapsed})
            failed += 1
            total_secs += elapsed
            logs.append(f"[{ts()}] ✖ [{i + 1}/{len(files)}] 失败：{name} → {_err_text(e)}")
        except Exception as e:
            elapsed = time.time() - t0
            rec.update({"status": "失败", "elapsed": f"{elapsed:.1f}s", "time": elapsed})
            failed += 1
            total_secs += elapsed
            logs.append(f"[{ts()}] ✖ [{i + 1}/{len(files)}] 失败：{name} → {_err_text(e)}")

        done_files = i + 1
        elapsed_global = time.time() - t_global
        remaining_files = len(files) - done_files
        if remaining_files > 0 and done_files > 0:
            eta_secs = remaining_files * (elapsed_global / done_files)
            pdesc = (f"已完成 {done_files}/{len(files)} 个文件 · 已用 {elapsed_global:.0f} 秒 · "
                     f"预计剩余 {eta_secs:.0f} 秒")
        else:
            pdesc = f"已完成 {done_files}/{len(files)} 个文件 · 已用 {elapsed_global:.0f} 秒"
        yield pack(
            df=_queue_table(files) or None,
            stats=gr.update(value=_stats_html(files)),
            prog=gr.update(value=_progress_html((i + 1) / len(files))),
            pdesc=gr.update(value=pdesc),
            log="\n".join(logs),
            # 右侧展示区保持空白，待单击左侧文件行后填充（_on_row_result_select）
            ob=gr.update(value=last_download),
            sb=gr.update(value=f"⏳ 批量处理中 {i + 1}/{len(files)}"),
            q=files, lastf=last_file, rc=result_cache,
        )

    elapsed_total = time.time() - t_global
    avg_sec_per_page = total_secs / total_pages if total_pages else 0.0
    if _stop_event.is_set() and (done + failed) < len(files):
        final_desc = (f"⏹ 批量已停止：成功 {done}，失败 {failed}，"
                      f"未处理 {len(files) - done - failed} 个，已用 {elapsed_total:.1f}s")
    else:
        final_desc = (f"✅ 批量完成：{len(files)} 个文件，成功 {done}，失败 {failed}，"
                      f"总耗时 {elapsed_total:.1f}s（平均 {avg_sec_per_page:.2f}s/页）")
    logs.append(f"[{ts()}] 🏁 {final_desc}")
    yield pack(
        df=_queue_table(files) or None,
        stats=gr.update(value=_stats_html(files)),
        prog=gr.update(value=_progress_html(1.0)),
        pdesc=gr.update(value=final_desc),
        log="\n".join(logs),
        # 明确指向最后成功生成的结果文件，避免按钮值被空更新覆盖
        ob=gr.update(value=last_download) if last_download else gr.update(),
        sb=gr.update(value=final_desc),
        q=files, lastf=last_file, rc=result_cache, mds=last_md,
    )


def _stop_batch(queue, log_text):
    """停止批量处理：置位停止标志（进行中的 HTTP 请求会被 _http_post 放弃，
    批量循环随之退出），并把处理中/等待中的任务标记为已停止。
    注意：服务端已接收的解析任务无法取消，会自行跑完。"""
    _stop_event.set()
    queue = list(queue or [])
    for rec in queue:
        if rec["status"] in ("处理中", "等待"):
            rec["status"] = "已停止"
    t = time.strftime("%H:%M:%S")
    log_text = f"{log_text or ''}\n[{t}] ⏹ 用户手动停止批量处理"
    table = _queue_table(queue)
    _persist_state(queue=queue)
    return (
        queue,
        gr.update(value=table) if table else gr.update(value=None),
        gr.update(value=_file_count_md(queue)),
        gr.update(value=_stats_html(queue)),
        log_text,
        "⏹ 已停止（服务端进行中的解析会自行跑完）",
    )


def _export_last(mode, last_file, *ui_core_with_md):
    """按 mode 重新导出：重新解析当前查看的文件（最近成功或队列中点选）。

    注意：last_file 始终保持「原始待解析文件」；
    mode ∈ ("docx", "html", "json", "zip_md") 直接传给 _parse_core.export_mode。
    """
    logs = []
    now = lambda: time.strftime("%H:%M:%S")
    if not last_file:
        yield f"[{now()}] ⚠️ 没有可导出的结果，请先执行识别", "⚠️ 请先执行识别", None, None
        return
    n = len(ui_core_with_md)
    per_page = bool(ui_core_with_md[n - 2]) if n >= 18 else False
    export_chart = bool(ui_core_with_md[n - 1]) if n >= 19 else False
    ui_core = list(ui_core_with_md[1:17]) if n >= 17 else list(ui_core_with_md[1:])
    canon = list(_to_canonical(ui_core))
    labels = {"docx": "Word", "json": "JSON", "html": "HTML", "zip_md": "Markdown 原始文件"}
    label = labels.get(mode, mode)
    logs.append(f"[{now()}] ▶ 重新解析并导出 {label}：{Path(last_file).name}")
    # 与 ui_parse 同理：重新导出也是一次网页提交的解析，同步 bump submitted
    status_update(bumps={"submitted": 1})
    _stop_event.clear()   # 新导出开始，清除上一次可能残留的停止标志
    try:
        gen = _guarded_entry("web", last_file, *canon, mode, per_page, export_chart)
        last = None
        for item in gen:
            if isinstance(item[0], dict) and item[0].get("__type__") == "update":
                val = item[0].get("value")
                if isinstance(val, str):
                    logs.append(f"    {val}")
            else:
                last = item
        st, _md, path = last[:3]
        if str(st).startswith(("❌", "⛔", "⏹")):
            logs.append(f"[{now()}] ✖ 导出失败：{str(st).split('：', 1)[-1]}")
        else:
            logs.append(f"[{now()}] ✔ 已生成 {label}，正在自动下载")
        yield "\n".join(logs), st, path, last_file
    except _UserStopped:
        logs.append(f"[{now()}] ⏹ 导出被用户停止")
        yield "\n".join(logs), "⏹ 已停止：用户手动停止", None, last_file
    except Exception as e:
        logs.append(f"[{now()}] ✖ 导出失败：{_err_text(e)}")
        yield "\n".join(logs), f"❌ 导出失败：{_err_text(e)}", None, last_file


def _export_word(last_file, *ui_core_with_md):
    yield from _export_last("docx", last_file, *ui_core_with_md)


def _export_html_doc(last_file, *ui_core_with_md):
    yield from _export_last("html", last_file, *ui_core_with_md)


def _export_json_doc(last_file, *ui_core_with_md):
    yield from _export_last("json", last_file, *ui_core_with_md)


def _export_zip_md(last_file, *ui_core_with_md):
    yield from _export_last("zip_md", last_file, *ui_core_with_md)


def _clear_log():
    return ""


def _restore_defaults():
    """恢复默认参数：16 核心设置 + 状态栏。"""
    return _default_updates() + (gr.update(value="♻️ 已恢复默认参数"),)


# ==================== 界面 ====================

def _default_updates():
    """返回设置组件的默认更新（16 个核心参数 + 2 个 PDF/图表高级选项）。"""
    d = load_config()["defaults"]
    return (
        gr.update(value="不限制" if not d.get("max_pixels")
                  else str(int(d["max_pixels"]))),                    # 显存占用比→max_pixels
        gr.update(value=bool(d.get("use_seal", False))),             # ov_seal
        gr.update(value=bool(d.get("use_chart", False))),            # ov_chart
        gr.update(value=bool(d.get("use_orientation", False))),      # ov_orientation
        gr.update(value=bool(d.get("use_unwarping", False))),        # ov_unwarping
        gr.update(value=bool(d.get("use_ocr_image_block", False))),  # ov_ocr_image_block
        gr.update(value=bool(d.get("use_format_block", False))),     # ov_format_block
        gr.update(value=bool(d.get("use_layout_mode", True))),       # ov_layout_mode
        gr.update(value="留空"),                                     # ov_prompt_label
        gr.update(value=bool(d.get("use_merge_blocks", True))),      # ov_merge_blocks
        gr.update(value="留空"),                                      # ov_layout_threshold
        gr.update(value="留空"),                                      # ov_min_pixels
        gr.update(value="留空"),                                      # ov_max_new_tokens
        gr.update(value="留空"),                                      # ov_temperature
        gr.update(value="留空"),                                      # ov_top_p
        gr.update(value="留空"),                                      # ov_repetition_penalty
        gr.update(value=bool(d.get("pdf_per_page", False))),         # PDF 每页输出单独文件
        gr.update(value=bool(d.get("export_chart", False))),         # 导出图表区域为图片
    )


def _page_load(sid):
    """每次页面加载：按开关状态显示/隐藏关闭横幅 + 应用设置默认值 + 恢复扫描UI状态。
    仅当会话标记 sid 与持久化文件一致（浏览器前进/后退，同一标签页会话）时才恢复；
    关闭标签页/浏览器后 sessionStorage 清空，下次打开会生成新 sid，从而清空旧状态。"""
    import json as _json

    cfg = load_config()
    sw = cfg["switches"]
    if not sw["scan_service"]:
        banner = gr.update(
            visible=True,
            value="## ⛔ 扫描服务当前关闭\n管理员尚未开启服务（管理后台：7861 端口），暂时无法解析。",
        )
    elif not sw["web_ui"]:
        banner = gr.update(
            visible=True,
            value="## ⛔ 网页入口已关闭\n管理员关闭了网页服务，暂时无法解析。",
        )
    else:
        banner = gr.update(visible=False)

    # 尝试从持久化文件恢复状态（仅当会话标记匹配时）
    state_file = _OUTPUT_ROOT / ".scan_state.json"
    saved = {}
    try:
        if state_file.exists():
            saved = _json.loads(state_file.read_text())
    except Exception:
        saved = {}

    if saved.get("sid") != sid:
        # 新会话（首次打开或关闭后重开）→ 清空旧状态并写入当前会话标记
        saved = {"sid": sid, "queue": [], "last": None, "cache": {}}
        _persist_state(sid=sid, queue=[], last="", cache={})

    queue = list(saved.get("queue", []) or [])
    last = saved.get("last")
    cache = saved.get("cache", {}) or {}

    # 恢复扫描 UI 状态
    queue = list(queue or [])
    table = _queue_table(queue)
    drop_visible = gr.update(visible=not bool(table))
    count_md = _file_count_md(queue)
    stats = _stats_html(queue)

    # 恢复右侧结果预览
    md_text = ""
    images = {}
    render_html = ""
    img_list = None
    last_path = last if isinstance(last, str) else None
    if last_path:
        info = (cache or {}).get(last_path)
        if info:
            md_text = info.get("md") or ""
            images = info.get("images") or {}
            render_html = _md_to_preview_html(md_text, images)
            img = info.get("img") or []
            if isinstance(img, str):
                img = [img]
            if img:
                img_list = img

    return (
        (banner,) + _default_updates()
        + (
            gr.update(value=table) if table else gr.update(value=None),
            drop_visible,
            gr.update(value=count_md),
            gr.update(value=stats),
            gr.update(value=_inline_md_images(md_text, images) or "暂无内容"),
            gr.update(value=render_html),
            gr.update(value=md_text or ""),
            (gr.update(value=img_list) if img_list else gr.update()),
            last_path,
            cache,
        )
    )


def _persist_state(queue=None, last=None, cache=None, sid=None):
    """将当前扫描状态合并写入持久化文件，供页面重新加载时恢复。
    参数为 None 表示不更新该字段（保留文件中已有值）；
    传入 sid 时同步写入会话标记（用于区分浏览器前进/后退与关闭后重开）。"""
    import json as _json

    def _serialize_cache(c):
        out = {}
        for k, v in (c or {}).items():
            if isinstance(v, dict):
                out[k] = {
                    "md": v.get("md", ""),
                    "img": v.get("img"),
                    "images": v.get("images") or {},
                    "html": v.get("html") or "",
                }
            else:
                out[k] = {"md": str(v), "img": None, "images": {}, "html": ""}
        return out

    state_file = _OUTPUT_ROOT / ".scan_state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)

    # 读取已有状态
    existing = {}
    try:
        if state_file.exists():
            existing = _json.loads(state_file.read_text())
    except Exception:
        pass

    # 合并更新
    if sid is not None:
        existing["sid"] = sid
    if queue is not None:
        existing["queue"] = list(queue)
    if last is not None:
        existing["last"] = last if isinstance(last, str) else None
    if cache is not None:
        existing["cache"] = _serialize_cache(cache)

    state_file.write_text(_json.dumps(existing, ensure_ascii=False))


def _clear_queue_persist():
    """清空队列时同时清空持久化状态（保留会话标记 sid）。"""
    _persist_state(queue=[], last="", cache={})


CUSTOM_CSS = r"""
:root {
  --scan-grad-1: #4361ee;
  --scan-grad-2: #7209b7;
  --scan-ok: #22c55e;
  --scan-warn: #f59e0b;
  --scan-muted: #64748b;
  --scan-border: #e2e8f0;
  --scan-bg: #f1f5f9;
}
body {
  background: var(--scan-bg) !important;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "PingFang SC",
    "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
}
.gradio-container {
  max-width: 1560px !important;
  padding: 20px 24px 0 !important;
}

/* 隐藏 Gradio 自动进度条（界面使用自定义进度条） */
.progress-bar,
.progress-bar > div,
.progress > .bar,
.progress-track,
.progress-wrapper,
.progress-container {
  display: none !important;
}

a {
  color: var(--scan-grad-1);
}

/* === 顶部导航栏 === */
.scan-topbar {
  position: relative;
  display: flex;
  align-items: center;
  gap: 12px;
  background: #ffffff;
  border: 1px solid var(--scan-border);
  border-radius: 8px;
  padding: 6px 12px;
  margin-bottom: 16px;
}
.scan-nav-group {
  display: flex;
  gap: 6px;
}
.scan-nav-btn {
  border: none !important;
  background: transparent !important;
  color: var(--scan-muted) !important;
  font-size: 14px !important;
  font-weight: 500 !important;
  padding: 7px 16px !important;
  border-radius: 6px !important;
  display: inline-flex !important;
  align-items: center !important;
  gap: 6px !important;
  box-shadow: none !important;
}
.scan-nav-btn svg {
  vertical-align: -2px;
}
.scan-nav-btn:hover {
  background: #eef2f7 !important;
  color: #334155 !important;
}
.scan-nav-btn.active {
  background: linear-gradient(135deg, var(--scan-grad-1), var(--scan-grad-2)) !important;
  color: #ffffff !important;
  box-shadow: 0 2px 8px rgba(67, 97, 238, 0.35) !important;
}
/* GPU 信息 + 模型就绪徽章：水平居中偏右展示 */
.scan-gpu-wrap {
  position: absolute;
  left: 55%;
  top: 50%;
  transform: translate(-50%, -50%);
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.scan-gpu {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #475569;
  background: #f1f5f9;
  border: 1px solid var(--scan-border);
  padding: 3px 10px;
  border-radius: 999px;
  white-space: nowrap;
}
.scan-ready-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  font-weight: 500;
  color: #15803d;
  background: #dcfce7;
  border: 1px solid #bbf7d0;
  border-radius: 999px;
  padding: 2px 10px;
  white-space: nowrap;
  transition: background 0.2s, color 0.2s, border-color 0.2s;
}
/* 徽章四态（对齐 Qt 版状态机）：ready 绿 / busy 琥珀 / error 红 */
.scan-ready-badge.busy {
  color: #b45309;
  background: #fef3c7;
  border-color: #fde68a;
}
.scan-ready-badge.error {
  color: #dc2626;
  background: #fee2e2;
  border-color: #fecaca;
}
.scan-nav-links {
  margin-left: auto;
}
.scan-nav-links a {
  color: #94a3b8;
  font-size: 11px;
  text-decoration: none;
  margin-left: 10px;
}
.scan-nav-links a:hover {
  color: var(--scan-grad-1);
}

/* === 卡片通用 === */
.scan-page {
  gap: 16px;
}
/* 页签初始状态：设置页默认隐藏，由 SCAN_JS 的 _scanTab 控制显隐
   （Gradio 6 中 visible=False 的组件不会进入 DOM，故改为始终渲染 + CSS 隐藏。
    不能用 !important：内联 display 样式需能覆盖本规则） */
#tab-settings {
  display: none;
}
.scan-main-row {
  gap: 16px;
  align-items: stretch;
}
.scan-col {
  gap: 16px;
}
.scan-card {
  background: #ffffff;
  border: 1px solid var(--scan-border);
  border-radius: 8px;
  padding: 16px;
}
.scan-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 12px;
}
.scan-card-title {
  font-size: 15px;
  font-weight: 600;
  color: #0f172a;
}
.scan-badge-md .markdown-body p {
  margin: 0;
  font-size: 12px;
  color: var(--scan-grad-1);
  background: #eef2ff;
  border-radius: 999px;
  padding: 2px 10px;
}

/* === 队列操作栏 === */
.scan-toolbar {
  gap: 8px;
  justify-content: space-between;
  align-items: center;
}
.scan-toolbar-left {
  gap: 8px;
  flex-wrap: wrap;
  flex: 1 1 auto;
}
/* 类可能落在按钮或外层包装上，两种选择器都覆盖 */
.scan-tool-btn,
.scan-tool-btn button {
  font-size: 13px !important;
  background: #ffffff !important;
  border: 1px solid var(--scan-border) !important;
  color: #475569 !important;
  padding: 5px 12px !important;
  border-radius: 6px !important;
  box-shadow: none !important;
}
.scan-tool-btn:hover,
.scan-tool-btn button:hover {
  border-color: var(--scan-grad-1) !important;
  color: var(--scan-grad-1) !important;
}
/* 图标按钮（纯图标 + tooltip） */
.scan-icon-btn,
.scan-icon-btn button {
  width: 34px !important;
  min-width: 34px !important;
  padding: 5px 0 !important;
  background: #ffffff !important;
  border: 1px solid var(--scan-border) !important;
  color: #475569 !important;
  border-radius: 6px !important;
  box-shadow: none !important;
  display: inline-flex !important;
  align-items: center !important;
  justify-content: center !important;
}
.scan-icon-btn:hover,
.scan-icon-btn button:hover {
  border-color: var(--scan-grad-1) !important;
  color: var(--scan-grad-1) !important;
}
.scan-icon-btn img,
.scan-tool-btn img,
.scan-result-icon img {
  width: 16px !important;
  height: 16px !important;
  -webkit-mask-repeat: no-repeat;
  mask-repeat: no-repeat;
  -webkit-mask-size: contain;
  mask-size: contain;
  -webkit-mask-position: center;
  mask-position: center;
}
.scan-recursive-cb label {
  font-size: 13px !important;
}
.scan-file-table {
  min-height: 150px;
  font-size: 13px;
  overflow-x: auto;
}
/* 自渲染任务队列表格：固定布局 + 文件名溢出省略 */
.scan-qtable {
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
  font-size: 13px;
  /* 去掉 Gradio .prose 默认的表格外框线 */
  border: none !important;
}
.scan-qtable col.w-idx { width: 34px; }
.scan-qtable col.w-pages { width: 52px; }
.scan-qtable col.w-status { width: 72px; }
.scan-qtable col.w-elapsed { width: 72px; }
/* 先彻底去掉 Gradio .prose 默认给 table/thead/tbody/tr/th/td 的所有格子线，
   再只给单元格加行底部细分隔线 */
.scan-qtable,
.scan-qtable thead,
.scan-qtable tbody,
.scan-qtable tr,
.scan-qtable th,
.scan-qtable td {
  border: none !important;
}
.scan-qtable th,
.scan-qtable td {
  padding: 8px 6px;
  border-bottom: 1px solid var(--scan-border) !important;
  text-align: left;
  vertical-align: middle;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.scan-qtable th {
  font-weight: 600;
  color: #334155;
  background: #f8fafc;
  user-select: none;
}
.scan-qtable td.c-idx,
.scan-qtable td.c-pages,
.scan-qtable td.c-status,
.scan-qtable td.c-elapsed {
  text-align: center;
}
.scan-qtable tbody tr {
  cursor: pointer;
  transition: background 0.15s;
}
.scan-qtable tbody tr:hover {
  background: #eef2f7;
}
/* 行状态配色（JS 以行整体 color 着色，此处兜底同色系背景） */
.scan-qtable tr.st-ok td { color: #15803d; }
.scan-qtable tr.st-run td { color: #f59e0b; }
.scan-qtable tr.st-err td { color: #dc2626; }
.scan-qtable tr.st-wait td { color: #64748b; }

/* 拖拽空态（gr.File 组件作为拖放区） */
#file-drop {
  border: 2px dashed #cbd5e1 !important;
  border-radius: 8px !important;
  background: #fafbfc !important;
  min-height: 70px;
  font-size: 13px;
  text-align: center;
}
#file-drop .wrap,
#file-drop .file-preview {
  min-height: 70px;
}
#file-drop label span {
  color: #94a3b8;
  font-size: 13px;
}
#file-drop label {
  justify-content: center !important;
}
#file-drop .wrap {
  justify-content: center !important;
}

/* === 统计面板 === */
.scan-stat-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
  margin: 14px 0;
}
.scan-stat-card {
  background: #f8fafc;
  border: 1px solid #eef2f7;
  border-radius: 8px;
  padding: 10px 12px;
  text-align: center;
}
.scan-stat-label {
  font-size: 11px;
  color: var(--scan-muted);
}
.scan-stat-value {
  font-size: 18px;
  font-weight: 700;
  color: #0f172a;
  margin-top: 2px;
}
.scan-stat-sub {
  font-size: 10px;
  color: #94a3b8;
  margin-top: 2px;
}
/* 统计数值状态色：成功绿 / 失败橙 */
.scan-stat-ok {
  color: var(--scan-ok) !important;
}
.scan-stat-err {
  color: var(--scan-warn) !important;
}

/* === 进度条（蓝紫渐变） === */
.scan-progress {
  height: 10px;
  background: #eef2f7;
  border-radius: 999px;
  overflow: hidden;
}
.scan-progress-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--scan-grad-1), var(--scan-grad-2));
  border-radius: 999px;
  transition: width 0.3s ease;
}
.scan-progress-desc .markdown-body p {
  margin: 8px 0 0;
  font-size: 13px;
  color: #475569;
}

/* 去掉 Gradio Generating 状态覆盖层的「框呼吸」特效：
   Gradio 6 在事件运行期间会给所有输出组件叠加 2px 蓝色边框 + opacity 呼吸动画。
   处理进度只保留自定义进度条即可 */
.wrap.generating,
.wrap.default.generating,
.wrap.center.generating {
  border: none !important;
  box-shadow: none !important;
  animation: none !important;
  opacity: 0 !important;
}

/* === 主按钮 === */
/* Gradio 6 中 elem_classes 直接落在 <button> 自身，需同时覆盖自身与内部 */
.scan-primary-btn,
.scan-primary-btn button {
  background: linear-gradient(135deg, var(--scan-grad-1), var(--scan-grad-2)) !important;
  border: none !important;
  color: #ffffff !important;
  font-weight: 600 !important;
  box-shadow: 0 2px 10px rgba(67, 97, 238, 0.3) !important;
}
.scan-outline-btn,
.scan-outline-btn button {
  background: #ffffff !important;
  border: 1px solid #cbd5e1 !important;
  color: #475569 !important;
}
/* 停止按钮 danger 变体（对齐 Qt 版 danger 按钮） */
.scan-stop-btn,
.scan-stop-btn button {
  background: #ffffff !important;
  border: 1px solid #fca5a5 !important;
  color: #dc2626 !important;
}
.scan-stop-btn:hover,
.scan-stop-btn:hover button {
  background: #fef2f2 !important;
  border-color: #f87171 !important;
  color: #b91c1c !important;
}

/* === 识别结果 === */
.scan-result-card {
  min-height: 620px;
  display: flex;
  flex-direction: column;
}
.scan-result-icons {
  gap: 4px;
  flex-wrap: nowrap;
}
.scan-result-icons button,
button.scan-result-icon {
  font-size: 12px !important;
  width: 28px !important;
  min-width: 28px !important;
  padding: 3px 0 !important;
  background: #f8fafc !important;
  border: 1px solid var(--scan-border) !important;
  color: #475569 !important;
  border-radius: 6px !important;
  display: inline-flex !important;
  align-items: center !important;
  justify-content: center !important;
  flex: none !important;
}
.scan-result-icons button:hover,
button.scan-result-icon:hover {
  border-color: var(--scan-grad-1) !important;
  color: var(--scan-grad-1) !important;
}
.scan-result-icons button:disabled,
button.scan-result-icon:disabled {
  opacity: 0.45 !important;
  cursor: not-allowed !important;
  border-color: var(--scan-border) !important;
  color: #94a3b8 !important;
}

/* === 高级导出下拉菜单（勾选「导出结构化 JSON / HTML」后出现对应项） === */
#adv-export-wrap { position: relative; }
#adv-export-menu {
  position: absolute;
  right: 0;
  top: 40px;
  z-index: 200;
  background: #ffffff;
  border: 1px solid var(--scan-border);
  border-radius: 8px;
  box-shadow: 0 4px 18px rgba(15, 23, 42, 0.14);
  min-width: 230px;
  padding: 4px;
  display: none;
}
.adv-menu-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  font-size: 13px;
  color: #334155;
  border-radius: 6px;
  cursor: pointer;
  white-space: nowrap;
}
.adv-menu-item:hover { background: #f1f5f9; }
.adv-menu-icon {
  flex: none;
  font-size: 10px;
  font-weight: 700;
  color: #4361ee;
  background: #eef2ff;
  border-radius: 4px;
  padding: 1px 5px;
}
.scan-hidden { display: none !important; }

/* 识别结果内容页签（Markdown / 源码 / 原图） */
.scan-rtab {
  border: none !important;
  background: transparent !important;
  color: var(--scan-muted) !important;
  font-size: 13px !important;
  padding: 6px 14px !important;
  border-radius: 6px 6px 0 0 !important;
  cursor: pointer !important;
  box-shadow: none !important;
}
.scan-rtab:hover {
  color: #334155 !important;
}
.scan-rtab.active {
  color: var(--scan-grad-1) !important;
  font-weight: 600 !important;
  box-shadow: inset 0 -2px 0 var(--scan-grad-1) !important;
}
#md-preview .markdown-body {
  min-height: 460px;
  max-height: 720px;
  overflow-y: auto;
  padding: 8px 4px;
}
#rtab-pane-render {
  min-height: 460px;
  max-height: 720px;
  overflow-y: auto;
  padding: 8px 4px;
}
#rtab-pane-render img {
  max-width: 100%;
  height: auto;
  display: block;
  margin: 12px auto;
}
#rtab-pane-render table {
  border-collapse: collapse;
  margin: 12px 0;
  width: 100%;
}
#rtab-pane-render th, #rtab-pane-render td {
  border: 1px solid #ccc;
  padding: 6px 10px;
  text-align: center;
}
#rtab-pane-render th {
  background: #f5f5f5;
}
#img-preview img {
  max-height: 640px;
  width: auto;
  margin: 0 auto;
}
#rtab-pane-code textarea {
  min-height: 440px;
  font-family: "SF Mono", Consolas, "Courier New", monospace;
  font-size: 12px;
}
#result-card.scan-fullscreen {
  position: fixed;
  inset: 0;
  z-index: 9999;
  background: #ffffff;
  border: none;
  border-radius: 0;
  overflow: auto;
  padding: 24px;
}

/* === 运行日志 === */
.scan-log-card {
  margin-top: 16px;
}
.scan-log-title {
  font-size: 14px;
  font-weight: 700;
  color: #0f172a;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.scan-log {
  font-family: "SF Mono", Consolas, "Courier New", "PingFang SC", monospace;
  font-size: 12px !important;
  background: #f8fafc !important;
  color: #334155 !important;
  border: 1px solid var(--scan-border) !important;
}
.scan-log textarea {
  font-family: inherit !important;
  font-size: 12px !important;
  background: #f8fafc !important;
  color: #334155 !important;
}

/* === 参数设置页 === */
.scan-card-grid {
  gap: 10px;
}
.scan-hint {
  font-size: 12px;
  color: var(--scan-muted);
  background: #f8fafc;
  border: 1px solid #eef2f7;
  border-radius: 6px;
  padding: 8px 10px;
  line-height: 1.6;
  margin-top: 8px;
}
.scan-model-info .markdown-body p {
  margin: 4px 0;
  font-size: 13px;
  color: #475569;
}
/* 复选框双列网格 */
.scan-check-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 2px 18px;
  margin-bottom: 4px;
}
.scan-check-grid .form {
  padding: 0;
}
/* 统计面板容器（Markdown 包裹） */
.scan-stats .markdown-body {
  padding: 0;
}
.scan-accordion {
  border: 1px solid var(--scan-border) !important;
  border-radius: 8px !important;
}
.scan-reset-btn,
.scan-reset-btn button {
  font-size: 12px !important;
  color: var(--scan-grad-1) !important;
  background: transparent !important;
  border: none !important;
  padding: 4px 8px !important;
  text-align: right !important;
}

/* === 底部状态栏 === */
#status-bar {
  position: sticky;
  bottom: 0;
  background: #ffffff;
  border: 1px solid var(--scan-border);
  border-radius: 8px 8px 0 0;
  padding: 8px 16px;
  font-size: 13px;
  color: #475569;
  margin-top: 16px;
  z-index: 40;
}
#status-bar .markdown-body p {
  margin: 0;
}

/* === 服务关闭横幅 === */
#svc-banner {
  background: #fef2f2;
  border: 1px solid #fecaca;
  color: #b91c1c;
  border-radius: 8px;
  padding: 10px 16px;
  margin-bottom: 16px;
}

/* === Checkbox / Switch：只改背景与边框，保留 Gradio 默认白色勾选 SVG === */
input[type="checkbox"]:checked,
.checkbox input:checked,
.toggle input:checked + label {
  background-color: var(--scan-grad-1) !important;
  border-color: var(--scan-grad-1) !important;
  background-image: url("data:image/svg+xml,%3csvg viewBox='0 0 16 16' fill='white' xmlns='http://www.w3.org/2000/svg'%3e%3cpath d='M12.207 4.793a1 1 0 010 1.414l-5 5a1 1 0 01-1.414 0l-2-2a1 1 0 011.414-1.414L6.5 9.086l4.293-4.293a1 1 0 011.414 0z'/%3e%3c/svg%3e") !important;
  background-position: 50% center !important;
  background-repeat: no-repeat !important;
  background-size: 100% 100% !important;
}
"""

_GPU_TEXT = _get_gpu_text()

TOPBAR_HTML = f"""
<div class="scan-topbar">
  <div class="scan-nav-group">
    <button type="button" class="scan-nav-btn active" id="nav-batch" onclick="_scanTab('batch')">
      {_svg(_IP_SCAN)}批量识别
    </button>
    <button type="button" class="scan-nav-btn" id="nav-settings" onclick="_scanTab('settings')">
      {_svg(_IP_SETTINGS)}参数设置
    </button>
  </div>
  <span class="scan-gpu-wrap">
    <span class="scan-gpu" title="GPU 型号与显存">{_GPU_TEXT}</span>
    <span class="scan-ready-badge" id="scan-state-badge" title="后端 OCR 服务已就绪">模型已就绪</span>
  </span>
  <span class="scan-nav-links">
    <a href="https://github.com/PaddlePaddle/PaddleOCR" target="_blank">GitHub</a>
    <a href="/gradio_api/mcp/" target="_blank">MCP</a>
    <a href="http://127.0.0.1:8080/docs" target="_blank">API</a>
  </span>
</div>
"""

# 顶部导航页签 / 识别结果内容页签的 JS 切换逻辑。
# 以原始 <script> 通过 launch(head=...) 注入页面 head，绕过 Gradio 对事件
# js 的 AsyncFunction 编译（多语句脚本会导致页面初始化 / 重载时报 SyntaxError）。
SCAN_JS = r"""<script>
window._scanTab = function (name) {
  var b = document.getElementById('tab-batch');
  var s = document.getElementById('tab-settings');
  if (!b || !s) return;
  b.style.display = name === 'batch' ? 'flex' : 'none';
  s.style.display = name === 'settings' ? 'flex' : 'none';
  var bb = document.getElementById('nav-batch');
  var bs = document.getElementById('nav-settings');
  if (bb) bb.classList.toggle('active', name === 'batch');
  if (bs) bs.classList.toggle('active', name === 'settings');
};
window._scanRtab = function (name) {
  var panes = {md: 'rtab-pane-md', code: 'rtab-pane-code', img: 'rtab-pane-img', render: 'rtab-pane-render'};
  var btns = {md: 'rtab-btn-md', code: 'rtab-btn-code', img: 'rtab-btn-img', render: 'rtab-btn-render'};
  Object.keys(panes).forEach(function (k) {
    var el = document.getElementById(panes[k]);
    if (el) el.style.display = k === name ? 'flex' : 'none';
    var b = document.getElementById(btns[k]);
    if (b) b.classList.toggle('active', k === name);
  });
};
// 徽章四态状态机（§6.1）：ready(绿) / busy(琥珀「处理中」) / error(红)
window._scanBadge = function (state, text) {
  var el = document.getElementById('scan-state-badge');
  if (!el) return;
  var cls = state === 'ready' ? 'scan-ready-badge' : 'scan-ready-badge ' + state;
  if (el.className !== cls) el.className = cls;
  if (el.textContent !== text) el.textContent = text;
};
window._scanSyncBadge = function () {
  var fill = document.querySelector('.scan-progress-fill');
  var desc = document.querySelector('.scan-progress-desc');
  var pct = fill ? (parseInt((fill.style.width || '0').replace('%', ''), 10) || 0) : 0;
  var txt = desc ? desc.textContent : '';
  if (/(?:失败[：:\s]*[1-9]\d*|错误|异常)/.test(txt)) { _scanBadge('error', '处理出错'); return; }
  if (pct > 0 && pct < 100) { _scanBadge('busy', '处理中'); return; }
  _scanBadge('ready', '模型已就绪');
};
// 右侧结果区六个操作按钮：未选中左侧文件（结果区为空）时禁用
window._scanSyncButtons = function () {
  var el = document.getElementById('md-preview');
  var txt = el ? (el.textContent || '').trim() : '';
  var has = !!txt && txt !== '暂无内容';
  var ids = ['btn-open-result', 'btn-word', 'btn-html', 'btn-json', 'btn-copy', 'btn-fullscreen'];
  ids.forEach(function (id) {
    var el = document.getElementById(id);
    if (!el) return;
    var btn = el.querySelector('button') || el;
    if (has) btn.removeAttribute('disabled');
    else btn.setAttribute('disabled', 'disabled');
  });
};
// 运行日志折叠（§5）：只隐藏日志文本框，保留标题栏与折叠按钮
window._scanToggleLog = function () {
  var box = document.getElementById('scan-log-box');
  var btn = document.getElementById('btn-log-toggle');
  if (!box) return;
  var hidden = box.style.display === 'none';
  box.style.display = hidden ? '' : 'none';
  if (btn) btn.textContent = hidden ? '折叠日志' : '展开日志';
};
// 表格行状态配色（§3.2）：待处理灰 / 处理中橙 / 完成绿 / 失败红
function _scanPaintTable() {
  var t = document.querySelector('.scan-file-table table');
  if (!t) return;
  var rows = t.querySelectorAll('tbody tr');
  for (var r = 0; r < rows.length; r++) {
    var stEl = rows[r].querySelector('td.c-status');
    var st = stEl ? (stEl.textContent || '').trim() : '';
    var color = '';
    if (/处理中/.test(st)) color = '#f59e0b';
    else if (/完成/.test(st)) color = '#15803d';
    else if (/失败|已停止/.test(st)) color = '#dc2626';
    else if (/等待/.test(st)) color = '#64748b';
    if (color) rows[r].style.color = color;
  }
}
// 等待 Gradio 组件挂载完成后：初始化页签状态 + 图标 tooltip/着色
(function () {
  var BLANK = 'data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==';
  var tips = [
    ['btn-add-files', '添加文件'], ['btn-add-folder', '添加文件夹'],
    ['btn-remove', '移除选中'], ['btn-clear-queue', '清空队列'],
    ['btn-open-result', '下载markdown原始文件'], ['btn-word', '导出 Word'],
    ['btn-html', '导出 HTML'], ['btn-json', '导出 JSON'], ['btn-copy', '复制 Markdown'],
    ['btn-fullscreen', '全屏'], ['btn-clear-log', '清空日志'],
  ];
  function init() {
    if (!document.getElementById('tab-batch') || !document.getElementById('btn-add-files')) return false;
    window._scanTab('batch');
    window._scanRtab('md');
    tips.forEach(function (t) {
      var el = document.getElementById(t[0]);
      if (!el) return;
      var b = el.querySelector('button');
      (b || el).setAttribute('title', t[1]);
      var img = el.querySelector('img');
      if (img && img.src && img.src.indexOf('data:image/gif') === -1) {
        var uri = 'url("' + img.src + '")';
        img.setAttribute('src', BLANK);
        img.style.backgroundColor = 'currentColor';
        img.style.webkitMaskImage = uri;
        img.style.maskImage = uri;
      }
    });
    // 徽章状态机与表格行配色：启动轮询（进度条/表格更新后自动刷新）
    _scanSyncBadge();
    _scanPaintTable();
    _scanSyncButtons();
    setInterval(_scanSyncBadge, 500);
    setInterval(_scanPaintTable, 800);
    setInterval(_scanSyncButtons, 500);
    // 高级导出按钮：原生事件绑定（不依赖 Gradio 事件系统，确保点击响应）
    var advBtn = document.getElementById('btn-adv-export');
    if (advBtn) {
      advBtn.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        var m = document.getElementById('adv-export-menu');
        if (m) _scanAdvMenu(m.style.display !== 'block');
      });
    }
    return true;
  }
  if (!init()) {
    var iv = setInterval(function () { if (init()) clearInterval(iv); }, 300);
    setTimeout(function () { clearInterval(iv); }, 20000);
  }

  // 浏览器前进/后退时，强制重载页面以确保 WebSocket 重连 + UI 状态恢复
  window.addEventListener('pageshow', function (e) {
    if (e.persisted) {
      // bfcache 恢复：页面被浏览器缓存后重新显示，强制刷新以保证 UI 同步
      location.reload();
    }
  });
})();
</script>"""

_MODEL_INFO_MD = (
    f"**VLM 模型**：PaddleOCR-VL（多模态文档解析）\n\n"
    f"**版面模型**：Paddle-Layout（GPU 推理）\n\n"
    f"**GPU**：`{_GPU_TEXT}`\n\n"
    f"**服务地址**：`{get_api_url()}`"
)

with gr.Blocks(title="PaddleOCR-VL 文档解析") as demo:
    svc_banner = gr.Markdown(visible=False, elem_id="svc-banner")
    gr.HTML(TOPBAR_HTML)
    status_bar = gr.Markdown("✅ 就绪", elem_id="status-bar")

    # ---------- 隐藏组件与状态 ----------
    queue_state = gr.State([])     # 任务队列
    sel_row = gr.State(None)
    last_file = gr.State(None)     # 当前结果文件：最近一次成功识别，或队列中点选查看的文件
    md_state = gr.State("")        # 最近一次扫描的原始 Markdown 文本
    result_cache = gr.State({})    # {path: {"md":..., "img":...}} 文件解析结果缓存
    # 标签页会话标记：由 sessionStorage 生成，关闭标签页/浏览器即失效。
    # 用隐藏 Textbox 承载（而非 gr.State），以便页面加载时通过前端 JS 写入，
    # 再经 change 事件把 sid 可靠地传给后端（demo.load 的 js 返回值不会覆盖后端输入）。
    sid_box = gr.Textbox(value="", visible=False, elem_id="scan-sid-box")
    # MCP 隐藏入口（受 mcp 子开关控制；Agent 调用 paddleocr_vl 时传文件）
    file_input = gr.File(visible=False, file_count="single", type="filepath")
    mcp_btn = gr.Button(visible=False)
    # MCP 官方协议兼容参数（隐藏，仅供 Agent 调用 paddleocr_vl 时传参，网页不可见）
    # input_data 用 Textbox（纯字符串），与官方协议一致：HTTP(S) URL / 本机绝对路径 /
    # base64 data URI 均可直接传入，由本进程负责下载/读取（不经 Gradio 下载，
    # 因此不受 Gradio SSRF 防护限制，内网/本机地址同样可用）。
    input_data_cb = gr.Textbox(value=None, visible=False)
    file_path_alt = gr.File(visible=False, file_count="single", type="filepath")  # 旧版参数名 file_path 别名
    output_mode_cb = gr.Dropdown(choices=["simple", "detailed"], value="simple", visible=False)
    file_type_cb = gr.Number(value=None, visible=False)   # 0=PDF，1=图片，None=自动
    return_images_cb = gr.Checkbox(value=True, visible=False)
    runtime_params_cb = gr.Textbox(
        value=None, visible=False, placeholder='{"use_chart_recognition": true, ...}',
    )  # 官方 22 键 runtime_params（JSON 对象或 JSON 字符串）
    # 隐藏的下载目标：各导出按钮把生成的文件路径写入此 DownloadButton，
    # 再由前端 _AUTO_DL_JS 模拟点击其内部 <button> 触发浏览器下载。
    # 与「下载 Markdown」触发按钮分离，避免同一个按钮既触发导出又被自动点击。
    dl_target = gr.DownloadButton(visible="hidden", elem_id="btn-dl-target")

    # ================= 页签一：批量识别 =================
    # 用 Column 作为外层：内部纵向堆叠「两栏主区 + 底部运行日志」，
    # 避免 gr.Row（横向 flex）把运行日志挤到右侧
    with gr.Column(elem_id="tab-batch", elem_classes="scan-page"):
        with gr.Row(elem_classes="scan-main-row"):
            # ---------- 左列 45%：任务队列 ----------
            with gr.Column(scale=45, elem_classes="scan-col"):
                with gr.Column(elem_classes="scan-card"):
                    with gr.Row(elem_classes="scan-card-head"):
                        gr.HTML('<span class="scan-card-title">任务队列</span>')
                        file_count_md = gr.Markdown(
                            "共 **0** 个文件", elem_classes="scan-badge-md")
                    with gr.Row(elem_classes="scan-toolbar"):
                        with gr.Row(elem_classes="scan-toolbar-left"):
                            add_files_btn = gr.UploadButton(
                                icon=_icon_url(_IP_FILE_PLUS), label="添加文件",
                                file_count="multiple", type="filepath",
                                elem_id="btn-add-files", elem_classes="scan-tool-btn")
                            add_folder_btn = gr.UploadButton(
                                icon=_icon_url(_IP_FOLDER_PLUS), label="添加文件夹",
                                file_count="directory", type="filepath",
                                elem_id="btn-add-folder", elem_classes="scan-tool-btn")
                            remove_btn = gr.Button(
                                icon=_icon_url(_IP_TRASH), value="移除",
                                elem_id="btn-remove", elem_classes="scan-tool-btn")
                            clear_queue_btn = gr.Button(
                                icon=_icon_url(_IP_X), value="清空",
                                elem_id="btn-clear-queue", elem_classes="scan-tool-btn")
                        recursive_cb = gr.Checkbox(
                            label="含子目录", value=False, elem_classes="scan-recursive-cb")
                    file_table = gr.HTML(
                        value=_queue_table([]),
                        elem_classes="scan-file-table",
                        # 关闭 Gradio 默认 prose 样式（其会给表格加格子线）
                        apply_default_css=False,
                        # 行点击：事件委托绑定到容器，用 trigger('click', {row}) 把行号传回后端
                        js_on_load=(
                            "element.addEventListener('click', function (e) {"
                            "var tr = e.target.closest ? e.target.closest('tr[data-row]') : null;"
                            "if (tr) trigger('click', {row: parseInt(tr.getAttribute('data-row'), 10)});"
                            "});"
                        ),
                    )
                    file_drop = gr.File(
                        label="支持拖拽文件或文件夹到此处",
                        file_count="multiple", type="filepath",
                        elem_id="file-drop", interactive=True,
                    )
                # ---------- 统计与操作（对齐参考：独立卡片） ----------
                with gr.Column(elem_classes="scan-card"):
                    stats_html = gr.Markdown(value=_stats_html([]), elem_classes="scan-stats")
                    progress_html = gr.Markdown(value=_progress_html(0.0))
                    progress_desc_md = gr.Markdown(
                        "等待开始…", elem_classes="scan-progress-desc")
                    with gr.Row():
                        start_btn = gr.Button(
                            "开始处理", variant="primary", elem_classes="scan-primary-btn")
                        stop_btn = gr.Button("停止", elem_classes="scan-outline-btn scan-stop-btn")

            # ---------- 右列 55%：识别结果 ----------
            with gr.Column(scale=55, elem_classes="scan-col"):
                with gr.Column(elem_classes="scan-card scan-result-card", elem_id="result-card"):
                    with gr.Row(elem_classes="scan-card-head"):
                        gr.HTML('<span class="scan-card-title">识别结果</span>')
                        with gr.Row(elem_classes="scan-result-icons"):
                            md_dl_btn = gr.Button(
                                icon=_icon_url(_IP_DOWNLOAD), value="",
                                elem_id="btn-open-result", elem_classes="scan-result-icon")
                            word_btn = gr.Button(
                                icon=_icon_url(_IP_FILE_TEXT), value="",
                                elem_id="btn-word", elem_classes="scan-result-icon")
                            html_btn = gr.Button(
                                icon=_icon_url(_IP_CODE), value="",
                                elem_id="btn-html", elem_classes="scan-result-icon")
                            json_btn = gr.Button(
                                icon=_icon_url(_IP_BRACKET), value="",
                                elem_id="btn-json", elem_classes="scan-result-icon")
                            copy_md_btn = gr.Button(
                                icon=_icon_url(_IP_COPY), value="",
                                elem_id="btn-copy", elem_classes="scan-result-icon")
                            fullscreen_btn = gr.Button(
                                icon=_icon_url(_IP_MAXIMIZE), value="",
                                elem_id="btn-fullscreen", elem_classes="scan-result-icon")
                    gr.HTML(
                        '<div style="display:flex;gap:4px;border-bottom:1px solid var(--scan-border);'
                        'margin-bottom:12px;padding:0 2px;">'
                        '<button type="button" class="scan-rtab active" id="rtab-btn-md" '
                        'onclick="_scanRtab(\'md\')">Markdown</button>'
                        '<button type="button" class="scan-rtab" id="rtab-btn-render" '
                        'onclick="_scanRtab(\'render\')">渲染</button>'
                        '<button type="button" class="scan-rtab" id="rtab-btn-code" '
                        'onclick="_scanRtab(\'code\')">源码</button>'
                        '<button type="button" class="scan-rtab" id="rtab-btn-img" '
                        'onclick="_scanRtab(\'img\')">原图</button></div>'
                    )
                    with gr.Column(elem_id="rtab-pane-md"):
                        md_out = gr.Markdown("暂无内容", elem_id="md-preview")
                    with gr.Column(elem_id="rtab-pane-render"):
                        render_out = gr.HTML("", elem_id="render-preview")
                    with gr.Column(elem_id="rtab-pane-code"):
                        md_code = gr.Code(language="markdown", value="", lines=18)
                    with gr.Column(elem_id="rtab-pane-img"):
                        img_out = gr.Gallery(columns=1, height=560, elem_id="img-preview")

        # ---------- 底部：运行日志 ----------
        with gr.Column(elem_classes="scan-card scan-log-card", elem_id="scan-log-card"):
            with gr.Row(elem_classes="scan-card-head"):
                gr.HTML(f'<span class="scan-card-title scan-log-title">{_svg(_IP_TERMINAL)} 运行日志</span>')
                with gr.Row(elem_classes="scan-toolbar-left"):
                    autoscroll_cb = gr.Checkbox(
                        label="自动滚动", value=True, elem_classes="scan-recursive-cb")
                    clear_log_btn = gr.Button(
                        icon=_icon_url(_IP_TRASH), value="",
                        elem_id="btn-clear-log", elem_classes="scan-icon-btn")
                    log_toggle_btn = gr.Button(
                        value="折叠日志", elem_id="btn-log-toggle",
                        elem_classes="scan-tool-btn", scale=0)
            log_text = gr.Textbox(
                lines=10, value="[启动] 就绪，等待添加任务…",
                elem_id="scan-log-box", elem_classes="scan-log",
                interactive=False, show_label=False,
            )

    # ================= 页签二：参数设置 =================
    with gr.Row(elem_id="tab-settings", elem_classes="scan-page"):
        with gr.Row(elem_classes="scan-main-row"):
            # ---------- 左列：参数组 ----------
            with gr.Column(scale=45, elem_classes="scan-col"):
                with gr.Column(elem_classes="scan-card"):
                    with gr.Row(elem_classes="scan-card-head"):
                        gr.HTML('<span class="scan-card-title">模型参数</span>')
                    model_info_md = gr.Markdown(
                        value=_MODEL_INFO_MD, elem_classes="scan-model-info")
                    ctx_len_dd = gr.Dropdown(
                        choices=["留空", "512", "1024", "2048", "4096", "8192"],
                        value="留空", label="上下文长度（单页最大输出 token）")
                    vram_ratio_dd = gr.Dropdown(
                        choices=["不限制", "2000000", "3000000", "4000000"],
                        value=("不限制" if not _cfg_defaults["max_pixels"]
                               else str(int(_cfg_defaults["max_pixels"]))),
                        label="显存占用比（单图最大像素）",
                        info="当前 llama-cpp-server 后端不支持此参数（仅 vllm-server 生效）")
                    temperature_dd = gr.Dropdown(
                        choices=["留空", "0.1", "0.3", "0.5", "0.7", "0.9", "1.0"],
                        value="留空", label="采样温度 temperature")
                    topp_dd = gr.Dropdown(
                        choices=["留空", "0.8", "0.9", "0.95", "1.0"],
                        value="留空", label="top_p 核采样")
                    gr.Markdown(
                        "当前后端为 llama-cpp-server，显存占用比与最小像素总量参数不生效；"
                        "如需缓解显存不足（OOM），请在管理后台切换为更低精度的 VL 量化模型（如 q5_k_m / q4_k_m）。",
                        elem_classes="scan-hint")

                with gr.Column(elem_classes="scan-card"):
                    with gr.Row(elem_classes="scan-card-head"):
                        gr.HTML('<span class="scan-card-title">识别参数</span>')
                    with gr.Row(elem_classes="scan-check-grid"):
                        orient_cb = gr.Checkbox(
                            label="文档方向分类", value=_cfg_defaults["use_orientation"],
                            info="自动校正倒置/旋转的文档图片")
                        unwarp_cb = gr.Checkbox(
                            label="文本图像矫正", value=_cfg_defaults["use_unwarping"],
                            info="拉平卷曲/折痕导致的不规则形变")
                    with gr.Row(elem_classes="scan-check-grid"):
                        seal_cb = gr.Checkbox(
                            label="印章识别", value=_cfg_defaults["use_seal"],
                            info="提取文档中的印章内容")
                        chart_cb = gr.Checkbox(
                            label="图表解析", value=_cfg_defaults["use_chart"],
                            info="解析嵌入式数据图表")
                    with gr.Row(elem_classes="scan-check-grid"):
                        layout_cb = gr.Checkbox(
                            label="版面分析", value=bool(_cfg_defaults.get("use_layout_mode", True)),
                            info="自动检测分栏、表格、标题、图像区域")
                        merge_blocks_cb = gr.Checkbox(
                            label="跨栏分栏合并", value=bool(_cfg_defaults.get("use_merge_blocks", True)),
                            info="将跨栏/交错排列的文本合并为连续段落")
                    with gr.Row(elem_classes="scan-check-grid"):
                        ocrblk_cb = gr.Checkbox(
                            label="图像块内 OCR", value=bool(_cfg_defaults.get("use_ocr_image_block", False)),
                            info="对图片内嵌文字再做一次 OCR")
                        fmtblk_cb = gr.Checkbox(
                            label="块内容格式化", value=bool(_cfg_defaults.get("use_format_block", False)),
                            info="将表格/公式等结构化内容渲染为 Markdown 格式")
                    prompt_label_dd = gr.Dropdown(
                        choices=["留空", "ocr", "formula", "table", "seal", "chart", "spotting"],
                        value="留空", label="单图识别类型（关闭版面分析后生效）")
                    threshold_dd = gr.Dropdown(
                        choices=["留空", "0.3", "0.5", "0.7", "0.9"],
                        value="留空", label="版面检测阈值")
                    minpix_dd = gr.Dropdown(
                        choices=["留空", "200000", "400000", "800000", "1200000"],
                        value="留空", label="最小像素总量",
                        info="当前 llama-cpp-server 后端不支持此参数（仅 vllm-server 生效）")
                    rep_dd = gr.Dropdown(
                        choices=["留空", "1.0", "1.2", "1.5", "2.0"],
                        value="留空", label="重复惩罚")

            # ---------- 右列：设置组 ----------
            with gr.Column(scale=55, elem_classes="scan-col"):
                with gr.Column(elem_classes="scan-card"):
                    with gr.Row(elem_classes="scan-card-head"):
                        gr.HTML('<span class="scan-card-title">PDF 与高级选项</span>')
                    pdf_per_page_cb = gr.Checkbox(
                        label="PDF 每页输出单独文件",
                        value=bool(_cfg_defaults.get("pdf_per_page", False)),
                        info="多页 PDF 按页拆分为独立结果文件")
                    export_chart_cb = gr.Checkbox(
                        label="导出图表区域为图片",
                        value=bool(_cfg_defaults.get("export_chart", False)),
                        info="将识别出的图表区域单独导出为图片文件")
                    with gr.Row():
                        gr.HTML("")
                        restore_btn = gr.Button("恢复默认值", elem_classes="scan-reset-btn")

    # ================= 事件接线 =================
    # 16 个核心参数顺序（与 _to_canonical / _default_updates 一一对应）
    core_components = [
        vram_ratio_dd,      # 0 显存占用比 → max_pixels
        seal_cb,            # 1 印章
        chart_cb,           # 2 图表
        orient_cb,          # 3 方向分类
        unwarp_cb,          # 4 文本矫正
        ocrblk_cb,          # 5 图像块 OCR
        fmtblk_cb,          # 6 块格式化
        layout_cb,          # 7 版面分析
        prompt_label_dd,    # 8 单图识别类型
        merge_blocks_cb,    # 9 跨栏合并
        threshold_dd,       # 10 版面阈值
        minpix_dd,          # 11 最小像素
        ctx_len_dd,         # 12 上下文长度
        temperature_dd,     # 13 采样温度
        topp_dd,            # 14 top_p
        rep_dd,             # 15 重复惩罚
    ]
    core_components_with_md = [md_state] + core_components
    # 批量识别输出顺序（与 _batch_run.pack 对应）
    batch_outputs = [
        file_table, file_drop, file_count_md, stats_html,
        progress_html, progress_desc_md, log_text,
        md_out, render_out, md_code, img_out,
        dl_target, status_bar, queue_state, last_file,
        result_cache, md_state,
    ]

    # 页面加载：服务横幅 + 设置默认值 + 恢复扫描队列和结果。
    # 用 sessionStorage 里的会话标记 sid 区分「浏览器前进/后退（同标签页会话，恢复）」
    # 与「关闭标签页/浏览器后重开（新会话，清空）」。
    # 注意：demo.load 的 js 返回值不会覆盖后端输入，因此分两步——
    #   1) demo.load 仅用前端 JS 把 sid 写入隐藏 sid_box；
    #   2) sid_box.change 触发时再调用后端 _page_load，从而可靠传入 sid。
    _SID_JS = (
        "() => {"
        "var s = null;"
        "try { s = sessionStorage.getItem('scan_sid'); } catch (e) {}"
        "if (!s) {"
        "  s = 'sid_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 10);"
        "  try { sessionStorage.setItem('scan_sid', s); } catch (e) {}"
        "}"
        "return s;"
        "}"
    )
    demo.load(
        None,
        inputs=None,
        outputs=[sid_box],
        js=_SID_JS,
        api_visibility="private",
    )
    sid_box.change(
        _page_load,
        inputs=[sid_box],
        outputs=[svc_banner] + core_components + [pdf_per_page_cb, export_chart_cb]
                + [file_table, file_drop, file_count_md, stats_html,
                   md_out, render_out, md_code, img_out, last_file, result_cache],
        api_visibility="private",
    )

    # ---- 队列操作 ----
    file_drop.upload(
        _add_files, inputs=[file_drop, queue_state, recursive_cb],
        outputs=[queue_state, file_table, file_drop, file_count_md, stats_html],
        api_visibility="private")
    add_files_btn.upload(
        _add_files, inputs=[add_files_btn, queue_state, recursive_cb],
        outputs=[queue_state, file_table, file_drop, file_count_md, stats_html],
        api_visibility="private")
    add_folder_btn.upload(
        _add_files, inputs=[add_folder_btn, queue_state, recursive_cb],
        outputs=[queue_state, file_table, file_drop, file_count_md, stats_html],
        api_visibility="private")
    remove_btn.click(
        _remove_selected, inputs=[queue_state, sel_row],
        outputs=[queue_state, file_table, file_drop, file_count_md, stats_html, sel_row],
        api_visibility="private")
    clear_queue_btn.click(
        _clear_queue,
        outputs=[queue_state, file_table, file_drop, file_count_md, stats_html, sel_row,
                 md_out, render_out, md_code, img_out, last_file, status_bar],
        api_visibility="private")
    file_table.click(
        _on_row_result_select, inputs=[queue_state, result_cache],
        outputs=[sel_row, md_out, render_out, md_code, img_out, last_file],
        api_visibility="private")

    # ---- 批量处理：开始（生成器实时刷新）/ 停止（取消生成器） ----
    run_evt = start_btn.click(
        _batch_run,
        inputs=[queue_state] + core_components + [pdf_per_page_cb, export_chart_cb],
        outputs=batch_outputs,
        api_visibility="private")
    stop_btn.click(
        _stop_batch, inputs=[queue_state, log_text],
        outputs=[queue_state, file_table, file_count_md, stats_html, log_text, status_bar],
        cancels=[run_evt],
        api_visibility="private")

    # ---- 结果导出（重新解析当前查看的文件：最近成功或队列中点选） ----
    # 导出完成后自动点击下载按钮（Gradio 的 DownloadButton 不会自动触发下载，
    # 需模拟点击其内部 <button> 才能让浏览器弹出下载）
    _AUTO_DL_JS = (
        "() => { const w = document.getElementById('btn-dl-target'); "
        "const b = w ? (w.querySelector('button') || w) : null; "
        "if (b) b.click(); }"
    )
    word_btn.click(
        _export_word, inputs=[last_file] + core_components_with_md + [pdf_per_page_cb, export_chart_cb],
        outputs=[log_text, status_bar, dl_target, last_file],
        api_visibility="private").then(
        None, js=_AUTO_DL_JS, api_visibility="private")
    html_btn.click(
        _export_html_doc, inputs=[last_file] + core_components_with_md + [pdf_per_page_cb, export_chart_cb],
        outputs=[log_text, status_bar, dl_target, last_file],
        api_visibility="private").then(
        None, js=_AUTO_DL_JS, api_visibility="private")
    json_btn.click(
        _export_json_doc, inputs=[last_file] + core_components_with_md + [pdf_per_page_cb, export_chart_cb],
        outputs=[log_text, status_bar, dl_target, last_file],
        api_visibility="private").then(
        None, js=_AUTO_DL_JS, api_visibility="private")
    # 下载 Markdown 原始文件（ZIP：MD + imgs/）
    md_dl_btn.click(
        _export_zip_md, inputs=[last_file] + core_components_with_md + [pdf_per_page_cb, export_chart_cb],
        outputs=[log_text, status_bar, dl_target, last_file],
        api_visibility="private").then(
        None, js=_AUTO_DL_JS, api_visibility="private")

    # 复制 Markdown（带成功提示）。复制「源码」页签的干净 Markdown，
    # 而非 md_out 中为预览内联了 base64 图片的版本。
    copy_md_btn.click(
        None, inputs=[md_code],
        js="""(md) => {
          const txt = md || '';
          function showToast(msg, ok) {
            const old = document.getElementById('scan-copy-toast');
            if (old) old.remove();
            const t = document.createElement('div');
            t.id = 'scan-copy-toast';
            t.textContent = msg;
            t.style.cssText = 'position:fixed;top:20px;left:50%;transform:translateX(-50%);'
              + 'padding:10px 24px;border-radius:8px;font-size:14px;z-index:99999;'
              + 'box-shadow:0 4px 12px rgba(0,0,0,0.15);transition:opacity 0.3s;'
              + (ok ? 'background:#15803d;color:#fff;' : 'background:#dc2626;color:#fff;');
            document.body.appendChild(t);
            setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 300); }, 2000);
          }
          // http 非安全上下文下 navigator.clipboard 不可用，退化为 textarea+execCommand
          try {
            const ta = document.createElement('textarea');
            ta.value = txt;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            const ok = document.execCommand('copy');
            document.body.removeChild(ta);
            if (ok) { showToast('✅ Markdown 已复制到剪贴板', true); return; }
          } catch (e) {}
          try {
            navigator.clipboard.writeText(txt);
            showToast('✅ Markdown 已复制到剪贴板', true);
          } catch (e) {
            showToast('❌ 复制失败，请手动复制', false);
          }
        }""")
    fullscreen_btn.click(
        None,
        js="() => { const c = document.getElementById('result-card'); "
           "if (c) c.classList.toggle('scan-fullscreen'); }")

    # ---- 运行日志 ----
    autoscroll_cb.change(
        None, inputs=[autoscroll_cb],
        js="""(on) => {
          const el = document.querySelector('#scan-log-box textarea') || document.getElementById('scan-log-box');
          if (!el) return;
          if (window._scanLogIv) clearInterval(window._scanLogIv);
          window._scanLogIv = null;
          if (on) window._scanLogIv = setInterval(() => { el.scrollTop = el.scrollHeight; }, 400);
        }""")
    clear_log_btn.click(_clear_log, outputs=[log_text], api_visibility="private")
    log_toggle_btn.click(
        None,
        js="() => { const box = document.getElementById('scan-log-box'); "
           "if (!box) return; const h = box.style.display === 'none'; "
           "box.style.display = h ? '' : 'none'; "
           "const t = document.getElementById('btn-log-toggle'); "
           "if (t) t.textContent = h ? '折叠日志' : '展开日志'; }")

    # ---- 恢复默认值 ----
    restore_btn.click(
        _restore_defaults,
        outputs=core_components + [pdf_per_page_cb, export_chart_cb, status_bar],
        js="(e) => confirm('确定恢复全部默认设置？当前参数将被重置。') ? e : null",
        api_visibility="private")

    # ---- MCP 入口：隐藏按钮，Agent 调用 paddleocr_vl（官方协议签名） ----
    # inputs 顺序 = paddleocr_vl 签名参数顺序；input_data 走 Textbox（URL/路径/
    # data URI 纯字符串，由 _parse_mcp 下载/读取），file_path 为旧版别名组件；
    # 单输出 md_out（Markdown 文本，detailed 时末尾已追加 "Pages: N"）。
    mcp_btn.click(
        paddleocr_vl,
        inputs=[input_data_cb, file_path_alt, output_mode_cb, file_type_cb,
                return_images_cb, runtime_params_cb],
        outputs=[md_out],
        api_name="paddleocr_vl",
    )

# ---- MCP 合规：只暴露 paddleocr_vl 一个工具，内部 UI 事件函数对 MCP 隐藏 ----
# Gradio 会把所有注册了事件的函数默认列为 MCP 工具；这里把内部函数标记为
# 非 tool 类型，使 /gradio_api/mcp/ 的 tools/list 仅返回官方同名的 paddleocr_vl。
for _fn in (_page_load, _add_files, _remove_selected, _clear_queue,
            _on_row_result_select, _batch_run, _stop_batch, _export_word,
            _export_html_doc, _export_json_doc, _export_zip_md, _clear_log,
            _restore_defaults):
    _fn._mcp_type = "hidden"

if __name__ == "__main__":
    # 队列：服务一次只处理一个请求（8GB 显存 + 原生推理），其余排队等待
    demo.queue(max_size=32, default_concurrency_limit=1)
    demo.launch(
        server_name=os.environ.get("GRADIO_SERVER_NAME", "0.0.0.0"),
        server_port=int(os.environ.get("GRADIO_SERVER_PORT", "7860")),
        show_error=True,
        css=CUSTOM_CSS,
        head=SCAN_JS,
        theme=gr.themes.Base(),
        # 允许前端通过文件端点访问 outputs 目录下的解析结果与 PDF 页面预览图
        allowed_paths=[str(_OUTPUT_ROOT)],
        # 内置 MCP 服务器：网页给人用，MCP 给 Agent 用
        mcp_server=os.environ.get("GRADIO_MCP_SERVER", "True").lower() != "false",
    )
