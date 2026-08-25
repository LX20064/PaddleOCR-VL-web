#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
model_manager.py —— PaddleOCR-VL 模型自动下载 / 转换 / 按需量化

职责：
  1. 下载官方 PaddleOCR-VL 模型（默认 ModelScope，可回退 HuggingFace）
  2. 用项目内置 llama.cpp 把官方模型转换为 GGUF（主语言模型 + mmproj 投影层）
  3. 按需量化：只生成所选精度的 GGUF，切换精度时不删除其它精度文件，
     也不删除官方模型与 FP16 主模型
  4. 预热产线子模型（版面检测 / 文档预处理等无需量化的模型）

用法：
  python model_manager.py ensure-vl --precision q4_k_m   # 按需准备某个精度的 VL 模型
  python model_manager.py ensure-all --precision q5_k_m  # 首次初始化：VL 模型 + 产线子模型
  python model_manager.py status                          # 查看各精度文件是否就绪
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
LLAMA_CPP_DIR = PROJECT_DIR / "tools" / "llama.cpp"
CONVERT_SCRIPT = LLAMA_CPP_DIR / "convert_hf_to_gguf.py"
QUANTIZE_BIN = LLAMA_CPP_DIR / "build-cuda" / "bin" / "llama-quantize"
LLAMA_SERVER_BIN = LLAMA_CPP_DIR / "build-cuda" / "bin" / "llama-server"

GGUF_DIR = PROJECT_DIR / "tools" / "gguf" / "1.6"
MODELS_DIR = PROJECT_DIR / "tools" / "models"
OFFICIAL_MODEL_DIR = MODELS_DIR / "PaddleOCR-VL-1.6"

PIPELINE_CONFIG = PROJECT_DIR / "PaddleOCR-VL.yaml"

MAIN_GGUF = GGUF_DIR / "PaddleOCR-VL-1.6-GGUF.gguf"
MMPROJ_GGUF = GGUF_DIR / "PaddleOCR-VL-1.6-GGUF-mmproj.gguf"

# 精度 -> 量化参数与文件名（与 start_llama_server.sh 保持一致）
PRECISIONS = {
    "fp16": {"quant_type": None, "file": "PaddleOCR-VL-1.6-GGUF.gguf", "label": "FP16"},
    "q8_0": {"quant_type": "Q8_0", "file": "PaddleOCR-VL-1.6-GGUF-Q8_0.gguf", "label": "Q8_0"},
    "q5_k_m": {"quant_type": "Q5_K_M", "file": "PaddleOCR-VL-1.6-GGUF-Q5_K_M.gguf", "label": "Q5_K_M"},
    "q4_k_m": {"quant_type": "Q4_K_M", "file": "PaddleOCR-VL-1.6-GGUF-Q4_K_M.gguf", "label": "Q4_K_M"},
}

# 官方模型下载源（按顺序尝试，成功即止）
DOWNLOAD_SOURCES = [
    ("modelscope", "PaddlePaddle/PaddleOCR-VL-1.6"),
    ("huggingface", "PaddlePaddle/PaddleOCR-VL-1.6"),
    ("huggingface", "PaddlePaddle/PaddleOCR-VL"),
]

# 转换耗时可能较长（首次下载 + 转换），超时按小时级设置
DOWNLOAD_TIMEOUT = 6 * 3600
CONVERT_TIMEOUT = 6 * 3600
QUANTIZE_TIMEOUT = 2 * 3600


def log(msg: str) -> None:
    print(msg, flush=True)


def _run(cmd, cwd, timeout):
    log(f"执行: {' '.join(str(c) for c in cmd)}")
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
    )


def _run_checked(cmd, cwd, timeout):
    result = _run(cmd, cwd, timeout)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip()[-2000:]
        raise RuntimeError(f"命令执行失败（exit={result.returncode}）\n{tail}")
    return result


def _check_toolchain():
    if not CONVERT_SCRIPT.exists():
        raise RuntimeError(f"缺少转换脚本：{CONVERT_SCRIPT}")
    if not QUANTIZE_BIN.exists() or not os.access(QUANTIZE_BIN, os.X_OK):
        raise RuntimeError(f"缺少 llama-quantize：{QUANTIZE_BIN}")
    if not LLAMA_SERVER_BIN.exists() or not os.access(LLAMA_SERVER_BIN, os.X_OK):
        raise RuntimeError(f"缺少 llama-server：{LLAMA_SERVER_BIN}")


# 转换依赖：（导入名, pip 包名）。protobuf 的导入名是 google.protobuf，
# 而 pip 包名为 protobuf，两者必须分开，否则 __import__("protobuf") 永远失败。
CONVERT_DEPS = [
    ("torch", "torch"),
    ("transformers", "transformers"),
    ("google.protobuf", "protobuf"),
    ("safetensors", "safetensors"),
    ("sentencepiece", "sentencepiece"),
]


def _missing_deps():
    missing = []
    for import_name, _ in CONVERT_DEPS:
        try:
            __import__(import_name)
        except Exception:
            missing.append(import_name)
    return missing


def _install_deps():
    missing = _missing_deps()
    if not missing:
        return
    log(f"缺少转换依赖：{', '.join(missing)}，正在自动安装（仅首次）...")
    pip = [sys.executable, "-m", "pip", "install"]

    if "torch" in missing:
        # 转换仅需 CPU 版 torch，避免下载数 GB 的 CUDA 版本
        _run_checked(
            pip + ["torch", "--index-url", "https://download.pytorch.org/whl/cpu"],
            cwd=str(PROJECT_DIR),
            timeout=DOWNLOAD_TIMEOUT,
        )

    rest = [pkg for import_name, pkg in CONVERT_DEPS
            if import_name in missing and import_name != "torch"]
    if rest:
        _run_checked(
            pip + rest + ["-i", "https://pypi.tuna.tsinghua.edu.cn/simple/"],
            cwd=str(PROJECT_DIR),
            timeout=DOWNLOAD_TIMEOUT,
        )

    still_missing = _missing_deps()
    if still_missing:
        raise RuntimeError(f"依赖安装后仍缺失：{', '.join(still_missing)}")


def _official_model_ready():
    return (OFFICIAL_MODEL_DIR / "config.json").exists()


def _download_official_model():
    if _official_model_ready():
        log(f"官方模型已存在：{OFFICIAL_MODEL_DIR}")
        return str(OFFICIAL_MODEL_DIR)

    OFFICIAL_MODEL_DIR.parent.mkdir(parents=True, exist_ok=True)
    errors = []

    for source, model_id in DOWNLOAD_SOURCES:
        env_id = os.environ.get("PADDLEOCR_VL_MODEL_ID")
        if env_id:
            model_id = env_id
        log(f"尝试下载官方模型：{source} -> {model_id}")
        try:
            if source == "modelscope":
                from modelscope import snapshot_download
                snapshot_download(model_id, local_dir=str(OFFICIAL_MODEL_DIR))
            else:
                from huggingface_hub import snapshot_download
                snapshot_download(model_id, local_dir=str(OFFICIAL_MODEL_DIR))
            if _official_model_ready():
                log(f"官方模型下载完成：{OFFICIAL_MODEL_DIR}")
                return str(OFFICIAL_MODEL_DIR)
            errors.append(f"{source}:{model_id} 下载后未找到 config.json")
        except Exception as e:
            errors.append(f"{source}:{model_id} -> {e}")

    raise RuntimeError("官方模型下载失败，请检查网络后重试。\n" + "\n".join(errors))


def _convert_main_gguf():
    log("转换主语言模型 -> GGUF（f16，供后续量化）...")
    GGUF_DIR.mkdir(parents=True, exist_ok=True)
    _run_checked(
        [
            sys.executable,
            str(CONVERT_SCRIPT),
            str(OFFICIAL_MODEL_DIR),
            "--outfile",
            str(MAIN_GGUF),
            "--outtype",
            "f16",
        ],
        cwd=str(LLAMA_CPP_DIR),
        timeout=CONVERT_TIMEOUT,
    )
    if not MAIN_GGUF.exists():
        raise RuntimeError(f"转换完成但未找到主模型文件：{MAIN_GGUF}")


def _convert_mmproj_gguf():
    log("转换投影层 -> GGUF（mmproj，保持原精度不量化）...")
    tmp_dir = GGUF_DIR / "_mmproj_tmp"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    _run_checked(
        [
            sys.executable,
            str(CONVERT_SCRIPT),
            str(OFFICIAL_MODEL_DIR),
            "--mmproj",
            "--outfile",
            str(tmp_dir),
            "--outtype",
            "f16",
        ],
        cwd=str(LLAMA_CPP_DIR),
        timeout=CONVERT_TIMEOUT,
    )

    produced = sorted(tmp_dir.glob("mmproj-*.gguf"))
    if not produced:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError(f"mmproj 转换完成但未找到输出文件：{tmp_dir}")

    shutil.move(str(produced[0]), str(MMPROJ_GGUF))
    shutil.rmtree(tmp_dir, ignore_errors=True)
    log(f"mmproj 就绪：{MMPROJ_GGUF}")


def _quantize(precision: str):
    info = PRECISIONS[precision]
    target = GGUF_DIR / info["file"]
    if target.exists():
        log(f"量化文件已存在，跳过：{target}")
        return

    log(f"量化主模型 -> {info['label']}（{info['quant_type']}）...")
    _run_checked(
        [str(QUANTIZE_BIN), str(MAIN_GGUF), str(target), info["quant_type"]],
        cwd=str(PROJECT_DIR),
        timeout=QUANTIZE_TIMEOUT,
    )
    if not target.exists():
        raise RuntimeError(f"量化完成但未找到输出文件：{target}")


def ensure_vl_model(precision: str):
    """按需确保某个精度的 VL GGUF 模型存在。已存在时快速跳过。"""
    precision = (precision or "").strip().lower()
    if precision not in PRECISIONS:
        raise ValueError(f"不支持的精度：{precision}，可选 {', '.join(PRECISIONS)}")

    info = PRECISIONS[precision]
    target = GGUF_DIR / info["file"]

    need_convert = (
        (not MAIN_GGUF.exists())
        or (not MMPROJ_GGUF.exists())
        or (precision != "fp16" and not target.exists())
    )
    if not need_convert:
        log(f"VL 模型 {info['label']} 已就绪，无需准备。")
        return str(target)

    _check_toolchain()
    _install_deps()

    if not _official_model_ready():
        _download_official_model()

    if not MAIN_GGUF.exists():
        _convert_main_gguf()

    if not MMPROJ_GGUF.exists():
        _convert_mmproj_gguf()

    if precision != "fp16" and not target.exists():
        _quantize(precision)

    if not target.exists():
        raise RuntimeError(f"模型准备完成但未找到目标文件：{target}")

    log(f"VL 模型 {info['label']} 准备完成：{target}")
    return str(target)


def ensure_pipeline_models():
    """预热无需量化的产线子模型（版面检测 / 文档预处理等）。

    这里仅做尽力而为的下载；服务启动时 paddlex 也会自动下载，失败不影响主流程。
    """
    log("预热产线子模型（PP-DocLayoutV3 / 方向分类 / 矫正等）...")
    try:
        from paddlex import create_pipeline
        create_pipeline(pipeline=str(PIPELINE_CONFIG), device="gpu:0")
        log("产线子模型就绪。")
    except Exception as e:
        log(f"产线子模型预热失败（可忽略，paddlex --serve 首次启动时会自动下载）：{e}")


def ensure_all(precision: str = "q5_k_m"):
    ensure_vl_model(precision)
    ensure_pipeline_models()


def status():
    lines = []
    lines.append(f"官方模型目录：{OFFICIAL_MODEL_DIR}（{'已下载' if _official_model_ready() else '未下载'}）")
    lines.append(f"主模型 FP16：{MAIN_GGUF}（{'✅' if MAIN_GGUF.exists() else '❌'}）")
    lines.append(f"投影层 mmproj：{MMPROJ_GGUF}（{'✅' if MMPROJ_GGUF.exists() else '❌'}）")
    for key, info in PRECISIONS.items():
        if key == "fp16":
            continue
        p = GGUF_DIR / info["file"]
        lines.append(f"量化 {info['label']}：{p}（{'✅' if p.exists() else '❌'}）")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="PaddleOCR-VL 模型自动下载 / 转换 / 量化")
    sub = parser.add_subparsers(dest="command", required=True)

    p_vl = sub.add_parser("ensure-vl", help="按需准备指定精度的 VL GGUF 模型")
    p_vl.add_argument("--precision", required=True,
                      choices=list(PRECISIONS), help="目标精度")

    p_all = sub.add_parser("ensure-all", help="首次初始化：VL 模型 + 产线子模型")
    p_all.add_argument("--precision", default="q5_k_m",
                       choices=list(PRECISIONS), help="默认 VL 精度")

    sub.add_parser("status", help="查看模型就绪状态")

    args = parser.parse_args()

    if args.command == "ensure-vl":
        ensure_vl_model(args.precision)
    elif args.command == "ensure-all":
        ensure_all(args.precision)
    elif args.command == "status":
        print(status())


if __name__ == "__main__":
    main()
