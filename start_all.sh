#!/usr/bin/env bash
# ============================================================
# 一键启动 PaddleOCR-VL API 服务 + 用户网页 + 管理后台
# 用法：bash start_all.sh     （停止：bash stop_all.sh）
# 前置：已按 README 完成环境安装，且本目录下已生成 PaddleOCR-VL.yaml
# ============================================================
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PIPELINE_CONFIG="$PROJECT_DIR/PaddleOCR-VL.yaml"

# Python 环境：Docker 内直接用系统 Python（无 venv）；宿主机按需激活 venv。
# 可用 VENV_PATH 显式指定，未指定则回退 ~/.venv_paddleocr（存在才激活）。
if [ -n "${VENV_PATH:-}" ] && [ -f "$VENV_PATH/bin/activate" ]; then
    source "$VENV_PATH/bin/activate"
elif [ -f "$HOME/.venv_paddleocr/bin/activate" ]; then
    source "$HOME/.venv_paddleocr/bin/activate"
fi
mkdir -p "$PROJECT_DIR/logs"

start_if_not_running() {
    # $1=pid文件 $2=描述 $3=启动命令...
    local pidfile="$PROJECT_DIR/$1"; shift
    local desc="$1"; shift
    if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        echo "$desc 已在运行（PID $(cat "$pidfile")），跳过"
    else
        echo "启动 $desc ..."
        nohup "$@" > "$PROJECT_DIR/logs/$(basename "$pidfile" .pid).log" 2>&1 &
        echo $! > "$pidfile"
    fi
}

# ---------- 0. llama.cpp llama-server（8081，VLM 推理后端） ----------
# 从 web_config.json 读取当前保存的 VL 精度（默认 fp16），确保重启后精度与后台设置一致
VL_PRECISION=$(python -c "
import json, sys
try:
    with open('$PROJECT_DIR/web_config.json', 'r', encoding='utf-8') as f:
        cfg = json.load(f)
    print(cfg.get('defaults', {}).get('vl_precision', 'fp16'))
except Exception:
    print('fp16')
" 2>/dev/null || echo "fp16")
echo "检查 llama-server（GGUF ${VL_PRECISION} 后端）..."
bash "$PROJECT_DIR/start_llama_server.sh" "$VL_PRECISION"

# ---------- 1. API 服务（8080） ----------
# PADDLE_PDX_PDF_RENDER_SCALE=4.0：paddlex 默认 PDF 渲染 scale=2.0（A4 等效
# 144 DPI），小字号 LaTeX 命令（\frac 等）易识别错乱；4.0 等效 288 DPI，
# 与网页「PDF 扫描 DPI」默认值一致，公式/命令识别质量明显更好。
start_if_not_running api.pid "API 服务（0.0.0.0:8080）" \
    env PADDLE_PDX_PDF_RENDER_SCALE=4.0 \
    paddlex --serve --pipeline "$PIPELINE_CONFIG" --device gpu:0 --host 0.0.0.0 --port 8080

# ---------- 2. 等待 API 就绪 ----------
echo "等待 API 服务就绪（首次需加载模型，可能要几分钟）..."
for i in $(seq 1 90); do
    CODE=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/ 2>/dev/null || true)
    if [ -n "$CODE" ] && [ "$CODE" != "000" ]; then
        echo "API 服务已就绪（HTTP $CODE）"
        break
    fi
    if [ "$i" -eq 90 ]; then
        echo "等待超时，请查看 logs/api.log 排查"
        exit 1
    fi
    sleep 5
done

# ---------- 3. 用户网页（7860） ----------
start_if_not_running web.pid "用户网页（0.0.0.0:7860）" \
    python "$PROJECT_DIR/web_ocr.py"

# ---------- 4. 管理后台（7861） ----------
start_if_not_running admin.pid "管理后台（0.0.0.0:7861）" \
    python "$PROJECT_DIR/admin.py"

IP=$(hostname -I | awk '{print $1}')
echo ""
echo "============================================================"
echo " 部署完成！"
echo "   用户网页：  http://$IP:7860"
echo "   管理后台：  http://$IP:7861  （初始密码 admin123，请尽快修改）"
echo "============================================================"
