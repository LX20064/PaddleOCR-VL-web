#!/usr/bin/env bash
# ============================================================
# Docker 容器入口：准备模型 -> 启动四服务 -> 前台跟随日志
# 环境变量：
#   VL_PRECISION   VL 模型精度（fp16|q8_0|q5_k_m|q4_k_m，默认 q5_k_m）
#   PREWARM        是否提前准备 VL 模型（1=是，默认 1）
# ============================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

echo "==> PaddleOCR-VL 容器启动"

# GPU 就绪性检查（需以 --gpus all 运行）
if ! command -v nvidia-smi >/dev/null 2>&1 || ! nvidia-smi -L >/dev/null 2>&1; then
    echo "错误：未检测到可用的 NVIDIA GPU。"
    echo "请使用 --gpus all 运行容器（或 docker compose，已内置 GPU 配置）。"
    exit 1
fi
nvidia-smi --query-gpu=name,compute_cap,memory.total --format=csv,noheader

# 提前准备 VL 模型（首次会下载官方模型 + 转 GGUF + 量化，耗时较长；已就绪则秒级跳过）
if [ "${PREWARM:-1}" = "1" ]; then
    PRECISION="${VL_PRECISION:-q5_k_m}"
    echo "==> 准备 VL 模型（精度 $PRECISION）..."
    python model_manager.py ensure-vl --precision "$PRECISION" || {
        echo "[警告] VL 模型准备失败（详见上方日志），仍继续尝试启动服务。"
    }
fi

# 启动四服务：llama-server(8081) + API(8080) + 用户网页(7860) + 管理后台(7861)
echo "==> 启动服务..."
bash start_all.sh

# 前台保持容器存活，实时输出服务日志
mkdir -p logs
touch logs/llama.log logs/api.log logs/web.log logs/admin.log
echo "==> 全部服务已启动，进入日志跟随模式（Ctrl-C 退出）"
exec tail -n 50 -F logs/llama.log logs/api.log logs/web.log logs/admin.log
