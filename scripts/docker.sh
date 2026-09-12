#!/usr/bin/env bash
# VTS Docker 一键脚本：docker compose 的薄封装（统一 -f 路径，免记 compose 子命令）。
# 用法：bash scripts/docker.sh up|build|down|logs|ps|help
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

COMPOSE=(docker compose -f docker/docker-compose.yml)

usage() {
    cat <<EOF
Usage: bash scripts/docker.sh <cmd>

Commands:
  build   仅构建镜像（不启动）
  up      构建并后台启动（docker compose up -d --build）——推荐入口
  down    停止并移除容器（数据卷保留）
  logs    跟踪服务日志
  ps      查看容器状态
  help    显示本帮助

与 `docker compose -f docker/docker-compose.yml <cmd>` 等价，详见 docker/README.md。
EOF
}

cmd="${1:-help}"
case "$cmd" in
    build) "${COMPOSE[@]}" build ;;
    up)    "${COMPOSE[@]}" up -d --build ;;
    down)  "${COMPOSE[@]}" down ;;
    logs)  "${COMPOSE[@]}" logs -f ;;
    ps)    "${COMPOSE[@]}" ps ;;
    help|-h|--help) usage ;;
    *)
        echo "[docker] unknown command: $cmd" >&2
        usage
        exit 1
        ;;
esac
