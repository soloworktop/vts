#!/bin/sh
# docker/setup-apt-mirror.sh —— 构建期把 Debian apt 源替换为镜像源（APT_MIRROR）。
#
# 为什么不能写在 Dockerfile 的 RUN 里（真实 Docker/BuildKit 服务器实测）：
#   1) 该环境下 $$ 并不是「Dockerfile 转义」，而是被原样交给 shell：
#      $$ = shell 进程 PID = 1。变体探针实测：
#        ${APT_MIRROR}                → https://mirrors.tuna.tsinghua.edu.cn  （构建期展开，正确）
#        ${APT_MIRROR%/debian}        → https://mirrors.tuna.tsinghua.edu.cn  （解析器静默忽略 % 修饰！）
#        $${APT_MIRROR%/}             → 1{APT_MIRROR%/}                       （$$ 变成 PID 1）
#        M="$${APT_MIRROR%/}"; M="$${M%/debian}"; echo "$$M"  →  1M           （于是 URIs: 1M/debian）
#      真实报错：E: Malformed entry 1 in sources file ... (URI parse)，URIs: 1M/debian。
#   2) Dockerfile 解析器对 ${VAR%pattern} 的 % 修饰支持不确定（实测被静默忽略），不可依赖。
#   结论：归一化逻辑必须放进真正的脚本文件——构建期把 ARG 展开后的值作为环境变量传入，
#   在 shell 里自由使用参数展开（这里没有 Dockerfile 转义问题）。因此不要用 $$，
#   也不要指望 Dockerfile 解析器处理 ${VAR%pattern}。
#
# 输入：环境变量 APT_MIRROR（空 = 官方源，直接 exit 0、不改任何文件）。
# 输出：把 /etc/apt/sources.list 与 /etc/apt/sources.list.d/*.sources（deb822）中的
#       http://deb.debian.org / https://deb.debian.org 替换为归一化后的镜像地址；
#       目标文件不存在时跳过（不报错）。
# 测试注入：默认改真实路径；可用 APT_SOURCES_GLOB 覆盖目标文件清单（见 docker/README.md）。
set -u

APT_MIRROR="${APT_MIRROR:-}"
[ -n "$APT_MIRROR" ] || exit 0

# 归一化：去尾部 "/"，再去尾部 "/debian"——纯 host 形式与带 /debian 后缀的地址都可用，
# 替换后不会产生 debian/debian 路径重复。
MIRROR="${APT_MIRROR%/}"
MIRROR="${MIRROR%/debian}"

# 目标文件；默认真实路径。APT_SOURCES_GLOB 为空/无匹配时循环体自然跳过。
APT_SOURCES_GLOB="${APT_SOURCES_GLOB:-/etc/apt/sources.list /etc/apt/sources.list.d/*.sources}"

# 探测 sed 的 -i 写法：GNU sed（Debian 容器内）为 `-i`，BSD sed（本地测试）为 `-i ''`。
if sed --version >/dev/null 2>&1; then
    SED_GNU=1
else
    SED_GNU=0
fi

EXPR="s|http://deb.debian.org|$MIRROR|g; s|https://deb.debian.org|$MIRROR|g"
for f in $APT_SOURCES_GLOB; do
    [ -f "$f" ] || continue
    if [ "$SED_GNU" = 1 ]; then
        sed -i "$EXPR" "$f" 2>/dev/null || true
    else
        sed -i '' "$EXPR" "$f" 2>/dev/null || true
    fi
done

exit 0
