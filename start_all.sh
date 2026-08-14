#!/usr/bin/env bash
# ============================================================
# 一键启动 PaddleOCR-VL API 服务 + 用户网页 + 管理后台
# 用法：bash start_all.sh     （停止：bash stop_all.sh）
# 前置：已按 README 完成环境安装，且本目录下已生成 PaddleOCR-VL.yaml
# ============================================================
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PATH="${VENV_PATH:-$HOME/.venv_paddleocr}"
PIPELINE_CONFIG="$PROJECT_DIR/PaddleOCR-VL.yaml"

source "$VENV_PATH/bin/activate"
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
# 从 web_config.json 读取当前保存的 VL 精度（默认 q5_k_m），确保重启后精度与后台设置一致
VL_PRECISION=$(python -c "
import json, sys
try:
    with open('web_config.json', 'r', encoding='utf-8') as f:
        cfg = json.load(f)
    print(cfg.get('defaults', {}).get('vl_precision', 'q5_k_m'))
except Exception:
    print('q5_k_m')
" 2>/dev/null || echo "q5_k_m")
echo "检查 llama-server（GGUF ${VL_PRECISION} 后端）..."
bash "$PROJECT_DIR/start_llama_server.sh" "$VL_PRECISION"

# ---------- 1. API 服务（8080） ----------
start_if_not_running api.pid "API 服务（0.0.0.0:8080）" \
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
