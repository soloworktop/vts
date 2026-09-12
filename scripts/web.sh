#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

PYTHON="${PYTHON:-python3}"
VENV_DIR=".venv"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8080}"

usage() {
    cat <<EOF
Usage:
  bash scripts/web.sh [--port PORT]

Options:
  --port PORT       监听端口，默认 8080；也可通过 PORT 环境变量配置
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --port)
            PORT="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "[web] unknown arg: $1" >&2
            usage
            exit 1
            ;;
    esac
done

ensure_venv() {
    if [ ! -d "$VENV_DIR" ]; then
        echo "[web] create venv"
        "$PYTHON" -m venv "$VENV_DIR"
    fi
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    echo "[web] venv activated"
}

ensure_deps() {
    echo "[web] ensure dependencies"
    # fastapi/uvicorn/python-multipart/aiofiles 已是 pyproject 的基础依赖
    python -m pip install -e . -q --no-cache-dir
}

# .env 承载 BYOK 的默认接入信息（可选）：未配置也能启动——
# 「字幕优先」路径无需任何 Key，缺 Key 时任务仍产出转写文本
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    echo "[web] .env not found, copy .env.example -> .env（按需填入自己的 Key）"
    cp .env.example .env
fi

ensure_venv
ensure_deps

# 前端构建产物检测：源码检出默认不带构建产物（不入库）。
# 缺失时服务仍可启动（首页返回「前端未构建」提示页），但这里提前给出可行动指引。
FRONTEND_INDEX="src/video_to_summary/web/static/index.html"
if [ ! -f "$FRONTEND_INDEX" ]; then
    echo "[web] ⚠️  未检测到前端构建产物（$FRONTEND_INDEX）"
    echo "[web]     生产态：cd web-src && pnpm install && pnpm build"
    echo "[web]     开发态：cd web-src && pnpm dev（Vite dev server 代理 /api 到本服务）"
    echo "[web]     首页将显示「前端未构建」提示页，API 不受影响。"
fi

resolve_port() {
    local try="$1"
    local max_tries="${2:-20}"
    local i="$try"
    local count=0
    while [ "$count" -lt "$max_tries" ]; do
        if command -v lsof >/dev/null 2>&1; then
            if ! lsof -nP -iTCP:"$i" -sTCP:LISTEN >/dev/null 2>&1; then
                echo "$i"
                return 0
            fi
        else
            echo "$i"
            return 0
        fi
        i=$((i+1))
        count=$((count+1))
    done
    echo "$try"
    return 0
}

PORT="$(resolve_port "$PORT" 20)"
echo "[web] start server at http://$HOST:$PORT"
exec uvicorn video_to_summary.web.app:app --host "$HOST" --port "$PORT"
