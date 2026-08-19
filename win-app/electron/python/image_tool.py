#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
image_tool.py —— 图像工具后端（PIL + OpenCV，离线可用）
支持：旋转 / 翻转 / 裁剪 / 亮度 / 对比度 / 灰度 / 黑白 / 反相 / 滤镜 / 格式转换
以及"扫描仪效果"：docscan（文档检测+透视校正+去阴影+增强）/ deshadow / retinex / autoorient

用法：
  python image_tool.py --in src.png --ops '[{"rot":90},{"brightness":1.2}]' --out out.png
  python image_tool.py --in src.png --convert jpg --out out.jpg
  python image_tool.py --in src.png --api http://127.0.0.1:8080/layout-parsing \
      --ops '[{"docscan":{"gray":true,"autoorient":true,"layout":true}}]' --out out.png

ops 列表按顺序应用，每个 op 支持：
  {"rot": 90|180|270|-90}
  {"flip": "h"|"v"}
  {"crop": [left, top, right, bottom]}     // 原图像素坐标
  {"brightness": 1.2}                       // 倍率
  {"contrast": 1.1}
  {"grayscale": 1}
  {"bw": 1}                                 // 黑白阈值
  {"invert": 1}
  {"filter": "blur"|"sharpen"|"emboss"|"edge"|"detail"|"smooth"}
  {"docscan": {"gray": false, "autoorient": false, "layout": false}}  // 扫描仪效果
  {"deshadow": 1}                           // 单独去阴影
  {"retinex": {"gray": true}}               // Retinex 增强（FairScan 风格）

输出 JSONL 事件流：{"t":"result","ok":true,"file":"...","width":..,"height":..}
"""
import argparse
import base64
import json
import sys


# =====================================================================
# 扫描仪效果（借鉴 FairScan：文档取景 / 透视校正 / Retinex 增强 / 去阴影）
# =====================================================================

# 标准纸张比例（portrait：高/宽）。与 FairScan PaperFormats 对齐
_STANDARD_RATIOS = [
    ("A3", 420.0 / 297.0), ("A4", 297.0 / 210.0), ("A5", 210.0 / 148.0),
    ("Letter", 279.4 / 215.9), ("Legal", 355.6 / 215.9),
]

# PPDocLayout 中属于"文档正文"的标签（用于智能取景时求并集 bbox）
_MAIN_LABELS = {
    "doc_title", "paragraph_title", "title", "text", "vertical_text", "table",
    "image", "content", "chart", "figure_title", "abstract", "algorithm",
    "reference", "reference_content", "seal", "header_image", "footer_image",
    "formula_number", "inline_formula", "display_formula", "aside_text", "doc",
}


def _to_bgr(img):
    import numpy as np
    return np.array(img.convert("RGB"))[:, :, ::-1]  # PIL RGB -> OpenCV BGR


def _to_pil(bgr):
    import numpy as np
    from PIL import Image
    return Image.fromarray(bgr[:, :, ::-1])


def _order_points(pts):
    """四个角点排序：左上 / 右上 / 右下 / 左下（逆时针，y 向下坐标系）"""
    import numpy as np
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    tl = pts[np.argmin(s)]
    br = pts[np.argmax(s)]
    tr = pts[np.argmin(d)]
    bl = pts[np.argmax(d)]
    return np.array([tl, tr, br, bl], dtype=np.float32)


def _estimate_aspect_ratio(quad, img_w, img_h):
    """消影点法估计文档真实高宽比（移植 FairScan Perspective.estimateRealDimensions）
    返回 (w, h) 像素尺寸（仅比例可信）；退化时返回对边平均"""
    import numpy as np
    tl, tr, br, bl = quad
    w_avg = (np.linalg.norm(tl - tr) + np.linalg.norm(bl - br)) / 2
    h_avg = (np.linalg.norm(tl - bl) + np.linalg.norm(tr - br)) / 2

    def to_h(p):
        return np.array([p[0], p[1], 1.0])

    def cross(a, b):
        return np.cross(a, b)

    def line(p1, p2):
        return cross(to_h(p1), to_h(p2))

    v1h = cross(line(tl, tr), line(bl, br))   # 上下两边交点（水平消影点）
    v2h = cross(line(tl, bl), line(tr, br))   # 左右两边交点（垂直消影点）
    if abs(v1h[2]) < 1e-6 or abs(v2h[2]) < 1e-6:
        return w_avg, h_avg
    # 主点近似用图像中心（移动设备相机常见假设）
    cx, cy = img_w / 2.0, img_h / 2.0
    v1 = np.array([v1h[0] / v1h[2] - cx, v1h[1] / v1h[2] - cy])
    v2 = np.array([v2h[0] / v2h[2] - cx, v2h[1] / v2h[2] - cy])
    f2 = -(v1[0] * v2[0] + v1[1] * v2[1])
    if f2 <= 0:
        return w_avg, h_avg
    f = np.sqrt(f2)
    if f > max(img_w, img_h) * 1.2:          # 透视过弱，消影点远 → 不稳定，回退
        return w_avg, h_avg
    d1 = np.array([v1[0], v1[1], f])
    d2 = np.array([v2[0], v2[1], f])
    n = np.cross(d1, d2)

    def ray(p):
        return np.array([(p[0] - cx) / f, (p[1] - cy) / f, 1.0])

    def corner3d(p):
        r = ray(p)
        return r * (1.0 / n.dot(r))

    x_tl, x_tr, x_br, x_bl = corner3d(tl), corner3d(tr), corner3d(br), corner3d(bl)
    real_w = (np.linalg.norm(x_tr - x_tl) + np.linalg.norm(x_br - x_bl)) / 2
    real_h = (np.linalg.norm(x_bl - x_tl) + np.linalg.norm(x_br - x_tr)) / 2
    if real_w < 1e-3 or real_h < 1e-3:
        return w_avg, h_avg
    return real_w, real_h


def _snap_dims(w, h):
    """按标准纸张比例修正输出宽高（FairScan snapToStandardFormat 思路）"""
    import numpy as np
    ratio = h / w
    r = ratio
    for _name, sr in _STANDARD_RATIOS:
        if abs(ratio - sr) / sr < 0.04:
            r = sr
            break
    area = w * h
    tw = int(round(np.sqrt(area / r)))
    th = int(round(tw * r))
    return max(1, tw), max(1, th)


def detect_document(bgr):
    """CV 文档检测：Canny 边缘 -> 最大轮廓 -> 四边形（失败返回 None）
    思路来自 FairScan DocumentDetection（biggestContour + 四边形近似）"""
    import cv2
    import numpy as np
    h, w = bgr.shape[:2]
    scale = min(1.0, 1000.0 / max(h, w))
    small = cv2.resize(bgr, (int(w * scale), int(h * scale))) if scale < 1.0 else bgr
    sh, sw = small.shape[:2]

    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(gray, 75, 200)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    c = max(contours, key=cv2.contourArea)
    area = abs(cv2.contourArea(c))
    if area < sw * sh * 0.05:          # 太小的轮廓视为无文档
        return None
    peri = cv2.arcLength(c, True)
    approx = cv2.approxPolyDP(c, 0.02 * peri, True)
    if len(approx) == 4:
        pts = approx.reshape(4, 2)
    else:
        rect = cv2.minAreaRect(c)
        pts = cv2.boxPoints(rect)
    if scale < 1.0:
        pts = pts / scale
    return _order_points(pts)


def _warp_document(bgr, quad):
    """透视校正到正视图；输出尺寸用消影点法估计真实比例 + 标准纸张比例修正"""
    import cv2
    import numpy as np
    h_img, w_img = bgr.shape[:2]
    w, h = _estimate_aspect_ratio(quad, w_img, h_img)
    tw, th = _snap_dims(w, h)
    # 长边超限时缩小（文档导出一般为 300dpi A4 ≈ 2480px，这里给宽松上限）
    if max(tw, th) > 4200:
        k = 4200.0 / max(tw, th)
        tw, th = int(tw * k), int(th * k)
    dst = np.array([[0, 0], [tw - 1, 0], [tw - 1, th - 1], [0, th - 1]], dtype=np.float32)
    M = cv2.getPerspectiveTransform(quad, dst)
    return cv2.warpPerspective(bgr, M, (tw, th), flags=cv2.INTER_CUBIC)


def remove_shadows(bgr):
    """形态学背景估计 + 除法归一化去阴影（reference.md 阶段⑤；保留彩色）"""
    import cv2
    import numpy as np
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    l = lab[:, :, 0]
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (25, 25))
    bg = cv2.morphologyEx(l, cv2.MORPH_DILATE, k)
    bg = cv2.GaussianBlur(bg, (51, 51), 0)
    ratio = l.astype(np.float32) / np.maximum(bg.astype(np.float32), 1)
    lab[:, :, 0] = np.clip(ratio * 255.0, 0, 255).astype(np.uint8)
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


def enhance_grayscale(bgr):
    """灰度扫描增强（FairScan enhanceGrayscaleImage：Retinex + 拉伸 + 双边）"""
    import cv2
    import numpy as np
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).astype(np.float32) + 1.0
    max_dim = float(max(bgr.shape[:2]))
    log_i = np.log(gray)
    retinex = np.zeros_like(gray)
    for ks in (max_dim / 6.0, max_dim / 50.0):
        k = int(ks) | 1
        blur = cv2.boxFilter(gray, -1, (k, k))
        retinex += (log_i - np.log(blur + 1.0)) / 2.0
    out = np.exp(retinex)
    p_low, p_high = np.percentile(out, 0.4), np.percentile(out, 99.0)
    out = np.clip((out - p_low) * (255.0 / (p_high - p_low + 1e-6)), 0, 255).astype(np.uint8)
    # 背景向纯白拉伸
    hist = cv2.calcHist([out], [0], None, [256], [0, 256]).ravel()
    mode_val, mode_cnt = 220, 0.0
    for i in range(180, 256):
        if hist[i] > mode_cnt:
            mode_cnt, mode_val = hist[i], i
    if mode_val < 254:
        out = np.clip(out.astype(np.float32) * (255.0 / mode_val), 0, 255).astype(np.uint8)
    out = cv2.bilateralFilter(out, 9, 20.0, 10.0)
    return cv2.cvtColor(out, cv2.COLOR_GRAY2BGR)


def enhance_color(bgr):
    """彩色扫描增强（FairScan multiScaleRetinexOnL 简化：Lab L 通道 MSR）"""
    import cv2
    import numpy as np
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    l = lab[:, :, 0].astype(np.float32) + 1.0
    max_dim = float(max(bgr.shape[:2]))
    log_l = np.log(l)
    ret = np.zeros_like(l)
    for ks in (max_dim / 2.0, max_dim / 10.0, max_dim / 80.0):
        k = max(int(ks), 3) | 1
        blur = cv2.boxFilter(l, -1, (k, k))
        ret += (log_l - np.log(blur + 1.0)) / 3.0
    mn, mx = ret.min(), ret.max()
    ret_n = (ret - mn) / (mx - mn + 1e-6)
    mean_l = l.mean()
    corrected = ret_n * 60.0 + (mean_l - 30.0)
    out = 0.4 * l + 0.6 * corrected
    p_low, p_high = np.percentile(out, 0.1), np.percentile(out, 99.5)
    out = np.clip((out - p_low) * (245.0 - p_low) / (p_high - p_low + 1e-6) + p_low, 0, 255)
    lab[:, :, 0] = out.astype(np.uint8)
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


# ---------------- 结合 PPDocLayout（paddlex serve）增强 ----------------

def _call_layout(bgr, api_url, timeout):
    """调用 paddlex serve 的 /layout-parsing，返回解析结果 dict（失败返回 None）"""
    import cv2
    import urllib.request
    h, w = bgr.shape[:2]
    scale = min(1.0, 1500.0 / max(h, w))
    small = cv2.resize(bgr, (int(w * scale), int(h * scale))) if scale < 1.0 else bgr
    ok, buf = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not ok:
        return None
    payload = {
        "file": base64.b64encode(buf.tobytes()).decode("ascii"),
        "fileType": 1,
        "useDocOrientationClassify": True,
        "useDocUnwarping": False,
        "useSealRecognition": False,
        "useChartRecognition": False,
        "useOcrForImageBlock": False,
        "formatBlockContent": False,
        "language": ["ch", "en"],
        "documentType": "document",
    }
    req = urllib.request.Request(api_url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            j = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None
    res = (j or {}).get("result") or {}
    lpr = (res.get("layoutParsingResults") or [{}])[0]
    pr = lpr.get("prunedResult") or {}
    return pr


def fetch_orientation(bgr, api_url, timeout):
    """自动转正：PP-LCNet 文档方向分类（经 paddlex serve）。返回旋转角度 0/90/180/270"""
    pr = _call_layout(bgr, api_url, timeout)
    if not pr:
        return 0
    angle = (pr.get("doc_preprocessor_res") or {}).get("angle")
    return {0: 0, 1: 90, 2: 180, 3: 270}.get(angle, 0)


def layout_document_quad(bgr, api_url, timeout):
    """PPDocLayout 智能取景：用正文区标签的并集求文档四边形（CV 失败时的增强路径）"""
    import cv2
    import numpy as np
    pr = _call_layout(bgr, api_url, timeout)
    if not pr:
        return None
    boxes = (pr.get("layout_det_res") or {}).get("boxes") or []
    pts = []
    for b in boxes:
        if (b.get("label") or "") in _MAIN_LABELS and b.get("score", 0) > 0.3:
            c = b.get("coordinate") or []
            if len(c) == 4:
                pts.append((c[0], c[1])); pts.append((c[2], c[1]))
                pts.append((c[2], c[3])); pts.append((c[0], c[3]))
    if not pts:
        return None
    hull = cv2.convexHull(np.array(pts, dtype=np.float32).reshape(-1, 2))
    rect = cv2.minAreaRect(hull)
    return _order_points(cv2.boxPoints(rect))


def apply_docscan(img, cfg, api_url):
    """扫描仪效果：方向校正(可选) → 文档取景 → 透视校正 → 去阴影 → 增强"""
    import cv2
    bgr = _to_bgr(img)
    timeout = float(cfg.get("timeout", 60)) if isinstance(cfg, dict) else 60.0
    auto = isinstance(cfg, dict) and cfg.get("autoorient")
    layout = isinstance(cfg, dict) and cfg.get("layout")
    gray = bool(isinstance(cfg, dict) and cfg.get("gray"))

    if auto and api_url:
        deg = fetch_orientation(bgr, api_url, timeout)
        if deg == 90:
            bgr = cv2.rotate(bgr, cv2.ROTATE_90_CLOCKWISE)
        elif deg == 180:
            bgr = cv2.rotate(bgr, cv2.ROTATE_180)
        elif deg == 270:
            bgr = cv2.rotate(bgr, cv2.ROTATE_90_COUNTERCLOCKWISE)

    # 文档取景：CV 为主，失败或显式要求时用 PPDocLayout 增强
    quad = detect_document(bgr)
    used_layout = False
    if quad is None and layout and api_url:
        quad = layout_document_quad(bgr, api_url, timeout)
        used_layout = quad is not None
    elif quad is not None and layout and api_url:
        q2 = layout_document_quad(bgr, api_url, timeout)
        if q2 is not None:
            quad = q2
            used_layout = True

    out = _warp_document(bgr, quad) if quad is not None else bgr
    out = remove_shadows(out)
    out = enhance_grayscale(out) if gray else enhance_color(out)
    return _to_pil(out)


def apply_retinex(img, gray):
    bgr = _to_bgr(img)
    out = enhance_grayscale(bgr) if gray else enhance_color(bgr)
    return _to_pil(out)


# =====================================================================
# 原有基础 ops（PIL）
# =====================================================================

def apply_ops(img, ops, api_url=None):
    from PIL import Image, ImageOps, ImageFilter, ImageEnhance
    for op in ops or []:
        if "rot" in op:
            img = img.rotate(int(op["rot"]), expand=True)
        elif "flip" in op:
            img = ImageOps.flip(img) if op["flip"] == "v" else ImageOps.mirror(img)
        elif "crop" in op:
            l, t, r, b = [int(x) for x in op["crop"]]
            w, h = img.size
            l = max(0, min(l, w)); r = max(l, min(r, w))
            t = max(0, min(t, h)); b = max(t, min(b, h))
            if r - l > 0 and b - t > 0:
                img = img.crop((l, t, r, b))
        elif "brightness" in op:
            img = ImageEnhance.Brightness(img).enhance(float(op["brightness"]))
        elif "contrast" in op:
            img = ImageEnhance.Contrast(img).enhance(float(op["contrast"]))
        elif "grayscale" in op and op["grayscale"]:
            img = img.convert("L").convert("RGB")
        elif "bw" in op and op["bw"]:
            g = ImageOps.grayscale(img)
            img = g.point(lambda v: 255 if v >= 150 else 0).convert("RGB")
        elif "invert" in op and op["invert"]:
            img = ImageOps.invert(img.convert("RGB"))
        elif "filter" in op:
            f = {
                "blur": ImageFilter.BLUR, "sharpen": ImageFilter.SHARPEN,
                "emboss": ImageFilter.EMBOSS, "edge": ImageFilter.FIND_EDGES,
                "detail": ImageFilter.DETAIL, "smooth": ImageFilter.SMOOTH,
            }.get(op["filter"])
            if f:
                img = img.filter(f)
        elif "docscan" in op:
            img = apply_docscan(img, op["docscan"], api_url)
        elif "deshadow" in op and op["deshadow"]:
            img = _to_pil(remove_shadows(_to_bgr(img)))
        elif "retinex" in op:
            cfg = op["retinex"] or {}
            img = apply_retinex(img, bool(isinstance(cfg, dict) and cfg.get("gray")))
    return img


def main():
    class _JsonArgParser(argparse.ArgumentParser):
        # argparse 参数错误默认打 usage 到 stderr 并 exit(2)，stdout 无 JSON。
        # 这里改成输出 JSON 错误，让主进程拿到可读错误。
        def error(self, message):
            print(json.dumps({"t": "result", "ok": False, "error": f"参数错误：{message}"}))
            sys.exit(2)

    ap = _JsonArgParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--ops", default="[]", help="JSON 操作列表；传 '-' 表示从 stdin 读取（避免命令行引号问题）")
    ap.add_argument("--convert", default=None, help="目标格式 jpg/png/tiff/bmp/webp")
    ap.add_argument("--out", required=True)
    ap.add_argument("--api", default=None, help="paddlex 服务地址（docscan 自动转正/PPDocLayout 取景用）")
    args = ap.parse_args()

    try:
        from PIL import Image
        if args.ops == "-":
            ops = json.loads(sys.stdin.read())
        else:
            ops = json.loads(args.ops) if args.ops else []
        img = Image.open(args.src)
        img = apply_ops(img, ops, api_url=args.api)

        raw_fmt = (args.convert or args.out.rsplit(".", 1)[-1].lower())
        fmt = "jpeg" if raw_fmt == "jpg" else raw_fmt
        save_kw = {}
        if fmt == "jpeg":
            img = img.convert("RGB")
            save_kw = {"quality": 92}
        if fmt == "webp":
            save_kw = {"quality": 88}
        img.save(args.out, fmt.upper(), **save_kw)
        print(json.dumps({"t": "result", "ok": True, "file": args.out,
                          "width": img.size[0], "height": img.size[1]}))
    except Exception as e:
        print(json.dumps({"t": "result", "ok": False,
                          "error": f"图像处理失败：{type(e).__name__}: {e}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
