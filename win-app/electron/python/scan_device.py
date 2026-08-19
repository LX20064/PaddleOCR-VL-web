#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scan_device.py —— WIA 2.0 扫描仪完整驱动（复刻 UWP「Windows 扫描」功能集）
================================================================================
功能：设备枚举 / 扫描前预览 / 平板或 ADF 多页扫描 / 色彩·DPI·页尺寸设置 /
      JPG·PNG·TIFF·PDF 输出（多页合并）/ 自动增强（去白边·去底色）

输出 JSONL 事件流到 stdout，每行一个 JSON：
  {"t":"devices","ok":true,"list":[{"id":0,"name":"..."}]}
  {"t":"progress","frac":0.4,"desc":"正在扫描第 2/5 页…"}
  {"t":"result","ok":true,"file":"C:\\...\\out.pdf","pages":["C:\\...\\p1.png"],"width":2480,"height":3508,"count":5}
  {"t":"result","ok":false,"error":"..."}

用法：
  python scan_device.py devices
  python scan_device.py preview <device_id> [--dpi 100] [--out file.jpg]
  python scan_device.py scan <device_id> --dpi 300 --mode color --format pdf
      --source flatbed|adf [--pages 5] [--outdir DIR] [--name PREFIX]
      [--enhance] [--preview]

说明：
  - device_id 为 `devices` 返回的 id，即 WIA 原始索引（DeviceInfos 从 1 开始）。
  - ADF 多页：--source adf 时循环取纸；--pages 指定最大页数（缺省 0 = 自动直到无纸）。
  - 无扫描仪 / 驱动缺失时返回 ok:false 的友好错误。
  - 临时页 PNG 写入系统临时目录，最终产物写入 --outdir。
"""
import argparse
import io
import json
import sys
import tempfile
import time
import traceback
from pathlib import Path

# ---------- WIA 2.0 属性常量 ----------
WIA_PROP = {
    "horizontal_resolution": 6146,
    "vertical_resolution": 6147,
    "current_intent": 6144,
    "horizontal_extent": 6148,
    "vertical_extent": 6149,
    "pages": 3096,
    "document_handling_select": 3088,
    "document_handling_status": 3089,
    "preferred_format": 4104,
}
# Current Intent 值
INTENT_COLOR = 1
INTENT_GRAYSCALE = 2
INTENT_TEXT = 4
# Document Handling Select 值
DHS_NONE = 0
DHS_FEEDER = 1
DHS_FLATBED = 2
DHS_DUPLEX = 4
# WIA 传输格式
FMT_PNG = "{B96B3CB0-0728-11D3-9D7B-0000F81EF32E}"
FMT_JPEG = "{B96B3CAE-0728-11D3-9D7B-0000F81EF32E}"
FMT_BMP = "{B96B3CAB-0728-11D3-9D7B-0000F81EF32E}"
FMT_TIFF = "{B96B3CB1-0728-11D3-9D7B-0000F81EF32E}"


def sanitize_name(name):
    """文件名消毒：去掉 Windows 非法字符与路径分隔符，防止 --name 注入路径。"""
    import re

    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name)).strip()
    cleaned = cleaned.replace("..", "_").strip(" .")
    return cleaned or "scan"


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def progress(frac, desc):
    emit({"t": "progress", "frac": max(0.0, min(1.0, frac)), "desc": desc})


def fail(msg):
    emit({"t": "result", "ok": False, "error": msg})
    sys.exit(1)


def _get_wia():
    try:
        import win32com.client
    except ImportError:
        fail("pywin32 未安装：请先在设置中安装后端环境。")
    try:
        return win32com.client.Dispatch("WIA.DeviceManager")
    except Exception as e:
        fail(f"无法连接 WIA（Windows 图像采集服务）：{type(e).__name__}: {e}")


# ---------- 设备 ----------
def list_devices():
    """返回 [{id,name,type}]，仅列出扫描仪（Type=1）。"""
    wia = _get_wia()
    out = []
    try:
        infos = wia.DeviceInfos
        for i in range(1, infos.Count + 1):
            info = infos(i)
            try:
                t = info.Type
                name = info.Properties("Name").Value
            except Exception:
                continue
            if t == 1:
                # id 直接采用 WIA 原始索引（DeviceInfos 从 1 开始），保证 connect_device
                # 能按同一索引寻址。此前用 len(out) 重编号，遇到摄像头(Type=2)等非扫描仪
                # 设备时 id 会与 WIA 索引错位，导致连接错误设备。
                out.append({"id": i, "name": str(name), "type": "scanner"})
    except Exception as e:
        fail(f"枚举扫描仪失败：{type(e).__name__}: {e}")
    if not out:
        fail("未检测到扫描仪。请确认设备已连接、驱动正常，且 Windows 图像采集(WIA)服务已启动。")
    return out


def connect_device(dev_id):
    wia = _get_wia()
    try:
        info = wia.DeviceInfos(int(dev_id))  # id 即 WIA 原始索引（1 起）
    except Exception:
        fail(f"设备 #{dev_id} 不存在，请重新枚举扫描仪。")
    try:
        device = info.Connect()
    except Exception as e:
        fail(f"连接扫描仪失败（{type(e).__name__}: {e}）。请检查设备是否被其他程序占用或已断开。")
    try:
        item = device.Items(1)
    except Exception:
        fail("扫描仪没有可扫描的文档项。")
    return device, item


def set_prop(item, name, value):
    """按名字设置 WIA 属性，带 ID 回退；属性不存在/只读时静默跳过。"""
    try:
        item.Properties(name).Value = value
        return True
    except Exception:
        try:
            item.Properties(WIA_PROP[name]).Value = value
            return True
        except Exception:
            return False


def get_prop(item, name, default=None):
    try:
        return item.Properties(name).Value
    except Exception:
        try:
            return item.Properties(WIA_PROP[name]).Value
        except Exception:
            return default


# ---------- 传输（扫描到内存） ----------
def transfer_one(item, fmt_id=FMT_PNG):
    """Transfer 一张图到内存 bytes。返回 (bytes, width, height)。"""
    img = item.Transfer(fmt_id)
    data = img.FileData
    if isinstance(data, str):
        data = data.encode("latin-1")
    w = int(getattr(img, "Width", 0) or 0)
    h = int(getattr(img, "Height", 0) or 0)
    return bytes(data), w, h


# ---------- 图像后处理 ----------
def auto_enhance(pil_img):
    """自动增强：去白边 + 去底色（提升对比）。返回处理后的 PIL.Image。"""
    from PIL import Image, ImageOps, ImageFilter

    img = pil_img.convert("RGB")
    # 1) 去白边：找非白色内容边界
    gray = ImageOps.grayscale(img)
    bg = gray.filter(ImageFilter.GaussianBlur(1)).point(lambda v: 255 if v >= 235 else 0)
    bbox = ImageOps.invert(bg).getbbox()
    if bbox:
        x1, y1, x2, y2 = bbox
        pad = 6
        w, h = img.size
        x1 = max(0, x1 - pad); y1 = max(0, y1 - pad)
        x2 = min(w, x2 + pad); y2 = min(h, y2 + pad)
        if (x2 - x1) > w * 0.3 and (y2 - y1) > h * 0.3:
            img = img.crop((x1, y1, x2, y2))
    # 2) 去底色：亮部提升、暗部压深，增强文本可读性
    img = img.point(lambda v: min(255, int(v * 1.08)))
    return img


# ---------- 保存 ----------
def save_page(pil_img, fmt, out_path, dpi):
    """保存单页为指定格式（多页格式的 PDF/TIFF 单独处理）。"""
    if fmt == "jpg":
        pil_img.convert("RGB").save(out_path, "JPEG", quality=92, dpi=(dpi, dpi))
    elif fmt == "png":
        pil_img.save(out_path, "PNG", dpi=(dpi, dpi))
    elif fmt == "bmp":
        pil_img.convert("RGB").save(out_path, "BMP")
    else:
        pil_img.save(out_path, "PNG")
    return out_path


def save_multi(pages, fmt, out_path, dpi):
    """多页合并：PDF（PyMuPDF）或 TIFF（PIL）。"""
    from PIL import Image
    if fmt == "pdf":
        try:
            import pymupdf  # PyMuPDF 新 API（1.24+），fitz 为旧名
            fitz = pymupdf
        except ImportError:
            try:
                import fitz
            except ImportError:
                fail("生成 PDF 需要 PyMuPDF，请安装后端依赖（pip install pymupdf）。")
        scale = 72.0 / dpi
        doc = fitz.open()
        for p in pages:
            pix = fitz.Pixmap(str(p))
            w = max(1, pix.width * scale)
            h = max(1, pix.height * scale)
            page = doc.new_page(width=w, height=h)
            page.insert_image(fitz.Rect(0, 0, w, h), pixmap=pix)
            pix = None
        doc.save(str(out_path), deflate=True, garbage=3)
        doc.close()
    else:  # tiff
        imgs = [Image.open(p) for p in pages]
        imgs[0].save(out_path, "TIFF", save_all=True,
                     append_images=imgs[1:], dpi=(dpi, dpi))
        for im in imgs:
            im.close()
    return out_path


# ---------- 子命令 ----------
def cmd_devices(_args):
    devs = list_devices()
    emit({"t": "result", "ok": True, "list": devs})


def cmd_preview(args):
    _, item = connect_device(args.device_id)
    set_prop(item, "horizontal_resolution", int(args.dpi))
    set_prop(item, "vertical_resolution", int(args.dpi))
    set_prop(item, "current_intent", INTENT_COLOR)
    set_prop(item, "horizontal_extent", 0)
    set_prop(item, "vertical_extent", 0)
    try:
        raw, _, _ = transfer_one(item, FMT_PNG)
    except Exception as e:
        fail(f"预览扫描失败（{type(e).__name__}: {e}）。请确认扫描仪可用。")
    from PIL import Image
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        fail("预览图像解析失败。")
    img.thumbnail((1200, 1200))
    out = args.out or Path(tempfile.gettempdir()) / f"paddleocr_preview_{int(time.time()*1000)}.jpg"
    img.save(out, "JPEG", quality=88)
    emit({"t": "result", "ok": True, "path": str(out)})


def cmd_scan(args):
    device, item = connect_device(args.device_id)
    dpi = int(args.dpi)
    mode = args.mode.lower()
    source = args.source.lower()

    # ---- 应用设置 ----
    set_prop(item, "horizontal_resolution", dpi)
    set_prop(item, "vertical_resolution", dpi)
    intent = {"color": INTENT_COLOR, "gray": INTENT_GRAYSCALE, "bw": INTENT_TEXT}.get(mode, INTENT_COLOR)
    set_prop(item, "current_intent", intent)
    set_prop(item, "horizontal_extent", 0)
    set_prop(item, "vertical_extent", 0)

    is_adf = source == "adf"
    if is_adf:
        sel = get_prop(item, "document_handling_select", DHS_FLATBED)
        if int(sel or DHS_FLATBED) == DHS_FLATBED:
            set_prop(item, "document_handling_select", DHS_FEEDER)
        max_pages = int(args.pages or 0)
        # 仅在指定页数时才设置 WIA pages 属性；自动模式（0=直到无纸）不设置，
        # 完全依赖驱动在出纸完毕时抛「纸空」异常退出循环，避免被误限为 1 页。
        if max_pages > 0:
            set_prop(item, "pages", max_pages)
    else:
        set_prop(item, "document_handling_select", DHS_FLATBED)

    # ---- 扫描 ----
    from PIL import Image
    outdir = Path(args.outdir) if args.outdir else Path(tempfile.gettempdir())
    outdir.mkdir(parents=True, exist_ok=True)
    prefix = sanitize_name(args.name or f"scan_{time.strftime('%Y%m%d_%H%M%S')}")
    # 临时页 PNG 放系统临时目录专用子目录，避免污染用户输出目录；
    # 渲染进程读取 pages 路径预览后，由系统临时目录统一清理。
    tmpdir = Path(tempfile.gettempdir()) / f"paddleocr_scan_{int(time.time() * 1000)}"
    tmpdir.mkdir(parents=True, exist_ok=True)
    pages = []  # 临时 PNG 路径
    max_pages = int(args.pages or 0)
    collected = 0
    try:
        while True:
            progress(0, "正在扫描…" if collected == 0 else f"正在扫描第 {collected + 1} 页…")
            try:
                raw, w, h = transfer_one(item, FMT_PNG)
            except Exception as e:
                # ADF 最后一页扫描后驱动通常抛「纸空/无文档」类错误，视为出纸完毕。
                # 仅对明确的“无纸/结束”错误静默收尾；其它异常给出提示，避免把
                # 卡纸/驱动故障误当正常结束导致静默漏页。
                if is_adf and collected > 0:
                    msg = f"{type(e).__name__}: {e}"
                    low = msg.lower()
                    if any(k in low for k in ("paper", "empty", "no more", "无纸", "出纸完毕")):
                        break
                    progress(0.96, f"已扫描 {collected} 页（后续页读取失败：{msg}）")
                    break
                raise
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            if args.enhance:
                img = auto_enhance(img)
            tmp = tmpdir / f"{prefix}_p{collected + 1:03d}.png"
            img.save(tmp, "PNG")
            pages.append(tmp)
            collected += 1
            progress(min(0.95, 0.1 + 0.85 * collected / (max_pages or collected)),
                     f"已完成 {collected} 页")
            if not is_adf:
                break
            if max_pages > 0 and collected >= max_pages:
                break
    except Exception as e:
        if not pages:
            fail(f"扫描失败（{type(e).__name__}: {e}）。请确认纸张已放好、扫描仪可用。")
        # 部分成功：继续处理已有页

    if not pages:
        fail("扫描未产生任何页面。")

    # ---- 输出格式 ----
    fmt = args.format.lower()
    if fmt == "pdf":
        out_file = outdir / f"{prefix}.pdf"
        save_multi(pages, "pdf", out_file, dpi)
    elif fmt == "tiff":
        out_file = outdir / f"{prefix}.tiff"
        # 单页也走 TIFF 保存，避免此前误用 PNG 编码写 .tiff 文件
        save_multi(pages, "tiff", out_file, dpi)
    elif len(pages) == 1:
        out_file = outdir / f"{prefix}.{fmt}"
        save_page(Image.open(pages[0]), fmt, out_file, dpi)
    else:
        # 多页 + 单页格式：逐页保存
        for i, p in enumerate(pages):
            save_page(Image.open(p), fmt, outdir / f"{prefix}_{i + 1:03d}.{fmt}", dpi)
        out_file = outdir  # 语义：目录（多页散图）

    # 真实尺寸（首个页面）
    width = height = 0
    try:
        with Image.open(pages[0]) as im:
            width, height = im.size
    except Exception:
        pass

    # 返回给前端的“页面”列表：单页图片格式应返回实际保存的文件（避免前端把
    # 临时 PNG 当作成品加入托盘/送入 OCR）；PDF/TIFF 返回临时 PNG 作逐页缩略图。
    if fmt in ("jpg", "png", "bmp"):
        result_pages = (
            [str(out_file)]
            if len(pages) == 1
            else [str(outdir / f"{prefix}_{i + 1:03d}.{fmt}") for i in range(len(pages))]
        )
    else:
        result_pages = [str(p) for p in pages]

    emit({"t": "result", "ok": True,
          "file": str(out_file),
          "pages": result_pages,
          "count": len(pages), "width": width, "height": height})


def main():
    class _JsonArgParser(argparse.ArgumentParser):
        # argparse 参数错误（如 device_id 非整数、缺参数）默认打 usage 到 stderr 并 exit(2)，
        # stdout 无 JSON，主进程只能报"Python 脚本退出码 2"。这里改成输出 JSON 错误。
        def error(self, message):
            emit({"t": "result", "ok": False, "error": f"参数错误：{message}"})
            sys.exit(2)

    ap = _JsonArgParser(description="WIA 扫描仪驱动")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_dev = sub.add_parser("devices")
    p_dev.set_defaults(func=cmd_devices)

    p_prev = sub.add_parser("preview")
    p_prev.add_argument("device_id", type=int)
    p_prev.add_argument("--dpi", type=int, default=100)
    p_prev.add_argument("--out", default=None)
    p_prev.set_defaults(func=cmd_preview)

    p_scan = sub.add_parser("scan")
    p_scan.add_argument("device_id", type=int)
    p_scan.add_argument("--dpi", type=int, default=300)
    p_scan.add_argument("--mode", choices=["color", "gray", "bw"], default="color")
    p_scan.add_argument("--format", choices=["jpg", "png", "bmp", "tiff", "pdf"], default="pdf")
    p_scan.add_argument("--source", choices=["flatbed", "adf"], default="flatbed")
    p_scan.add_argument("--pages", type=int, default=0)
    p_scan.add_argument("--outdir", default=None)
    p_scan.add_argument("--name", default=None)
    p_scan.add_argument("--enhance", action="store_true")
    p_scan.add_argument("--preview", action="store_true")
    p_scan.set_defaults(func=cmd_scan)

    args = ap.parse_args()
    try:
        args.func(args)
    except SystemExit:
        raise
    except Exception as e:
        emit({"t": "result", "ok": False, "error": f"扫描出错：{type(e).__name__}: {e}"})
        traceback.print_exc(file=sys.stderr)


if __name__ == "__main__":
    main()
