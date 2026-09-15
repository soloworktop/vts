# syntax=docker/dockerfile:1
# VTS 多阶段构建：构建期只需要 Node（编译 React 前端），运行期只需要 Python 运行时。
# 用法见 docker/README.md；构建上下文 = 仓库根（compose 已配好）。

# ---------------------------------------------------------------- stage 1: 前端构建
# pnpm 11 需要 Node 22+（Node 20 下 pnpm install 实测报 ERR_UNKNOWN_BUILTIN_MODULE，
# 勿再降回 node:20，否则构建必失败）。
FROM node:22-slim AS web-build

# 默认官方源；国内可传 PIP_INDEX_URL / NPM_REGISTRY 加速，见 README
ARG PIP_INDEX_URL=
ARG NPM_REGISTRY=

WORKDIR /build

# pnpm 版本与仓库对齐（web-src 无 packageManager 字段；lockfileVersion 9.0 由 pnpm 9+
# 读写同一格式，仓库实际以 pnpm 11.x 开发 → 固定 11.8.0）。
RUN npm install -g pnpm@11.8.0 ${NPM_REGISTRY:+--registry="$NPM_REGISTRY"}

# 先拷清单文件 → 利用层缓存装依赖，再拷源码（web-src/src 改动不触发重装）。
# 必须连同 pnpm-workspace.yaml 一起拷入依赖层（onlyBuiltDependencies: [esbuild] 在
# 该文件里），否则 pnpm 11 忽略 esbuild 构建脚本，实测报
# ERR_PNPM_IGNORED_BUILDS: Ignored build scripts: esbuild@0.21.5 → exit 1。
COPY web-src/package.json web-src/pnpm-lock.yaml web-src/pnpm-workspace.yaml ./web-src/
RUN cd web-src && pnpm install --frozen-lockfile ${NPM_REGISTRY:+--registry="$NPM_REGISTRY"}

# 必须保留 web-src 层级（拷到 /build/web-src 而非 /build 根）：copy-build.mjs 以
# __dirname/../.. 反推仓库根，拍平后 /build/src/... 不存在，实测报
# ENOENT: scandir '/src/video_to_summary/web/static'。
COPY web-src/ ./web-src/
# copy-build.mjs 会把 dist → 仓库根的 src/video_to_summary/web/static，且保留
# user-guide.html / README.md。构建期需要该目录存在（含需保留的文件）。
COPY src/video_to_summary/web/static/ /build/src/video_to_summary/web/static/
RUN cd web-src && pnpm build

# ---------------------------------------------------------------- stage 2: Python 运行
FROM python:3.12-slim

# 默认官方源；国内可传 PIP_INDEX_URL / NPM_REGISTRY 加速，见 README
ARG PIP_INDEX_URL=
ARG NPM_REGISTRY=

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # 运行时可配置项（也均可在 compose/运行期覆盖）
    PORT=8080 \
    VIDEO_TO_SUMMARY_DB=/data/app.db \
    VIDEO_TO_SUMMARY_OUTPUT_DIR=/output

# 默认官方 apt 源；国内可传 APT_MIRROR=https://mirrors.tuna.tsinghua.edu.cn 加速，见 README
ARG APT_MIRROR=

# 发布版本号：发布流水线经 --build-arg VIDEO_TO_SUMMARY_VERSION=<tag> 注入（如 v0.3.0）。
# 运行期 version.py 的 env 优先级最高（非空即返回）；留空 = 本地源码构建，回落
# 包元数据/开发态，不改变默认行为（见 src/video_to_summary/version.py）。
ARG VIDEO_TO_SUMMARY_VERSION=
ENV VIDEO_TO_SUMMARY_VERSION=${VIDEO_TO_SUMMARY_VERSION}

# ffmpeg：yt-dlp 抽音频/合流必需；tzdata：让 TZ 环境变量生效。
# APT_MIRROR 非空时先把 Debian 源替换为该镜像（python:3.12-slim 基于 Debian trixie，
# 用 deb822 格式 /etc/apt/sources.list.d/debian.sources，脚本同时兼容旧格式
# /etc/apt/sources.list），再 apt-get update。归一化（去尾部 "/"、再去尾部 "/debian"）
# 在 docker/setup-apt-mirror.sh 里用纯 shell 参数展开完成——不能写在 RUN 里：该
# Docker/BuildKit 环境下 $$ 不是 Dockerfile 转义而是 shell 进程 PID（$$=1），实测会把
# 源改坏成 URIs: 1M/debian（详见脚本头注释）。这里只把 ARG 展开后的值作为环境变量传给
# 脚本；不要用 $$，也不依赖 Dockerfile 解析器支持 ${VAR%pattern}。
# APT_MIRROR 为空 = 官方源，脚本直接退出不改文件，apt 命令与原状逐字符等价。
COPY docker/setup-apt-mirror.sh /tmp/setup-apt-mirror.sh
RUN APT_MIRROR="${APT_MIRROR}" sh /tmp/setup-apt-mirror.sh \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/* /tmp/setup-apt-mirror.sh

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
# 预构建的静态资源（index.html + assets/ + user-guide.html + README.md）。
# 必须保留：.dockerignore 把 static/index.html 与 static/assets 排除在构建上下文外，
# 只有这一句能把前端产物带进 /app/src，进而被下方 `pip install .` 的 package-data
# （pyproject 已显式列出 web/static/assets/*，见 tests/test_packaging.py 门禁）装入
# site-packages —— 运行时 uvicorn 从 site-packages 读 static，删了会首页白屏。
COPY --from=web-build /build/src/video_to_summary/web/static ./src/video_to_summary/web/static

RUN python -m pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} .

# 非 root 运行；数据与产物走 volume（compose 已声明）。
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin vts \
    && mkdir -p /data /output \
    && chown -R vts:vts /data /output /app

USER vts
EXPOSE 8080

VOLUME ["/data", "/output"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/api/v1/health',timeout=3)"

CMD ["sh", "-c", "exec uvicorn video_to_summary.web.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
