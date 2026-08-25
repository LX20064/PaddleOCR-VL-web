#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web_common.py —— 用户网页与管理后台共享的配置 / 状态读写工具

配置：web_config.json（管理后台修改；API 地址 / 超时 / 开关即时生效，
      用户网页默认值在页面刷新时生效）
状态：logs/status.json（用户网页实时写入扫描进度与排队计数，
      管理后台读取展示；写文件采用 临时文件+rename 原子替换，计数带文件锁）
"""

import fcntl
import hashlib
import json
import os
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "web_config.json"
LOG_DIR = BASE_DIR / "logs"
STATUS_PATH = BASE_DIR / "logs" / "status.json"
_LOCK_PATH = BASE_DIR / "logs" / ".status.lock"

# 内置默认配置（web_config.json 不存在或字段缺失时兜底）
DEFAULT_CONFIG = {
    # 后端 PaddleOCR-VL 服务地址（/layout-parsing 接口）
    "api_url": "http://127.0.0.1:8080/layout-parsing",
    # 单次请求超时（秒）
    "timeout": 600,
    # 最大并发推理数（网页队列 default_concurrency_limit）。默认 1 = 串行；
    # 增大可同时处理多个请求，但会成倍占用显存（需更大显存显卡，否则 OOM）。
    # 修改后需重启网页服务（web_ocr.py）生效。
    "max_parallel": 1,
    # 管理后台密码的 SHA-256（默认密码 admin123，请登录后台后立即修改）
    "admin_password_sha256": "",
    # 服务开关（软门控：关闭时对应入口拒绝解析请求，进程仍常驻）
    "switches": {
        "scan_service": False,  # 总扫描服务：默认关闭，需管理员登录后台手动首次开启
        "web_ui": True,         # 子开关：用户网页（7860）
        "mcp": True,            # 子开关：MCP 服务（/gradio_api/mcp/）
    },
    # 日志自动清理（管理后台可配置）：单个日志文件超过 trigger_mb 时，
    # 自动截断保留最新 keep_mb 内容，防止长期运行日志无限增长。
    "log_clean": {
        "enabled": True,     # 是否启用自动清理
        "trigger_mb": 11,    # 触发阈值（MB）
        "keep_mb": 3,        # 保留最新内容（MB）
        "interval_sec": 120, # 后台检查间隔（秒）
    },
    # 用户网页各选项的【默认勾选状态】（用户仍可在页面上临时更改）
    "defaults": {
        "use_orientation": True, # 文档方向分类
        "use_unwarping": False,   # 文本图像矫正
        "use_seal": True,        # 印章识别
        "use_chart": True,       # 图表解析
        "use_layout_mode": True,      # 版面分析（自动检测分栏、表格、标题、图像区域）
        "use_merge_blocks": True,     # 跨栏分栏合并（跨栏/交错文本合并为连续段落）
        "use_ocr_image_block": False, # 图像块内 OCR（对图片内嵌文字再做一次 OCR）
        "use_format_block": True,    # 块内容格式化（表格/公式等渲染为 Markdown）
        "pdf_per_page": False,        # PDF 每页输出单独文件（多页 PDF 按页拆分为独立结果文件）
        "export_chart": False,        # 导出图表区域为图片（将识别出的图表区域单独导出为图片文件）
        "max_pixels": 0,          # 单图最大像素（0 = 不限制）
        "pdf_render_dpi": 288,    # PDF 页面渲染 DPI（预览图 / 图表裁剪分辨率）
        "ctx_len": 32768,         # 上下文长度（单页最大输出 token，提交时作为 maxNewTokens）
        "cache_keep_days": 3,     # 后端临时缓存（outputs/ 目录）保留天数，到期自动清理
        "vl_precision": "fp16",  # VL 模型量化精度（fp16 | q8_0 | q5_k_m | q4_k_m）
        # ---- 版面后处理高级参数（None = 使用后端/产线默认值） ----
        "layout_nms": None,                 # 版面检测 NMS（bool | None）
        "layout_unclip_ratio": None,        # 版面框扩张比例（float | dict | None）
        "layout_merge_bboxes_mode": None,   # 版面框合并模式（str | dict | None）
        "layout_shape_mode": None,          # 版面框形状模式（rect|quad|poly|auto|None）
        "vlm_extra_args": None,             # 给 VLM 引擎的额外采样参数（dict | None）
        "markdown_ignore_labels": None,     # Markdown 忽略的块标签（list[str] | None）
    },
}

# 运行状态默认结构（logs/status.json）
DEFAULT_STATUS = {
    "state": "idle",     # idle / busy（由 active 自动推导）
    "active": 0,         # 当前正在并发处理的请求数
    "queued": 0,         # 当前排队等待处理的请求数（实时，清零累计统计不影响）
    "file": "",          # 当前处理的文件名
    "progress": 0.0,     # 0~1
    "desc": "空闲",       # 阶段描述
    "submitted": 0,      # 网页累计点击提交数
    "started": 0,        # 累计开始处理数
    "done": 0,           # 累计完成数（含失败）
    "failed": 0,         # 累计失败数
    "updated_at": 0.0,
}


# ---------------- 配置 ----------------

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _merge(base: dict, override: dict) -> dict:
    """递归合并：override 中的值覆盖 base，缺失字段保留 base。"""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝
    if CONFIG_PATH.exists():
        try:
            user_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg = _merge(cfg, user_cfg)
        except (json.JSONDecodeError, OSError):
            pass
    if not cfg.get("admin_password_sha256"):
        # 首次运行：写入默认密码 admin123 的哈希
        cfg["admin_password_sha256"] = hash_password("admin123")
        save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_api_url() -> str:
    """返回 PaddleOCR-VL 推理服务地址（http/https，不含路径后缀）。

    优先级：项目环境变量 PADDLEOCR_API_URL > 官方 paddleocr-mcp 兼容变量
    （PADDLEOCR_MCP_SELF_HOSTED_BASE_URL / PADDLEOCR_MCP_SERVER_URL）> 配置文件。
    官方变量兼容便于按官方文档方式（Self-hosted API）部署时直接复用同一套配置。
    """
    return (
        os.environ.get("PADDLEOCR_API_URL")
        or os.environ.get("PADDLEOCR_MCP_SELF_HOSTED_BASE_URL")
        or os.environ.get("PADDLEOCR_MCP_SERVER_URL")
        or load_config()["api_url"]
    )


def get_timeout() -> int:
    env = os.environ.get("PADDLEOCR_API_TIMEOUT")
    return int(env) if env else int(load_config()["timeout"])


def get_restructure_url() -> str:
    """由 /layout-parsing 地址推导 /restructure-pages 地址。"""
    return get_api_url().rsplit("/", 1)[0] + "/restructure-pages"


def get_switches() -> dict:
    return load_config()["switches"]


# ---------------- 日志自动清理 ----------------

def get_log_clean() -> dict:
    return dict(load_config().get("log_clean") or {})


def save_log_clean(enabled: bool, trigger_mb: int, keep_mb: int) -> None:
    """保存日志自动清理设置（保留 interval_sec 不变）。"""
    cfg = load_config()
    cfg["log_clean"] = {
        "enabled": bool(enabled),
        "trigger_mb": int(trigger_mb),
        "keep_mb": int(keep_mb),
        "interval_sec": (cfg.get("log_clean") or {}).get("interval_sec", 120),
    }
    save_config(cfg)


def cleanup_logs(trigger_mb=None, keep_mb=None) -> dict:
    """日志自动清理：logs/ 下单个 *.log 文件超过 trigger_mb MB 时，
    截断保留最新 keep_mb MB（按行切分，避免截断一行）。

    参数缺省时读取配置；返回本次清理动作汇总，供管理后台展示。
    写入采用"读尾-覆写"，配合 O_APPEND 写日志的进程不会产生空洞。
    """
    cfg = get_log_clean()
    trigger = int(trigger_mb if trigger_mb else cfg.get("trigger_mb", 11))
    keep = int(keep_mb if keep_mb else cfg.get("keep_mb", 3))
    if trigger < 1:
        trigger = 1
    if keep < 1:
        keep = 1
    trigger_bytes = trigger * 1024 * 1024
    keep_bytes = keep * 1024 * 1024
    actions = []
    if not LOG_DIR.is_dir():
        return {"actions": actions}
    for p in sorted(LOG_DIR.glob("*.log")):
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size <= trigger_bytes:
            continue
        try:
            with open(p, "rb") as f:
                f.seek(max(0, size - keep_bytes))
                f.readline()          # 丢弃可能被截断的半行
                tail = f.read()
            if not tail:
                continue
            with open(p, "wb") as f:
                f.write(tail)
            actions.append({
                "file": p.name,
                "from_mb": round(size / 1048576, 2),
                "to_mb": round(len(tail) / 1048576, 2),
            })
        except OSError:
            continue
    return {"actions": actions}


def log_sizes_md() -> str:
    """logs/ 下各日志文件当前大小（MB），供管理后台展示。"""
    rows, total = [], 0.0
    for p in sorted(LOG_DIR.glob("*.log")):
        try:
            mb = p.stat().st_size / 1048576
        except OSError:
            continue
        total += mb
        rows.append(f"- `{p.name}`：`{mb:.2f} MB`")
    return "**当前日志大小**：\n" + ("\n".join(rows) if rows else "（暂无日志）") \
        + f"\n\n合计 **{total:.2f} MB**"


def log_clean_summary_md(result: dict) -> str:
    """把 cleanup_logs() 的返回汇总为 Markdown 提示。"""
    actions = result.get("actions") or []
    if not actions:
        return "ℹ️ 当前无日志超过阈值，无需清理。"
    lines = [f"- `{a['file']}`：{a['from_mb']}MB → {a['to_mb']}MB" for a in actions]
    return "✅ 已清理日志：\n" + "\n".join(lines)


# ---------------- 运行状态（进度 / 排队） ----------------

def read_status() -> dict:
    st = dict(DEFAULT_STATUS)
    try:
        st.update(json.loads(STATUS_PATH.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass
    return st


def _write_status(st: dict) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATUS_PATH)  # 原子替换，避免读到半个文件


def status_update(fields: dict | None = None, bumps: dict | None = None) -> None:
    """带文件锁的状态更新。fields 直接覆盖，bumps 做计数累加。

    写入前强制清洗计数不变量：failed ≤ done ≤ started ≤ submitted，
    并保证 active/queued（处理中/排队中）不为负。实时排队数 queued 与累计
    统计解耦，清零累计统计（submitted/started/done/failed）不影响排队显示。
    """
    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_LOCK_PATH, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        st = read_status()
        if fields:
            st.update(fields)
        for k, delta in (bumps or {}).items():
            st[k] = int(st.get(k, 0)) + delta
        st["started"] = min(int(st.get("started", 0)), int(st.get("submitted", 0)))
        st["done"] = min(int(st.get("done", 0)), int(st.get("started", 0)))
        st["failed"] = min(int(st.get("failed", 0)), int(st.get("done", 0)))
        # 并发/排队计数：active、queued 均不得为负；state 完全由 active 推导（>0 即 busy）
        st["active"] = max(0, int(st.get("active", 0)))
        st["queued"] = max(0, int(st.get("queued", 0)))
        st["state"] = "busy" if st["active"] > 0 else "idle"
        st["updated_at"] = time.time()
        _write_status(st)
