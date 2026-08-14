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
STATUS_PATH = BASE_DIR / "logs" / "status.json"
_LOCK_PATH = BASE_DIR / "logs" / ".status.lock"

# 内置默认配置（web_config.json 不存在或字段缺失时兜底）
DEFAULT_CONFIG = {
    # 后端 PaddleOCR-VL 服务地址（/layout-parsing 接口）
    "api_url": "http://127.0.0.1:8080/layout-parsing",
    # 单次请求超时（秒）
    "timeout": 600,
    # 管理后台密码的 SHA-256（默认密码 admin123，请登录后台后立即修改）
    "admin_password_sha256": "",
    # 服务开关（软门控：关闭时对应入口拒绝解析请求，进程仍常驻）
    "switches": {
        "scan_service": False,  # 总扫描服务：默认关闭，需管理员登录后台手动首次开启
        "web_ui": True,         # 子开关：用户网页（7860）
        "mcp": True,            # 子开关：MCP 服务（/gradio_api/mcp/）
    },
    # 用户网页各选项的【默认勾选状态】（用户仍可在页面上临时更改）
    "defaults": {
        "use_orientation": False, # 文档方向分类
        "use_unwarping": False,   # 文本图像矫正
        "use_seal": False,        # 印章识别
        "use_chart": False,       # 图表解析
        "use_layout_mode": True,      # 版面分析（自动检测分栏、表格、标题、图像区域）
        "use_merge_blocks": True,     # 跨栏分栏合并（跨栏/交错文本合并为连续段落）
        "use_ocr_image_block": False, # 图像块内 OCR（对图片内嵌文字再做一次 OCR）
        "use_format_block": False,    # 块内容格式化（表格/公式等渲染为 Markdown）
        "pdf_per_page": False,        # PDF 每页输出单独文件（多页 PDF 按页拆分为独立结果文件）
        "export_chart": False,        # 导出图表区域为图片（将识别出的图表区域单独导出为图片文件）
        "max_pixels": 0,          # 单图最大像素（0 = 不限制）
        "cache_keep_days": 3,     # 后端临时缓存（outputs/ 目录）保留天数，到期自动清理
        "vl_precision": "q5_k_m", # VL 模型量化精度（fp16 | q8_0 | q5_k_m | q4_k_m）
    },
}

# 运行状态默认结构（logs/status.json）
DEFAULT_STATUS = {
    "state": "idle",     # idle / busy
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
    """环境变量优先，其次配置文件（管理后台可改，即时生效）。"""
    return os.environ.get("PADDLEOCR_API_URL") or load_config()["api_url"]


def get_timeout() -> int:
    env = os.environ.get("PADDLEOCR_API_TIMEOUT")
    return int(env) if env else int(load_config()["timeout"])


def get_restructure_url() -> str:
    """由 /layout-parsing 地址推导 /restructure-pages 地址。"""
    return get_api_url().rsplit("/", 1)[0] + "/restructure-pages"


def get_switches() -> dict:
    return load_config()["switches"]


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
    """带文件锁的状态更新。fields 直接覆盖，bumps 做计数累加。"""
    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_LOCK_PATH, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        st = read_status()
        if fields:
            st.update(fields)
        for k, delta in (bumps or {}).items():
            st[k] = int(st.get(k, 0)) + delta
        st["updated_at"] = time.time()
        _write_status(st)
