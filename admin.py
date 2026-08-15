#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PaddleOCR-VL 管理后台（admin.py，默认 7861 端口）
==================================================
路由器式管理页：左侧菜单栏 + 右侧内容区。
默认密码 admin123，登录后请立即修改。

管理内容：
  1. 服务开关  —— 总扫描服务开关 + 网页服务 / MCP 服务两个子开关（软门控，即时生效）
  2. 运行监控  —— 当前扫描进度、排队数量、累计统计（每 5 秒自动刷新）
  3. 默认设置  —— 用户网页各功能开关的默认勾选状态、后端 API 地址、超时
  4. 服务状态  —— API 服务连通性、GPU 显存 / 利用率
  5. 日志查看  —— api.log / web.log / admin.log 尾部内容
  6. 安全设置  —— 修改管理密码

说明：开关为"软门控"——paddlex 服务进程常驻（模型加载耗时数分钟），
关闭时用户网页与 MCP 入口拒绝解析请求并提示，恢复即时生效。

环境变量（可选）：
  ADMIN_SERVER_NAME  监听地址，默认 0.0.0.0
  ADMIN_SERVER_PORT  监听端口，默认 7861
"""

import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

# 完全离线本地部署：关闭 Gradio 遥测与版本检查，避免启动时访问外网
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

import gradio as gr
import requests

from web_common import (
    DEFAULT_CONFIG,
    hash_password,
    load_config,
    read_status,
    save_config,
    status_update,
)

BASE_DIR = Path(__file__).resolve().parent

SECTION_LABELS = [
    "服务开关",
    "运行监控",
    "默认设置",
    "服务状态",
    "日志查看",
    "安全设置",
]

# ============================================================
# 登录
# ============================================================

def _log(msg):
    """写入运行日志（stdout 由 start_all.sh 重定向到 logs/admin.log），
    供后台「日志查看」追溯关键操作。flush=True 避免块缓冲导致日志滞后。"""
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] [admin] {msg}", flush=True)


# ---- 版面后处理高级参数：UI ↔ 配置值转换（与扫描页 web_ocr.py 约定一致） ----
_ADMIN_OPT_NONE = "留空"
_ADMIN_OPT_ON = "开启"
_ADMIN_OPT_OFF = "关闭"


def _tri_bool_val(v):
    """「留空/开启/关闭」→ None/True/False。"""
    if v in (None, _ADMIN_OPT_NONE, ""):
        return None
    return str(v) == _ADMIN_OPT_ON


def _tri_bool_ui(v):
    """None/True/False → 「留空/开启/关闭」。"""
    if v is None:
        return _ADMIN_OPT_NONE
    return _ADMIN_OPT_ON if bool(v) else _ADMIN_OPT_OFF


def _parse_extra_text(text):
    """高级参数文本框 → Python 值：空/留空 → None；JSON 优先，否则视为裸字符串。"""
    if text is None:
        return None
    s = str(text).strip()
    if not s or s == _ADMIN_OPT_NONE:
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return s


def _extra_text_ui(v):
    """Python 值 → 高级参数文本框内容（None→空，字符串原样，其余 JSON 序列化）。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False)


def _shape_mode_ui(v):
    """layout_shape_mode → 下拉框值（None→留空，其余原样字符串）。"""
    return _ADMIN_OPT_NONE if v in (None, "") else str(v)


def _extra_default_updates(d):
    """返回 6 个版面后处理高级参数的默认更新值（与 UI 组件顺序一致）。"""
    return (
        gr.update(value=_tri_bool_ui(d.get("layout_nms"))),
        gr.update(value=_extra_text_ui(d.get("layout_unclip_ratio"))),
        gr.update(value=_extra_text_ui(d.get("layout_merge_bboxes_mode"))),
        gr.update(value=_shape_mode_ui(d.get("layout_shape_mode"))),
        gr.update(value=_extra_text_ui(d.get("vlm_extra_args"))),
        gr.update(value=_extra_text_ui(d.get("markdown_ignore_labels"))),
    )


def do_login(password: str):
    """校验密码，成功后显示管理面板并载入当前配置。
    密码错误时返回页面内错误提示，不抛出 gr.Error，避免必须刷新页面。"""
    cfg = load_config()
    if hash_password(password) != cfg["admin_password_sha256"]:
        _log("登录失败：密码错误")
        # 保持登录框可见，其他组件不更新，仅在 login_msg 显示错误
        return (
            gr.update(), gr.update(),
            *([gr.update()] * len(_section_updates(0))),
            gr.update(), gr.update(), gr.update(), gr.update(),
            gr.update(), gr.update(), gr.update(),
            gr.update(), gr.update(), gr.update(), gr.update(),
            gr.update(), gr.update(), gr.update(), gr.update(),
            gr.update(), gr.update(),
            gr.update(), gr.update(), gr.update(),
            *([gr.update()] * 6),  # 版面后处理高级参数（未登录不更新）
            gr.update(value="⚠️ **密码错误**，请重新输入。"),
            gr.update(value=False),  # login_state
        )
    d = cfg["defaults"]
    sw = cfg["switches"]
    _log("登录成功")
    return (
        gr.update(visible=False),          # 隐藏登录框
        gr.update(visible=True),           # 显示管理面板
        *_section_updates(0),              # 默认显示第一个菜单（展开列表）
        sw["scan_service"], sw["web_ui"], sw["mcp"],
        _switch_summary(sw),
        cfg["api_url"], cfg["timeout"], cfg.get("max_parallel", 1),
        d.get("use_orientation", False), d.get("use_unwarping", False),
        d.get("use_seal", False), d.get("use_chart", False),
        d.get("use_layout_mode", True), d.get("use_merge_blocks", True),
        d.get("use_ocr_image_block", False), d.get("use_format_block", False),
        d.get("pdf_per_page", False), d.get("export_chart", False),
        d.get("max_pixels", 0),
        d.get("cache_keep_days", 3), d.get("vl_precision", "q5_k_m"),
        *_extra_default_updates(d),
        gr.update(value=""),               # 清空错误提示
        gr.update(value=True),            # login_state → 已登录
    )


def _restore_login(login_state):
    """页面加载时恢复登录状态：若已登录则直接显示管理面板并载入配置。"""
    if not login_state:
        return (
            gr.update(visible=True),       # 显示登录框
            gr.update(visible=False),      # 隐藏管理面板
            *([gr.update()] * len(_section_updates(0))),
            gr.update(), gr.update(), gr.update(), gr.update(),
            gr.update(), gr.update(), gr.update(),
            gr.update(), gr.update(), gr.update(), gr.update(),
            gr.update(), gr.update(), gr.update(), gr.update(),
            gr.update(), gr.update(),
            gr.update(), gr.update(), gr.update(),
            *([gr.update()] * 6),  # 版面后处理高级参数（未登录不更新）
            gr.update(value=""),
            gr.update(value=False),
        )
    cfg = load_config()
    d = cfg["defaults"]
    sw = cfg["switches"]
    return (
        gr.update(visible=False),          # 隐藏登录框
        gr.update(visible=True),           # 显示管理面板
        *_section_updates(0),
        sw["scan_service"], sw["web_ui"], sw["mcp"],
        _switch_summary(sw),
        cfg["api_url"], cfg["timeout"], cfg.get("max_parallel", 1),
        d.get("use_orientation", False), d.get("use_unwarping", False),
        d.get("use_seal", False), d.get("use_chart", False),
        d.get("use_layout_mode", True), d.get("use_merge_blocks", True),
        d.get("use_ocr_image_block", False), d.get("use_format_block", False),
        d.get("pdf_per_page", False), d.get("export_chart", False),
        d.get("max_pixels", 0),
        d.get("cache_keep_days", 3), d.get("vl_precision", "q5_k_m"),
        *_extra_default_updates(d),
        gr.update(value=""),               # 清空错误提示
        gr.update(value=True),
    )


def do_logout():
    """退出登录：清除登录状态，返回登录页。"""
    _log("退出登录")
    return (
        gr.update(visible=True),           # 显示登录框
        gr.update(visible=False),          # 隐藏管理面板
        *([gr.update()] * len(_section_updates(0))),
        gr.update(), gr.update(), gr.update(), gr.update(),
        gr.update(), gr.update(), gr.update(),
        gr.update(), gr.update(), gr.update(), gr.update(),
        gr.update(), gr.update(), gr.update(), gr.update(),
        gr.update(), gr.update(),
        gr.update(), gr.update(), gr.update(),
        *([gr.update()] * 6),  # 版面后处理高级参数（退出登录不更新）
        gr.update(value=""),               # 清空错误提示
        gr.update(value=False),           # login_state → 未登录
        gr.update(value=""),              # 清空密码框
    )


def _section_updates(active_idx: int):
    """返回各内容区 visible 更新，以及侧边栏按钮样式更新。"""
    cols = [gr.update(visible=(i == active_idx)) for i in range(len(SECTION_LABELS))]
    btns = [
        gr.update(variant="primary" if i == active_idx else "secondary")
        for i in range(len(SECTION_LABELS))
    ]
    return cols + btns


# ============================================================
# 服务开关
# ============================================================

def _switch_summary(sw: dict) -> str:
    if not sw["scan_service"]:
        return (
            "## 🔴 扫描服务：关闭\n"
            "用户网页与 MCP 当前都会拒绝解析请求。打开下方「总扫描服务」开关后恢复。"
        )
    sub = []
    sub.append(f"网页 {'🟢' if sw['web_ui'] else '🔴（已关闭）'}")
    sub.append(f"MCP {'🟢' if sw['mcp'] else '🔴（已关闭）'}")
    return f"## 🟢 扫描服务：开启\n子开关状态：{' ｜ '.join(sub)}"


def _require_login(logged_in) -> None:
    """服务端登录态校验（写操作）。

    login_state 是 gr.State（按浏览器会话在服务端存储，默认 False）；
    匿名 HTTP API 调用（/gradio_api/call/...）没有登录会话，拿到的是新会话默认值
    False，从而被此处拦截——api_visibility="private" 只是隐藏 API，并不阻止执行。
    """
    if not logged_in:
        raise gr.Error("未登录或会话已过期，请登录后重试。")


def save_switches(master, web, mcp, logged_in):
    _require_login(logged_in)
    cfg = load_config()
    cfg["switches"] = {
        "scan_service": bool(master),
        "web_ui": bool(web),
        "mcp": bool(mcp),
    }
    save_config(cfg)
    _log(f"服务开关已保存：scan_service={bool(master)} web_ui={bool(web)} mcp={bool(mcp)}")
    msg = "✅ 已保存，即时生效（无需重启任何服务）。"
    if master and not (web or mcp):
        msg += "\n\n⚠️ 注意：两个子开关都关着，总开关虽开但没有任何可用入口。"
    return msg, _switch_summary(cfg["switches"])


# ============================================================
# 运行监控
# ============================================================

def refresh_monitor(logged_in):
    # Timer 每 5 秒触发（含未登录页面），未登录时静默不更新，不弹错误提示
    if not logged_in:
        return gr.update()
    st = read_status()
    sw = load_config()["switches"]
    waiting = int(st.get("queued", 0))   # 当前排队等待处理的请求数（实时，清零累计统计不影响）
    active = int(st.get("active", 0))    # 当前正在并发处理的请求数
    max_parallel = int(load_config().get("max_parallel", 1))

    if st["state"] == "busy":
        prog_txt = f"🔄 处理中：**{st['file']}** — {int(st['progress'] * 100)}%（{st['desc']}）"
    else:
        prog_txt = f"💤 空闲（{st['desc']}）"

    ts = (
        datetime.datetime.fromtimestamp(st["updated_at"]).strftime("%Y-%m-%d %H:%M:%S")
        if st["updated_at"]
        else "—"
    )
    return (
        f"**服务开关**：{_switch_summary(sw).split(chr(10))[0].replace('#', '').strip()}\n\n"
        f"**当前进度**：{prog_txt}\n\n"
        f"**排队等待**：{waiting} 个 ｜ **处理中**：{active} 个（最大并发 {max_parallel}）\n\n"
        f"**累计统计**：提交 {st['submitted']} ｜ 完成 {st['done'] - st['failed']} ｜ 失败 {st['failed']}\n\n"
        f"<sub>最近更新：{ts}（本页每 5 秒自动刷新）</sub>"
    )


def reset_stats(logged_in):
    """清零累计统计（提交 / 开始 / 完成 / 失败）。

    不影响实时显示：当前进度、处理中（active）、排队等待（queued）均保持不变。
    """
    _require_login(logged_in)
    status_update(fields={
        "submitted": 0,
        "started": 0,
        "done": 0,
        "failed": 0,
    })
    _log("累计统计已清零")
    return refresh_monitor(logged_in)


# ============================================================
# 默认设置
# ============================================================

def save_defaults(api_url, timeout, max_parallel, orientation, unwarping, seal, chart,
                  layout_mode, merge_blocks, ocr_image_block, format_block,
                  pdf_per_page, export_chart,
                  max_pixels, cache_days, vl_precision,
                  layout_nms, layout_unclip, layout_merge, layout_shape,
                  vlm_extra, md_ignore, logged_in):
    _require_login(logged_in)
    cfg = load_config()
    api_url = (api_url or "").strip()
    if api_url and not api_url.startswith(("http://", "https://")):
        raise gr.Error("API 地址必须以 http:// 或 https:// 开头")
    cfg["api_url"] = api_url or cfg["api_url"]
    cfg["timeout"] = max(10, int(timeout))
    cfg["max_parallel"] = max(1, min(8, int(max_parallel)))
    cfg["defaults"] = {
        "use_orientation": bool(orientation),
        "use_unwarping": bool(unwarping),
        "use_seal": bool(seal),
        "use_chart": bool(chart),
        "use_layout_mode": bool(layout_mode),
        "use_merge_blocks": bool(merge_blocks),
        "use_ocr_image_block": bool(ocr_image_block),
        "use_format_block": bool(format_block),
        "pdf_per_page": bool(pdf_per_page),
        "export_chart": bool(export_chart),
        "max_pixels": int(max_pixels),
        "cache_keep_days": max(1, int(cache_days)),
        "vl_precision": str(vl_precision or "q5_k_m"),
        "layout_nms": _tri_bool_val(layout_nms),
        "layout_unclip_ratio": _parse_extra_text(layout_unclip),
        "layout_merge_bboxes_mode": _parse_extra_text(layout_merge),
        "layout_shape_mode": _parse_extra_text(layout_shape),
        "vlm_extra_args": _parse_extra_text(vlm_extra),
        "markdown_ignore_labels": _parse_extra_text(md_ignore),
    }
    # 确保配置文件中所有默认值键都存在
    for key, default_val in DEFAULT_CONFIG["defaults"].items():
        if key not in cfg["defaults"]:
            cfg["defaults"][key] = default_val
    save_config(cfg)
    _log(f"默认设置已保存：api_url={cfg['api_url']} timeout={cfg['timeout']} "
         f"max_parallel={cfg['max_parallel']} vl_precision={cfg['defaults']['vl_precision']}")
    return (
        "✅ 已保存。用户网页【刷新页面】后按新默认值显示；API 地址与超时立即生效。\n\n"
        f"⚙️ 最大并发推理数已设为 **{cfg['max_parallel']}**，"
        "**需重启网页服务（web_ocr.py）后生效**。"
        "并发 > 1 会成倍占用显存，请确认显卡显存充足，否则会 OOM。"
    )


def clear_cache_now(logged_in):
    """立即清理后端临时缓存（outputs/ 目录下所有任务子目录），返回释放空间统计。"""
    _require_login(logged_in)
    import shutil

    out_root = BASE_DIR / "outputs"
    keep_days = int(load_config()["defaults"].get("cache_keep_days", 3))
    if not out_root.is_dir():
        return (
            "✅ 缓存目录不存在，无需清理。\n\n"
            f"当前策略：新任务生成时自动清理 {keep_days} 天前的旧文件。"
        )
    freed = 0
    count = 0
    for child in out_root.iterdir():
        if child.is_dir():
            size = sum(f.stat().st_size for f in child.rglob("*") if f.is_file())
            shutil.rmtree(child, ignore_errors=True)
            freed += size
            count += 1
    if freed >= 1024 * 1024:
        size_txt = f"{freed / 1024 / 1024:.1f} MB"
    else:
        size_txt = f"{freed / 1024:.0f} KB"
    return (
        f"✅ 已清理 {count} 个缓存任务目录，释放 {size_txt}。\n\n"
        f"当前策略：新任务生成时自动清理 {keep_days} 天前的旧文件。"
    )


# ============================================================
# 服务状态 / VL 模型切换 / 日志 / 密码
# ============================================================

VL_PRECISION_CHOICES = [
    ("FP16 — 最高精度，显存占用最高，接近原始质量", "fp16"),
    ("Q8_0 — 高精度，接近 FP16，显存占用高", "q8_0"),
    ("Q5_K_M — 中等精度，质量与速度兼顾（默认推荐）", "q5_k_m"),
    ("Q4_K_M — 低精度，显存占用低，省显存", "q4_k_m"),
]

VL_PRECISION_HINT = (
    "**精度对照表**\n\n"
    "| 精度 | 显存 | 质量 | 适用场景 |\n"
    "|------|------|------|----------|\n"
    "| FP16 | 最高 | 原始 | 高精度需求 |\n"
    "| Q8_0 | 高 | 接近FP16 | 质量优先 |\n"
    "| **Q5_K_M** | 中 | 很好 | **默认推荐** |\n"
    "| Q4_K_M | 低 | 佳 | 调试与原型 |"
)


def switch_vl_model(precision, logged_in):
    """热切换 VL 模型精度：先检查扫描状态，无冲突则重启 llama-server。
    如果所选精度与当前相同则跳过。返回 (消息, 精度值)。
    失败时 Radio 回退到当前配置中的精度，保持 UI 与配置一致。"""
    _require_login(logged_in)
    # 当前配置中的精度（用于失败时回退 Radio 显示）
    current = load_config()["defaults"].get("vl_precision", "q5_k_m")
    rollback = gr.update(value=current)

    if not precision:
        return "❌ 请先选择一个精度。", rollback

    # 检查是否与当前精度相同
    if current == precision:
        # 切换失败时 Radio 回退到 current 会再次触发本函数（change 对程序化更新同样生效）。
        # 此处不更新消息内容，避免覆盖上方真正需要用户看到的失败原因。
        return gr.update(), rollback

    # 1) 检查是否有扫描正在进行
    _log(f"请求切换 VL 精度：{current} → {precision}")
    st = read_status()
    if st["state"] == "busy":
        _log("切换被拒绝：当前有扫描任务进行中")
        return (
            "⚠️ **切换失败**：当前有扫描任务正在处理中（"
            f"`{st.get('file', '未知')}`），请等待扫描完成后再切换模型。",
            rollback,
        )

    # 2) 按需准备目标精度模型（下载官方模型 + GGUF 转换/量化；已存在则秒级跳过）
    ensure_script = BASE_DIR / "model_manager.py"
    try:
        result = subprocess.run(
            [sys.executable, str(ensure_script), "ensure-vl", "--precision", str(precision)],
            capture_output=True, text=True, timeout=7200,
            cwd=str(BASE_DIR),
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            _log(f"VL 模型准备失败（{precision}）")
            return (
                "❌ **模型准备失败**（当前运行的模型不受影响）。\n\n"
                f"```\n{err[-800:]}\n```",
                rollback,
            )
    except subprocess.TimeoutExpired:
        return "❌ **模型准备超时**，请查看日志后重试。", rollback
    except Exception as e:
        return f"❌ **模型准备异常**：{e}", rollback

    # 3) 停止当前 llama-server
    pid_file = BASE_DIR / "llama.pid"
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            # 终止整个进程组
            subprocess.run(["kill", "--", f"-{old_pid}"], capture_output=True, timeout=5)
            subprocess.run(["kill", str(old_pid)], capture_output=True, timeout=5)
            # 等待进程退出
            import time as _time
            for _ in range(10):
                result = subprocess.run(["kill", "-0", str(old_pid)], capture_output=True)
                if result.returncode != 0:
                    break
                _time.sleep(0.5)
            pid_file.unlink(missing_ok=True)
        except Exception as e:
            return f"❌ 停止旧 llama-server 失败：{e}", rollback

    # 4) 启动新模型（模型已就绪，通常数秒内完成）
    script = BASE_DIR / "start_llama_server.sh"
    try:
        result = subprocess.run(
            ["bash", str(script), str(precision)],
            capture_output=True, text=True, timeout=120,
            cwd=str(BASE_DIR),
        )
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip()
            # 旧服务已在上方停止：尽力恢复原精度，避免扫描服务整体中断
            back = subprocess.run(
                ["bash", str(script), str(current)],
                capture_output=True, text=True, timeout=120,
                cwd=str(BASE_DIR),
            )
            note = ("已自动恢复原精度模型服务。" if back.returncode == 0
                    else "⚠️ 自动恢复原精度服务也失败，请查看日志后手动执行 start_all.sh。")
            _log(f"启动 {precision} 失败：{err[:120]}；自动恢复原精度"
                 f"{'成功' if back.returncode == 0 else '失败'}")
            return f"❌ 启动新模型失败：{err[:300]}\n\n{note}", rollback
    except Exception as e:
        return f"❌ 启动新模型超时或异常：{e}", rollback

    # 5) 保存到配置
    cfg = load_config()
    cfg["defaults"]["vl_precision"] = str(precision)
    save_config(cfg)

    label = dict(VL_PRECISION_CHOICES).get(precision, precision)
    _log(f"VL 精度已切换：{current} → {precision}，llama-server 已重启")
    return (
        f"✅ 已切换至 **{label.split('—')[0].strip()}**，"
        f"llama-server 已重启，模型加载完成后扫描服务自动恢复。",
        gr.update(value=precision),
    )


def check_status(logged_in):
    # Timer 每 5 秒触发（含未登录页面），未登录时静默不更新
    if not logged_in:
        return gr.update()
    cfg = load_config()
    base = cfg["api_url"].rsplit("/", 1)[0]

    # --- API 服务 ---
    # 探活用 /health（2xx 视为在线）；根路径 / 对 paddlex serve 恒返 404，不能作为存活判据
    try:
        r = requests.get(base + "/health", timeout=5)
        if r.status_code < 400:
            api_txt = f"✅ 在线（HTTP {r.status_code}）— {base}"
        else:
            api_txt = f"⚠️ 响应异常（HTTP {r.status_code}）— {base}"
    except Exception as e:
        api_txt = f"❌ 不可达 — {base}（{e}）"

    # --- VL 模型（llama-server）---
    try:
        r2 = requests.get("http://127.0.0.1:8081/health", timeout=3)
        if r2.status_code < 400:
            vl_txt = f"✅ 在线（HTTP {r2.status_code}）"
        else:
            vl_txt = f"⚠️ 响应异常（HTTP {r2.status_code}）"
    except Exception:
        vl_txt = "❌ 不可达（llama-server 未运行或加载中）"

    # --- 当前精度 ---
    precision = cfg["defaults"].get("vl_precision", "q5_k_m")
    precision_map = {
        "fp16": "FP16（原始）", "q8_0": "Q8_0", "q5_k_m": "Q5_K_M", "q4_k_m": "Q4_K_M"
    }
    precision_label = precision_map.get(precision, precision)

    # --- GPU 详细信息 ---
    gpu_parts = []
    try:
        queries = [
            ("name", "型号"),
            ("memory.used", "显存已用"),
            ("memory.total", "显存总量"),
            ("memory.free", "显存空闲"),
            ("utilization.gpu", "GPU 利用率"),
            ("utilization.memory", "显存利用率"),
            ("temperature.gpu", "GPU 温度"),
            ("power.draw", "功耗"),
            ("fan.speed", "风扇转速"),
        ]
        query_str = ",".join(q[0] for q in queries)
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={query_str}",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            values = [v.strip() for v in out.stdout.strip().split(", ")]
            # nounits 输出为裸数字，按指标补回单位；
            # 温度跳过普通行，在下方统一以带阈值判断的高亮行展示，避免重复。
            units = {"显存已用": " MB", "显存总量": " MB", "显存空闲": " MB",
                     "GPU 利用率": " %", "显存利用率": " %", "功耗": " W", "风扇转速": " %"}
            for i, (_, label) in enumerate(queries):
                if i >= len(values) or label == "GPU 温度":
                    continue
                gpu_parts.append(f"  {label}：`{values[i]}{units.get(label, '')}`")
            # 计算显存使用百分比
            if len(values) >= 7:
                try:
                    temp = int(values[6]) if values[6] != "[N/A]" else None
                    if temp is not None:
                        gpu_parts.append(f"  **GPU 温度**：`{temp}°C`"
                                         f"{' ⚠️ 偏高' if temp > 80 else ''}")
                except ValueError:
                    pass
        else:
            gpu_parts.append("  （无输出）")
    except Exception as e:
        gpu_parts.append(f"  nvidia-smi 异常：{e}")

    # --- CPU / 内存 ---
    cpu_parts = []
    try:
        import psutil
        cpu_pct = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        cpu_parts.append(f"CPU 利用率：`{cpu_pct}%`")
        cpu_parts.append(f"内存：`{mem.used // (1024**2)}MB / {mem.total // (1024**2)}MB` ({mem.percent}%)")
        # 读取 CPU 温度（若有）
        try:
            temps = psutil.sensors_temperatures()
            for name, entries in temps.items():
                for entry in entries:
                    cpu_parts.append(f"CPU 温度 ({name})：`{entry.current}°C`")
        except Exception:
            pass
    except ImportError:
        cpu_parts.append("psutil 未安装，CPU/内存信息不可用")

    lines = [
        f"**API 服务**：{api_txt}",
        f"**VL 模型（llama-server :8081）**：{vl_txt}",
        f"**当前精度**：`{precision_label}`",
        "",
        "### GPU 设备",
        *gpu_parts,
    ]
    if cpu_parts:
        lines.extend(["", "### 系统资源", *cpu_parts])

    # --- llama-server 运行时长 ---
    pid_file = BASE_DIR / "llama.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            result = subprocess.run(
                ["ps", "-o", "etime=", "-p", str(pid)],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                uptime = result.stdout.strip()
                lines.append(f"\nllama-server 运行时长：`{uptime}`")
        except Exception:
            pass

    return "\n".join(lines)


def read_log(which, lines, logged_in):
    # Timer 每 5 秒触发（含未登录页面），未登录时静默不更新
    if not logged_in:
        return gr.update()
    name = {"API 服务": "api.log", "用户网页": "web.log",
            "管理后台": "admin.log", "Llama 服务": "llama.log"}.get(which, "api.log")
    p = BASE_DIR / "logs" / name
    if not p.exists():
        return f"日志不存在：logs/{name}（若未用 start_all.sh 启动，请查看对应终端或 journalctl）"
    content = p.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = content[-int(lines):]
    return "\n".join(tail) if tail else "（日志为空）"


def change_password(old_pw, new_pw1, new_pw2, logged_in):
    _require_login(logged_in)
    cfg = load_config()
    if hash_password(old_pw) != cfg["admin_password_sha256"]:
        _log("修改密码失败：当前密码错误")
        raise gr.Error("当前密码错误")
    if not new_pw1 or len(new_pw1) < 6:
        raise gr.Error("新密码至少 6 位")
    if new_pw1 != new_pw2:
        raise gr.Error("两次输入的新密码不一致")
    cfg["admin_password_sha256"] = hash_password(new_pw1)
    save_config(cfg)
    _log("管理密码已修改")
    return "✅ 密码已更新，下次登录生效"


# ============================================================
# 界面
# ============================================================

CUSTOM_CSS = r"""
:root {
  --router-primary: #1a4a8c;
  --router-hover: #2a5aa0;
  --router-active: #3a6ab0;
  --router-bg: #f0f4f8;
}
body {
  background: var(--router-bg) !important;
}
.router-login-box {
  max-width: 420px;
  margin: 80px auto 0;
  background: #ffffff;
  border-radius: 12px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.08);
  padding: 32px;
}
.router-layout {
  display: flex;
  min-height: 85vh;
  margin: -16px;
}
.router-sidebar {
  width: 220px;
  background: var(--router-primary);
  padding: 16px 0;
  flex-shrink: 0;
}
.router-sidebar-title {
  color: #ffffff;
  font-size: 16px;
  font-weight: 700;
  padding: 0 16px 16px;
  border-bottom: 1px solid rgba(255,255,255,0.15);
  margin-bottom: 8px;
}
.router-menu-btn {
  width: 100%;
  text-align: left;
  background: transparent !important;
  color: #ffffff !important;
  border: none !important;
  border-radius: 0 !important;
  box-shadow: none !important;
  padding: 12px 20px !important;
  font-size: 14px !important;
}
.router-menu-btn:hover {
  background: var(--router-hover) !important;
}
.router-menu-btn-primary {
  background: var(--router-active) !important;
  font-weight: 600;
}
.router-content {
  flex: 1;
  background: #ffffff;
  padding: 24px 32px;
  overflow: auto;
}
.router-section-title {
  font-size: 20px;
  font-weight: 700;
  color: #1a202c;
  margin-bottom: 20px;
  padding-bottom: 10px;
  border-bottom: 2px solid #e2e8f0;
}
.router-card {
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 20px;
  margin-bottom: 20px;
}
.router-hint {
  color: #718096;
  font-size: 13px;
  margin-bottom: 16px;
}
"""

with gr.Blocks(title="PaddleOCR-VL 管理后台") as app:
    login_state = gr.State(False)  # 登录状态：True=已登录，会话级（关闭页面/浏览器后失效）

    # ---------- 登录层 ----------
    with gr.Column(visible=True, elem_classes="router-login-box") as login_col:
        gr.Markdown(
            "# 🔐 PaddleOCR-VL 管理后台\n"
            "请输入管理密码。\n\n"
            "> 提示：扫描服务默认为**关闭**状态，登录后请在「服务开关」中手动开启。"
        )
        pw_input = gr.Textbox(type="password", label="管理密码", elem_id="admin-pw-input")
        login_msg = gr.Markdown(visible=True)
        login_btn = gr.Button("登录", variant="primary")

    # ---------- 管理面板 ----------
    with gr.Column(visible=False) as admin_col:
        with gr.Row():
            # ---- 左侧菜单 ----
            with gr.Column(scale=1, min_width=220, elem_classes="router-sidebar"):
                gr.HTML('<div class="router-sidebar-title">📋 管理菜单</div>')
                menu_btns = [
                    gr.Button(label, elem_classes="router-menu-btn")
                    for label in SECTION_LABELS
                ]
                # 退出登录按钮（固定在侧边栏底部）
                gr.HTML('<div style="flex:1"></div>')
                logout_btn = gr.Button("退出登录", elem_classes="router-menu-btn", elem_id="btn-logout")

            # ---- 右侧内容 ----
            with gr.Column(scale=4, elem_classes="router-content"):
                # 服务开关
                with gr.Column(visible=True) as sec_switch:
                    gr.HTML('<div class="router-section-title">服务开关</div>')
                    switch_summary = gr.Markdown()
                    gr.Markdown(
                        "<div class=\"router-hint\">软门控：开关即时生效，不重启进程。关闭总开关时，"
                        "用户网页与 MCP 均拒绝解析请求并提示；子开关可单独关闭某一入口。</div>",
                    )
                    with gr.Column(elem_classes="router-card"):
                        sw_master = gr.Checkbox(
                            label="🔌 总扫描服务开关（关闭 = 全部入口不可用）"
                        )
                        with gr.Row():
                            sw_web = gr.Checkbox(label="🌐 网页服务子开关（:7860）")
                            sw_mcp = gr.Checkbox(label="🤖 MCP 服务子开关（/gradio_api/mcp/）")
                        sw_save_btn = gr.Button("保存开关状态", variant="primary")
                        sw_save_msg = gr.Markdown()

                # 运行监控
                with gr.Column(visible=False) as sec_monitor:
                    gr.HTML('<div class="router-section-title">运行监控</div>')
                    monitor_out = gr.Markdown("载入中…")
                    with gr.Row():
                        monitor_btn = gr.Button("手动刷新")
                        reset_btn = gr.Button("清零统计", variant="secondary")

                # 默认设置
                with gr.Column(visible=False) as sec_defaults:
                    gr.HTML('<div class="router-section-title">默认设置</div>')
                    gr.Markdown(
                        "<div class=\"router-hint\">控制用户网页各选项的默认勾选状态（用户仍可临时更改），"
                        "以及后端服务连接参数。</div>",
                    )
                    with gr.Column(elem_classes="router-card"):
                        cfg_api_url = gr.Textbox(label="后端 API 地址（/layout-parsing）")
                        cfg_timeout = gr.Number(label="请求超时（秒）", minimum=10, maximum=3600)
                        cfg_max_parallel = gr.Number(
                            label="最大并发推理数",
                            minimum=1, maximum=8, precision=0, value=1,
                            info="同时处理的最大请求数（1 = 串行）。增大需更大显存，否则可能 OOM；修改后需重启网页服务生效。",
                        )
                        gr.Markdown("**用户网页默认勾选**")
                        with gr.Row():
                            d_orientation = gr.Checkbox(label="文档方向分类")
                            d_unwarping = gr.Checkbox(label="文本图像矫正")
                            d_seal = gr.Checkbox(label="印章识别")
                            d_chart = gr.Checkbox(label="图表解析")
                        with gr.Row():
                            d_layout_mode = gr.Checkbox(
                                label="版面分析", info="自动检测分栏、表格、标题、图像区域")
                            d_merge_blocks = gr.Checkbox(
                                label="跨栏分栏合并", info="将跨栏/交错排列的文本合并为连续段落")
                            d_ocr_image_block = gr.Checkbox(
                                label="图像块内 OCR", info="对图片内嵌文字再做一次 OCR")
                            d_format_block = gr.Checkbox(
                                label="块内容格式化", info="将表格/公式等结构化内容渲染为 Markdown 格式")
                        with gr.Row():
                            d_pdf_per_page = gr.Checkbox(
                                label="PDF 每页输出单独文件",
                                info="多页 PDF 按页拆分为独立结果文件")
                            d_export_chart = gr.Checkbox(
                                label="导出图表区域为图片",
                                info="将识别出的图表区域单独导出为图片文件")
                        with gr.Row():
                            d_max_pixels = gr.Slider(0, 4000000, step=100000,
                                                     label="单图最大像素默认值（0 = 不限制）",
                                                     info="当前 llama-cpp-server 后端不支持此参数（仅 vllm-server 生效）")
                        gr.Markdown("**VLM 推理（点击圆点切换 VL 模型精度）**")
                        with gr.Row():
                            vl_precision = gr.Radio(
                                choices=VL_PRECISION_CHOICES,
                                value="q5_k_m",
                                label="选择精度",
                            )
                            vl_switch_msg = gr.Markdown(VL_PRECISION_HINT, elem_classes="router-hint")
                        gr.Markdown("**缓存文件设置（后端临时结果 outputs/ 目录）**")
                        with gr.Row():
                            d_cache_days = gr.Number(
                                value=3, minimum=1, maximum=90, precision=0,
                                label="结果缓存保留天数（到期自动清理）",
                                info="每次新任务生成时，自动删除超过该天数的旧结果目录。",
                            )
                            cache_clear_btn = gr.Button("立即清理缓存", variant="secondary")
                        cache_msg = gr.Markdown()
                        gr.Markdown("**版面后处理默认值（高级）**")
                        with gr.Row():
                            d_layout_nms = gr.Dropdown(
                                choices=["留空", "开启", "关闭"],
                                value="留空", label="版面框 NMS",
                                info="对重叠的版面检测框做非极大值抑制")
                            d_layout_shape = gr.Dropdown(
                                choices=["留空", "rect", "quad", "poly", "auto"],
                                value="留空", label="版面框形状模式（shape_mode）")
                        with gr.Row():
                            d_layout_unclip = gr.Textbox(
                                label="版面框扩张比例（unclip_ratio）",
                                placeholder='留空或 2.0 / {"bbox": [2.0, 2.0]}',
                                info="数值或 JSON 对象；留空使用后端默认")
                            d_layout_merge = gr.Textbox(
                                label="版面框合并模式（merge_bboxes_mode）",
                                placeholder='留空或 union / large / small / {"box": "union"}',
                                info="字符串或 JSON 对象；留空使用后端默认")
                        with gr.Row():
                            d_vlm_extra = gr.Textbox(
                                label="VLM 额外采样参数（vlm_extra_args）",
                                placeholder='留空或 {"temperature": 0.7}',
                                info="JSON 对象；留空使用后端默认")
                            d_md_ignore = gr.Textbox(
                                label="Markdown 忽略标签（ignore_labels）",
                                placeholder='留空或 ["number", "footnote"]',
                                info="JSON 数组；留空使用后端默认")
                        save_btn = gr.Button("保存设置", variant="primary")
                        save_msg = gr.Markdown()

                # 服务状态
                with gr.Column(visible=False) as sec_status:
                    gr.HTML('<div class="router-section-title">服务状态</div>')
                    gr.Markdown("<div class=\"router-hint\">查看后端 API 服务连通性与 GPU 占用。</div>")
                    status_btn = gr.Button("手动刷新")
                    status_out = gr.Markdown("自动刷新中…（每 5 秒）")

                # 日志查看
                with gr.Column(visible=False) as sec_logs:
                    gr.HTML('<div class="router-section-title">日志查看</div>')
                    with gr.Row():
                        log_which = gr.Dropdown(
                            ["API 服务", "用户网页", "管理后台", "Llama 服务"],
                            value="API 服务", label="选择日志"
                        )
                        log_lines = gr.Slider(20, 500, value=100, step=20, label="显示行数")
                    log_btn = gr.Button("手动刷新")
                    log_out = gr.Code(language="shell", label="日志尾部（每 5 秒自动刷新）")

                # 安全设置
                with gr.Column(visible=False) as sec_security:
                    gr.HTML('<div class="router-section-title">安全设置</div>')
                    gr.Markdown("<div class=\"router-hint\">修改管理后台登录密码（至少 6 位）。</div>")
                    with gr.Column(elem_classes="router-card"):
                        old_pw = gr.Textbox(type="password", label="当前密码")
                        new_pw1 = gr.Textbox(type="password", label="新密码")
                        new_pw2 = gr.Textbox(type="password", label="确认新密码")
                        pw_btn = gr.Button("修改密码", variant="primary")
                        pw_msg = gr.Markdown()

    # ---------- 事件 ----------
    section_cols = [sec_switch, sec_monitor, sec_defaults, sec_status, sec_logs, sec_security]

    LOGIN_OUTPUTS = [
        login_col, admin_col,
    ] + section_cols + menu_btns + [
        sw_master, sw_web, sw_mcp, switch_summary,
        cfg_api_url, cfg_timeout, cfg_max_parallel,
        d_orientation, d_unwarping, d_seal, d_chart,
        d_layout_mode, d_merge_blocks, d_ocr_image_block, d_format_block,
        d_pdf_per_page, d_export_chart,
        d_max_pixels, d_cache_days, vl_precision,
        d_layout_nms, d_layout_unclip, d_layout_merge,
        d_layout_shape, d_vlm_extra, d_md_ignore,
        login_msg,
        login_state,
    ]
    LOGOUT_OUTPUTS = LOGIN_OUTPUTS + [pw_input]

    # 页面加载：恢复登录状态。
    # 注意：本应用所有事件均设为 api_visibility="private"，不暴露为可匿名调用的
    # HTTP API（登录仅是 UI 层，公开 endpoint 会被局域网内任何人直接调用）。
    app.load(
        _restore_login,
        inputs=[login_state],
        outputs=LOGIN_OUTPUTS,
        api_visibility="private",
    )

    login_btn.click(do_login, inputs=pw_input, outputs=LOGIN_OUTPUTS,
                    api_visibility="private")
    pw_input.submit(do_login, inputs=pw_input, outputs=LOGIN_OUTPUTS,
                    api_visibility="private")
    logout_btn.click(do_logout, outputs=LOGOUT_OUTPUTS,
                     api_visibility="private")

    # 菜单切换
    for idx, btn in enumerate(menu_btns):
        btn.click(
            fn=lambda i=idx: _section_updates(i),
            outputs=section_cols + menu_btns,
            api_visibility="private",
        )

    sw_save_btn.click(
        save_switches,
        inputs=[sw_master, sw_web, sw_mcp, login_state],
        outputs=[sw_save_msg, switch_summary],
        api_visibility="private",
    )

    monitor_btn.click(refresh_monitor, inputs=[login_state], outputs=monitor_out,
                      api_visibility="private")
    reset_btn.click(reset_stats, inputs=[login_state], outputs=monitor_out,
                    api_visibility="private")
    # 每 5 秒自动刷新监控（登录前隐藏面板，刷新开销可忽略）
    timer = gr.Timer(5)
    timer.tick(refresh_monitor, inputs=[login_state], outputs=monitor_out,
               api_visibility="private")

    save_btn.click(
        save_defaults,
        inputs=[cfg_api_url, cfg_timeout, cfg_max_parallel,
                d_orientation, d_unwarping, d_seal, d_chart,
                d_layout_mode, d_merge_blocks, d_ocr_image_block, d_format_block,
                d_pdf_per_page, d_export_chart,
                d_max_pixels, d_cache_days, vl_precision,
                d_layout_nms, d_layout_unclip, d_layout_merge,
                d_layout_shape, d_vlm_extra, d_md_ignore, login_state],
        outputs=save_msg,
        api_visibility="private",
    )
    cache_clear_btn.click(clear_cache_now, inputs=[login_state], outputs=cache_msg,
                          api_visibility="private")
    vl_precision.change(
        switch_vl_model,
        inputs=[vl_precision, login_state],
        outputs=[vl_switch_msg, vl_precision],
        api_visibility="private",
    )

    status_btn.click(check_status, inputs=[login_state], outputs=status_out,
                     api_visibility="private")
    status_timer = gr.Timer(5)
    status_timer.tick(check_status, inputs=[login_state], outputs=status_out,
                      api_visibility="private")

    log_btn.click(read_log, inputs=[log_which, log_lines, login_state], outputs=log_out,
                  api_visibility="private")
    log_timer = gr.Timer(5)
    log_timer.tick(read_log, inputs=[log_which, log_lines, login_state], outputs=log_out,
                   api_visibility="private")
    pw_btn.click(change_password, inputs=[old_pw, new_pw1, new_pw2, login_state], outputs=pw_msg,
                 api_visibility="private")

if __name__ == "__main__":
    # 注入 JS：让浏览器把管理密码当作真正的密码来处理（触发密码管理器保存）
    ADMIN_JS = """<script>
(function() {
  var iv = setInterval(function() {
    var pw = document.getElementById('admin-pw-input');
    if (!pw) return;
    // 找到实际的 <input> 元素（Gradio 包装在 label 内）
    var input = pw.querySelector('input[type="password"]');
    if (!input) return;
    // 设置浏览器密码管理器所需的属性
    input.setAttribute('autocomplete', 'current-password');
    input.setAttribute('name', 'password');
    // 在输入框外层包裹 form，让浏览器识别为登录表单（不隐藏）
    var container = pw.closest('.gradio-textbox') || pw.parentNode;
    if (container && !container.closest('form')) {
      var form = document.createElement('form');
      container.parentNode.insertBefore(form, container);
      form.appendChild(container);
      form.addEventListener('submit', function(e) { e.preventDefault(); });
    }
    clearInterval(iv);
  }, 200);
  setTimeout(function() { clearInterval(iv); }, 10000);
})();
</script>"""
    app.queue(max_size=8)
    _log(f"管理后台启动：{os.environ.get('ADMIN_SERVER_NAME', '0.0.0.0')}"
         f":{os.environ.get('ADMIN_SERVER_PORT', '7861')}")
    app.launch(
        server_name=os.environ.get("ADMIN_SERVER_NAME", "0.0.0.0"),
        server_port=int(os.environ.get("ADMIN_SERVER_PORT", "7861")),
        show_error=True,
        css=CUSTOM_CSS,
        head=ADMIN_JS,
    )
