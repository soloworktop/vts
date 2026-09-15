#!/usr/bin/env bash
set -euo pipefail

# E2E 测试一键运行：确保依赖 → 跑 e2e（真实 uvicorn + 进程内 fakes，浏览器层用 chromium）
# 用法：
#   bash scripts/e2e.sh            # 跑全部离线 e2e（API + 浏览器）
#   bash scripts/e2e.sh api        # 只跑 API 层（无需浏览器）
#   bash scripts/e2e.sh live       # 真实边界 E2E：真实下载/转写/LLM，配置读 .env
#                                  # （需 .env 里配好 Key 与 VTS_LIVE_TEST_URL；CI 不跑本层）
#   SKIP_INSTALL=1 bash scripts/e2e.sh   # 跳过依赖安装

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

VENV_DIR=".venv"
MODE="${1:-all}"

ensure_deps() {
    if [ ! -d "$VENV_DIR" ]; then
        echo "[e2e] create venv"
        python3 -m venv "$VENV_DIR"
    fi
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    if [ "${SKIP_INSTALL:-0}" != "1" ]; then
        echo "[e2e] ensure dependencies"
        python -m pip install -e ".[dev,e2e]" -q --no-cache-dir
        # chromium 二进制已存在时跳过下载
        python - <<'PY' 2>/dev/null || python -m playwright install chromium
from playwright.sync_api import sync_playwright
pw = sync_playwright().start()
try:
    pw.chromium.launch(headless=True).close()
    print("[e2e] chromium already installed")
finally:
    pw.stop()
PY
    fi
}

ensure_deps

# UI e2e 面向新 React 前端：必须先构建产物到 web/static/（缺失时页面是「未构建」占位）。
# 前端已构建且 web-src 未变更时可直接跳过构建（SKIP_FRONTEND_BUILD=1）。
# live 模式纯 API/CLI 子进程，不需要前端产物。
if [ "$MODE" != "live" ]; then
    if [ "${SKIP_FRONTEND_BUILD:-0}" != "1" ] && command -v pnpm >/dev/null 2>&1; then
        echo "[e2e] build frontend (web-src -> web/static)"
        (cd web-src && pnpm install --silent && pnpm build)
    elif [ ! -f "src/video_to_summary/web/static/index.html" ]; then
        echo "[e2e] 前端构建产物缺失且无 pnpm：请先 cd web-src && pnpm install && pnpm build" >&2
        exit 1
    fi
fi

case "$MODE" in
    api)
        python -m pytest tests/e2e/test_api_job_lifecycle.py -v
        ;;
    live)
        # 真实边界（零 fakes）：pytest 侧还有 VTS_LIVE_E2E 门控，这里显式打开
        # （用户环境已设值时不覆盖）。会用掉 .env 里 Key 的真实调用量。
        VTS_LIVE_E2E="${VTS_LIVE_E2E:-1}" python -m pytest -m live -v
        ;;
    all)
        python -m pytest -m e2e -v
        ;;
    *)
        echo "Usage: bash scripts/e2e.sh [api|all|live]" >&2
        exit 1
        ;;
esac
