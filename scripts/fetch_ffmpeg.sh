#!/usr/bin/env bash
# 下载 macOS 静态 ffmpeg / ffprobe 到指定目录（供桌面 App 随包分发）。
#
# 用法:
#   bash scripts/fetch_ffmpeg.sh [--dir DIR] [--arch arm64|x86_64]
#
# 默认从 evermeet.cx 下载（macOS 静态构建）。也可用环境变量指定其他镜像:
#   FFMPEG_ZIP_URL=... FFPROBE_ZIP_URL=... bash scripts/fetch_ffmpeg.sh
# 常用镜像: https://evermeet.cx/ffmpeg/ （arm64/x86_64）
set -euo pipefail

DIR="dist/assets/bin"
ARCH="$(uname -m)"

usage() {
    cat <<EOF
Usage:
  bash scripts/fetch_ffmpeg.sh [--dir DIR] [--arch arm64|x86_64]

环境变量:
  FFMPEG_ZIP_URL / FFPROBE_ZIP_URL  自定义 zip 下载地址（覆盖默认镜像）
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dir) DIR="$2"; shift 2 ;;
        --arch) ARCH="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "[ffmpeg] unknown arg: $1" >&2; usage; exit 1 ;;
    esac
done

# 分架构下载源：
# - x86_64 → evermeet.cx getrelease（Intel 静态构建，稳定直链）
# - arm64  → osxexperts.net（原生 arm64 静态构建；文件名随版本滚动，
#            构建时抓列表页取最新一条。evermeet 的 getrelease 是 x86_64，
#            曾导致 arm64 包里混入 Intel ffmpeg——现由下方 lipo 自校验兜底，
#            源失效会在 fetch 阶段明确报错而非静默装错架构）
# 也可用 FFMPEG_ZIP_URL / FFPROBE_ZIP_URL 手动指定直链覆盖。
OSXEXPERTS_LIST_URL="https://www.osxexperts.net/"
FFMPEG_ZIP_URL="${FFMPEG_ZIP_URL:-}"
FFPROBE_ZIP_URL="${FFPROBE_ZIP_URL:-}"

# 从 osxexperts.net 列表页解析 <bin> 的最新 arm64 zip 直链（相对路径补全域名）。
# 解析失败 return 1，由调用方给出可操作的报错（含手动覆盖方式）。
fetch_arm64_url() {  # $1 = ffmpeg|ffprobe
    local bin="$1" html url
    html="$(curl -fsSL --max-time 60 "$OSXEXPERTS_LIST_URL")" || return 1
    url="$(echo "$html" | grep -io "href=\"[^\"]*${bin}[^\"]*arm[^\"]*\\.zip\"" \
        | head -1 | sed -E 's/^href="(.*)"$/\1/I')" || return 1
    [ -n "$url" ] || return 1
    case "$url" in
        http*) echo "$url" ;;
        *)     echo "https://www.osxexperts.net/$url" ;;
    esac
}

# 架构判定：二进制包含目标架构（universal2 含 arm64 视为通过）返回 0。
arch_ok() {  # $1 = 二进制路径
    local got
    got="$(lipo -archs "$1" 2>/dev/null || echo unknown)"
    case "$got" in
        *"$ARCH"*) return 0 ;;
        *) return 1 ;;
    esac
}

# 下载后架构强校验：不匹配即报错退出（含手动覆盖提示）。
verify_arch() {  # $1 = 二进制路径
    arch_ok "$1" && return 0
    local got
    got="$(lipo -archs "$1" 2>/dev/null || echo unknown)"
    echo "[ffmpeg] error: $1 架构不符（got: $got，want: $ARCH）" >&2
    echo "[ffmpeg] hint: 下载源未提供 ${ARCH} 构建；用 FFMPEG_ZIP_URL/FFPROBE_ZIP_URL 指定直链后重试" >&2
    exit 1
}

mkdir -p "$DIR"
echo "[ffmpeg] arch=$ARCH -> $DIR"

# 优先复用系统已安装的 ffmpeg/ffprobe（Homebrew 等），避免大体积下载；
# 复用前先做架构判定（本机缓存可能是旧版脚本级下载的 Intel 构建），不符即
# 删除走下载。缺失时才从分架构镜像获取。
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

for name in ffmpeg ffprobe; do
    if [ -x "$DIR/$name" ]; then
        if arch_ok "$DIR/$name"; then
            echo "[ffmpeg] $name already present (arch ok), skip"
            continue
        fi
        echo "[ffmpeg] $name 已存在但架构不符（want $ARCH），删除后重新获取"
        rm -f "$DIR/$name"
    fi
    sys_bin="$(command -v "$name" 2>/dev/null || true)"
    if [ -n "$sys_bin" ] && [ -x "$sys_bin" ] && arch_ok "$sys_bin"; then
        echo "[ffmpeg] copying system $name ($sys_bin)"
        cp -f "$sys_bin" "$DIR/$name"
        chmod +x "$DIR/$name"
        continue
    elif [ -n "$sys_bin" ]; then
        echo "[ffmpeg] 系统 $name 架构不符（want $ARCH），改用分架构镜像下载"
    fi
    url_var="FFMPEG_ZIP_URL"; [ "$name" = "ffprobe" ] && url_var="FFPROBE_ZIP_URL"
    url="${!url_var}"
    if [ -z "$url" ] && [ "$ARCH" = "arm64" ]; then
        url="$(fetch_arm64_url "$name")" || {
            echo "[ffmpeg] error: 未能从 $OSXEXPERTS_LIST_URL 解析 $name 的 arm64 下载链接；" \
                 "用 $url_var=<直链> 手动指定后重试" >&2
            exit 1
        }
    fi
    if [ -z "$url" ]; then
        echo "[ffmpeg] error: 未确定 $name 的下载地址（arch=$ARCH）" >&2
        exit 1
    fi
    echo "[ffmpeg] downloading $name from $url"
    curl -fL "$url" -o "$tmp/$name.zip"
    ditto -x -k "$tmp/$name.zip" "$tmp" || unzip -o -q "$tmp/$name.zip" -d "$tmp"
    find "$tmp" -type f -name "$name" -perm -111 -exec cp {} "$DIR/$name" \;
    # 个别 zip 会丢执行位：find(-perm -111) 落空时按普通文件补拷
    if [ ! -f "$DIR/$name" ] && [ -f "$tmp/$name" ]; then
        echo "[ffmpeg] warn: $name 在 zip 内无执行位，按普通文件拷贝"
        cp "$tmp/$name" "$DIR/$name"
    fi
    chmod +x "$DIR/$name"
done

# 装完后逐个强校验架构——fetch 阶段就拦住错架构，不等到打包门禁才失败
for name in ffmpeg ffprobe; do
    [ -f "$DIR/$name" ] && verify_arch "$DIR/$name"
done

echo "[ffmpeg] done:"
ls -lh "$DIR" | grep -E "ffmpeg|ffprobe"
