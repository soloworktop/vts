#!/usr/bin/env bash
# VTS 一键 CLI：建 venv → 装依赖 → 调用 CLI（参数原样转发）。
#
# 用法:
#   bash scripts/run.sh <视频链接> [CLI 参数...]
#   bash scripts/run.sh "https://www.youtube.com/watch?v=..." --summary-template 学术笔记
#   bash scripts/run.sh <视频链接> --whisper-api --asr-key sk-xxx   # 无自带字幕时转写
#
# 字幕优先：视频自带字幕时零 API 成本直接出笔记，无需配置任何 Key。
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

PYTHON="${PYTHON:-python3}"
VENV_DIR=".venv"

usage() {
    cat <<'EOF'
Usage:
  bash scripts/run.sh <视频链接> [CLI 参数...]

示例:
  bash scripts/run.sh "https://www.youtube.com/watch?v=jNQXAC9IVRw"
  bash scripts/run.sh <视频链接> --summary-template 学术笔记
  bash scripts/run.sh <视频链接> --whisper-api --asr-key sk-xxx
  bash scripts/run.sh --help          # 查看完整 CLI 参数

常用参数（完整清单见 `python -m video_to_summary.main --help`）:
  --output-dir DIR          输出目录（默认 output）
  --summary-template NAME   总结模板（默认「通用」）
  --subtitle-preference     字幕策略 auto|manual_only|off（默认 auto）
  --cookies FILE            cookies.txt 路径（需登录的内容）
  --whisper-api             无自带字幕时用 OpenAI 兼容 Whisper API 转写
  --summary-key / --summary-base-url / --summary-model   BYOK 的推理接入信息（总结/润色）
  --asr-key / --asr-base-url / --asr-model   转写接入信息（视频无字幕时）
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    usage
    exit 0
fi

if [ $# -eq 0 ]; then
    echo "[run] 缺少视频链接参数" >&2
    usage >&2
    exit 2
fi

ensure_venv() {
    if [ ! -d "$VENV_DIR" ]; then
        echo "[run] create venv"
        "$PYTHON" -m venv "$VENV_DIR"
    fi
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    echo "[run] venv activated"
}

ensure_deps() {
    echo "[run] ensure dependencies"
    python -m pip install -e . -q --no-cache-dir
}

# .env 承载 BYOK 的默认接入信息（可选）：不存在也能跑——字幕优先路径无需任何 Key
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    echo "[run] .env not found, copy .env.example -> .env（按需填入自己的 Key）"
    cp .env.example .env
fi

ensure_venv
ensure_deps

echo "[run] $*"
exec python -m video_to_summary.main "$@"
