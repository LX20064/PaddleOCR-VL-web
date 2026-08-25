#!/usr/bin/env bash
# 停止 API 服务、用户网页和管理后台
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
for f in api.pid web.pid admin.pid llama.pid; do
    if [ -f "$PROJECT_DIR/$f" ]; then
        PID=$(cat "$PROJECT_DIR/$f")
        if kill -0 "$PID" 2>/dev/null; then
            # 先结束整个进程组，避免子进程继续占用端口
            kill -- -"$PID" 2>/dev/null || true
            kill "$PID" 2>/dev/null || true
            echo "已停止 $f（PID $PID 及其子进程）"
        fi
        rm -f "$PROJECT_DIR/$f"
    fi
done
