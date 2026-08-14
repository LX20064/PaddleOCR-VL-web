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
if command -v python >/dev/null 2>&1; then
    python "$PROJECT_DIR/model_manager.py" ensure-vl --precision "$PRECISION_NORM" || {
        echo "错误：VL 模型准备失败，请查看上方输出后重试。"
        exit 1
    }
else
    echo "错误：未找到 python，无法自动准备 VL 模型。"
    exit 1
fi

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

# CUDA 运行时库（持久化安装，避免 /tmp 被清理后符号链接失效）
export CUDA_HOME=/home/lx/.local/share/cuda-12.4
export LD_LIBRARY_PATH="/home/lx/.local/share/cuda-runtime/lib:$CUDA_HOME/lib64:${LD_LIBRARY_PATH:-}"

echo "启动 llama-server（$PRECISION_LABEL，端口 $PORT）..."
nohup "$LLAMA_BIN" \
    -m "$MODEL" \
    --mmproj "$MMPROJ" \
    --host 127.0.0.1 \
    --port "$PORT" \
    --temp 0 \
    --ctx-size 16384 \
    -ngl 99 \
    > "$PROJECT_DIR/logs/llama.log" 2>&1 &
echo $! > "$PROJECT_DIR/llama.pid"
echo "llama-server 已启动（PID $(cat "$PROJECT_DIR/llama.pid")），日志：logs/llama.log"
