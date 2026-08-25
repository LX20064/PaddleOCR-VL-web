#!/usr/bin/env bash
# ============================================================
# 启动 llama.cpp llama-server（PaddleOCR-VL-1.6 GGUF，CUDA GPU）
# 用法：bash start_llama_server.sh [精度]
#   精度可选：fp16 | q8_0 | q5_k_m | q4_k_m（默认 q5_k_m）
#   停止：随 stop_all.sh 一起停止
# 端口：8081（供 paddlex --serve 的 VLRecognition llama-cpp-server 后端调用）
# 前置：已用 CUDA 编译 llama.cpp（tools/llama.cpp/build-cuda），且 GGUF 已量化
# ============================================================
set -e

PRECISION="${1:-q5_k_m}"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LLAMA_BIN="$PROJECT_DIR/tools/llama.cpp/build-cuda/bin/llama-server"
MODEL_DIR="$PROJECT_DIR/tools/gguf/1.6"
MMPROJ="$MODEL_DIR/PaddleOCR-VL-1.6-GGUF-mmproj.gguf"
PORT=8081

# 精度 → 模型文件名映射
case "$PRECISION" in
    fp16|FP16)
        MODEL_FILE="PaddleOCR-VL-1.6-GGUF.gguf"
        PRECISION_LABEL="FP16"
        PRECISION_NORM="fp16"
        ;;
    q8_0|Q8_0|q8|Q8)
        MODEL_FILE="PaddleOCR-VL-1.6-GGUF-Q8_0.gguf"
        PRECISION_LABEL="Q8_0"
        PRECISION_NORM="q8_0"
        ;;
    q5_k_m|Q5_K_M|q5km|Q5KM)
        MODEL_FILE="PaddleOCR-VL-1.6-GGUF-Q5_K_M.gguf"
        PRECISION_LABEL="Q5_K_M"
        PRECISION_NORM="q5_k_m"
        ;;
    q4_k_m|Q4_K_M|q4km|Q4KM)
        MODEL_FILE="PaddleOCR-VL-1.6-GGUF-Q4_K_M.gguf"
        PRECISION_LABEL="Q4_K_M"
        PRECISION_NORM="q4_k_m"
        ;;
    *)
        echo "错误：不支持的精度 '$PRECISION'，可选：fp16 | q8_0 | q5_k_m | q4_k_m"
        exit 1
        ;;
esac

MODEL="$MODEL_DIR/$MODEL_FILE"

mkdir -p "$PROJECT_DIR/logs"

# 按需下载官方模型并转换 / 量化到所选精度（已存在则秒级跳过，不删除其它精度文件）
# Python 探测顺序与 start_all.sh 保持一致：显式 VENV_PATH > PATH 上的 python
# > 默认 venv > 系统 python3（admin 热切换等场景下 venv 可能未激活，python 不在 PATH）
PY=""
if [ -n "${VENV_PATH:-}" ] && [ -x "$VENV_PATH/bin/python" ]; then
    PY="$VENV_PATH/bin/python"
elif command -v python >/dev/null 2>&1; then
    PY="python"
elif [ -x "$HOME/.venv_paddleocr/bin/python" ]; then
    PY="$HOME/.venv_paddleocr/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY="python3"
else
    echo "错误：未找到可用的 Python 解释器（python / python3 / ~/.venv_paddleocr），无法自动准备 VL 模型。"
    exit 1
fi
"$PY" "$PROJECT_DIR/model_manager.py" ensure-vl --precision "$PRECISION_NORM" || {
    echo "错误：VL 模型准备失败，请查看上方输出后重试。"
    exit 1
}

if [ ! -x "$LLAMA_BIN" ]; then
    echo "错误：未找到 CUDA 版 llama-server（$LLAMA_BIN），请先完成 CUDA 编译。"
    exit 1
fi
if [ ! -f "$MODEL" ] || [ ! -f "$MMPROJ" ]; then
    echo "错误：GGUF 文件缺失（$MODEL / $MMPROJ）"
    exit 1
fi

# 已在运行则跳过
if [ -f "$PROJECT_DIR/llama.pid" ] && kill -0 "$(cat "$PROJECT_DIR/llama.pid")" 2>/dev/null; then
    echo "llama-server 已在运行（PID $(cat "$PROJECT_DIR/llama.pid")），跳过"
    exit 0
fi

# CUDA 运行时库路径：
#   - Docker 内：/usr/local/cuda（nvidia/cuda 镜像标准路径，ldconfig 已注册）
#   - 宿主机：可能装在自定义位置（如 ~/.local/share/cuda-12.4）
# 优先使用环境变量 CUDA_HOME，其次自动探测标准位置，最后回退宿主兼容目录。
if [ -z "${CUDA_HOME:-}" ]; then
    if [ -d /usr/local/cuda/lib64 ]; then
        CUDA_HOME=/usr/local/cuda
    elif [ -d "$HOME/.local/share/cuda-12.4/lib64" ]; then
        CUDA_HOME="$HOME/.local/share/cuda-12.4"
    fi
fi
if [ -n "${CUDA_HOME:-}" ] && [ -d "$CUDA_HOME/lib64" ]; then
    export CUDA_HOME
    _cuda_lib="$CUDA_HOME/lib64"
    # 宿主兼容：部分 CUDA 运行时库放在独立目录
    if [ -d "$HOME/.local/share/cuda-runtime/lib" ]; then
        _cuda_lib="$HOME/.local/share/cuda-runtime/lib:$_cuda_lib"
    fi
    export LD_LIBRARY_PATH="$_cuda_lib:${LD_LIBRARY_PATH:-}"
fi
# 回退：pip 安装的 nvidia CUDA 运行时库（nvidia-cuda-runtime-cu12 / nvidia-cublas-cu12）
# 适用于系统未安装 CUDA Toolkit、仅靠 pip 提供 libcudart / libcublas 的宿主机部署。
if [ -z "${LD_LIBRARY_PATH:-}" ] || ! ldconfig -p 2>/dev/null | grep -q libcudart.so.12; then
    _pycmd=$(command -v python3 2>/dev/null || command -v python 2>/dev/null || echo /nonexistent)
    # 候选 Python 前缀：PATH 上的解释器 + 常见 conda/venv 位置
    _cand_py="$HOME/miniconda3/bin/python3 $HOME/miniconda3/bin/python $_pycmd $HOME/.venv_paddleocr/bin/python"
    for _py in $_cand_py; do
        [ -x "$_py" ] || continue
        for _nv in "$(dirname "$(dirname "$_py")")/lib"/python3*/site-packages/nvidia/*/lib; do
            [ -d "$_nv" ] && _cuda_lib="${_cuda_lib:+$_cuda_lib:}$_nv"
        done
    done
    if [ -n "${_cuda_lib:-}" ]; then
        export LD_LIBRARY_PATH="$_cuda_lib:${LD_LIBRARY_PATH:-}"
    fi
fi

echo "启动 llama-server（$PRECISION_LABEL，端口 $PORT）..."
# llama.cpp 动态库（libllama-server-impl.so 等）与可执行文件同目录
export LD_LIBRARY_PATH="$(dirname "$LLAMA_BIN"):${LD_LIBRARY_PATH:-}"
nohup "$LLAMA_BIN" \
    -m "$MODEL" \
    --mmproj "$MMPROJ" \
    --host 127.0.0.1 \
    --port "$PORT" \
    --temp 0 \
    --ctx-size 32768 \
    -ngl 99 \
    > "$PROJECT_DIR/logs/llama.log" 2>&1 &
echo $! > "$PROJECT_DIR/llama.pid"
echo "llama-server 已启动（PID $(cat "$PROJECT_DIR/llama.pid")），日志：logs/llama.log"
