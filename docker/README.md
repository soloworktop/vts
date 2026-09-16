# VTS Docker 部署

本目录提供 VTS 的**多阶段 Docker 构建**：构建期只需要 Node（编译 React 前端），
运行期只需要 Python 运行时 + 预构建的静态资源 + `ffmpeg`（yt-dlp 抽音频需要）。
本文件为中文单语的部署手册；项目主文档（仓库根 `README.md` / `README.en.md`）提供
中英双语，部署细节以本文件为权威。

```text
docker/
├── Dockerfile              多阶段构建（stage1＝前端构建，stage2＝Python 运行时构建阶段）
├── docker-compose.yml      一键起服务（端口 / 数据卷 / 可选配置）
├── setup-apt-mirror.sh     APT_MIRROR 归一化 + 替换 Debian 源（stage2 构建期运行）
└── README.md               本文档
```

仓库根另有 `.dockerignore`（构建上下文排除 `node_modules`/`data`/`output*`/`.git` 等）。

**前置条件**

- 装有 **Docker**；源码构建（方式二）还需要 **Compose v2**（`docker compose` 子命令可用，
  `docker compose version` 可自检）。compose 文件使用了 v2 的顶层 `name:` 字段，
  v1 `docker-compose` 不支持。
- 方式二（源码构建）的构建上下文是**仓库根**（需要 `web-src/` 与 `src/`），命令都在
  仓库根执行；方式一（预构建镜像）无需克隆仓库。
- 首次**构建**需联网拉取 base 镜像与 Python/Node 依赖，通常数分钟，视网络而定（国内加速
  见「加速构建（可选）」）；预构建镜像只拉镜像层。

---

## 快速开始

### 方式一：预构建镜像（推荐——无需克隆仓库、无需本地构建）

发布流水线把多架构镜像（amd64 / arm64）推送到 GitHub Container Registry，直接拉取运行：

```bash
docker run -d --name vts -p 8080:8080 \
  -v vts_data:/data -v vts_output:/output \
  --restart unless-stopped \
  ghcr.io/soloworktop/vts:latest
```

- 标签：`latest` = 最新发布；固定版本用 `vX.Y.Z`（与
  [GitHub Release](https://github.com/soloworktop/vts/releases) 同版本号）。
- 数据存 `vts_data` / `vts_output` 两个卷，删容器不丢。
- 环境变量（Key / 风控 / Token 等）用 `-e` 传入，见下方「配置」。

### 方式二：源码构建（已克隆仓库 / 需要自定义）

```bash
# 在仓库根执行（以下两种等价，任选其一）
docker compose -f docker/docker-compose.yml up -d --build
# 或
bash scripts/docker.sh up
```

首次会构建镜像（需联网拉取 base 镜像与 Python/Node 依赖）。启动后访问：

- Web 控制台：<http://127.0.0.1:8080>
- 健康检查：<http://127.0.0.1:8080/api/v1/health>

查看日志：`docker compose -f docker/docker-compose.yml logs -f`
停止/删除（数据卷保留）：`docker compose -f docker/docker-compose.yml down`

> **⚠️ 注意事项**
>
> 1. **不要对本项目使用 `--remove-orphans`**：`down`/`up` 均**不带**该标志——它会删除
>    同一项目名下不属于本服务栈的「孤儿」容器，若本机有其他栈恰好与 VTS 共用项目
>    命名空间，会被一并误删。删除前请确认
>    `docker compose -f docker/docker-compose.yml ps` 列出的容器都属于本栈。
> 2. **端口**：宿主端口可用环境变量覆盖：
>    `VTS_PORT=9000 docker compose -f docker/docker-compose.yml up -d`。
>    宿主 8080 被占用时，请显式指定一个未占用端口——不用 sudo 时即此写法；需要 sudo
>    时用下面第 3 条的两种写法之一。
> 3. **`sudo` 会清空环境变量**（实测）：`VTS_PORT=18080 sudo docker compose ...`
>    **不生效**，容器仍映射到 8080。需要 sudo 时请用下面两种写法之一：
>
>    ```bash
>    # 写法一：把变量放在 sudo 之后，作为被执行的命令的一部分
>    sudo VTS_PORT=18080 docker compose -f docker/docker-compose.yml up -d --build
>    # 写法二：先 sudo -E 保留调用者环境，再按普通方式传变量
>    VTS_PORT=18080 sudo -E docker compose -f docker/docker-compose.yml up -d --build
>    ```

---

## 配置

服务端可配置项均通过环境变量传入（compose `environment:` 或 `docker run -e`）：

| 变量 | 说明 | 默认 |
|---|---|---|
| `PORT` | 容器内监听端口 | `8080` |
| `VIDEO_TO_SUMMARY_DB` | SQLite 库路径 | `/data/app.db` |
| `VIDEO_TO_SUMMARY_OUTPUT_DIR` | 产物目录 | `/output` |
| `VIDEO_TO_SUMMARY_TOKEN` | 可选鉴权 Token（暴露到局域网/公网前**必须**配置） | 空 |
| `VIDEO_TO_SUMMARY_MAX_CONCURRENT` | Web 同时运行任务数上限 | `2` |
| `VIDEO_TO_SUMMARY_JOB_TIMEOUT` | 单任务超时秒数 | `0` = 不限 |
| `VTS_STATIC_DIR` | 自定义前端：指向含 `index.html` 的目录时，首页与 `/static/*` 由该目录提供（无效则回落包内前端） | 空 |
| `TZ` | 时区（镜像内置 tzdata） | `Asia/Shanghai` |
| `VTS_USER_AGENT` | 自定义 yt-dlp User-Agent；B 站 412 解法之一（原理与注意事项见「网络与风控」） | 空 = yt-dlp 默认 UA |
| `VTS_COOKIES_FILE` | 登录 cookies 文件路径，等价 CLI `--cookies`；B 站 412 解法之一（见「网络与风控」） | 空 |
| `SUMMARY_API_KEY` / `SUMMARY_BASE_URL` / `SUMMARY_MODEL` | BYOK：任意 OpenAI 兼容端点的默认接入信息（旧名 `LLM_*` / `OPENAI_API_KEY` 仍被识别） | 空 |
| `ASR_API_KEY` / `ASR_BASE_URL` / `ASR_MODEL` | 转写接口（仅当视频没有字幕时需要） | 空 |
| `ASR_RESPONSE_FORMAT` | 转写响应格式（`json`/`text`/`verbose_json`，默认自动降级，详见下节） | 自动降级 |

> 上表均为**运行期**环境变量；构建期镜像源参数（`APT_MIRROR` / `PIP_INDEX_URL` /
> `NPM_REGISTRY`）见「加速构建（可选）」。

### 转写响应格式

`ASR_RESPONSE_FORMAT` 默认**自动降级**：先请求 `verbose_json`（含分段/时间戳，可产出
`.srt` 字幕）；若端点以 HTTP 400 且错误信息提及 `response_format` 拒绝，则自动降级
`json`（仅文本，无 SRT）并打 WARNING。部分兼容端点只支持 `json`/`text`，此时可固定
格式，避免每个视频都先失败一次再降级；非法取值会告警并回落默认行为。

### 配置 LLM（生成总结必需）

不配置 LLM Key 时仍可运行「字幕优先」路径，但只会产出转写原文（`.txt`/`.srt`）并跳过
总结。需要 LLM 摘要时，任选其一：

1. 编辑 `docker/docker-compose.yml`，取消注释并填写 `LLM_*` 变量后重启。
2. 或通过 Web 控制台界面配置（`设置 → LLM`），Key 以 Fernet 加密后存入 `/data` 的
   SQLite 库，只返回掩码值。重启容器后配置仍在（数据在卷里）。

没有配置 Key 时任务不会失败：仍产出转写原文（`.txt`/`.srt`），跳过总结阶段；
补上 Key 后对任务点「重新生成」即可拿到完整笔记。（前提是视频有字幕：视频无字幕且未配
任何转写 Key 时，任务会以引导配置的报错结束，不会静默出空笔记。）

---

## 数据持久化

| 卷 | 挂载点 | 内容 |
|---|---|---|
| `vts_data` | `/data` | SQLite 库 `app.db`（含任务、标签、加密后的 LLM Key）、`enc_key` |
| `vts_output` | `/output` | 任务产物（`.txt` / `.srt` / `.segments.json` / `.summary.md`） |

两个都是 Docker **named volume**：`docker compose down` 不会删除数据；
需要彻底清空时用 `docker volume rm vts_vts_data vts_vts_output`。

> 卷名为什么带双 `vts_` 前缀：compose 顶层固定项目名 `name: vts`（不随目录名走——
> 本文件位于 `docker/` 目录，不固定则项目名默认取目录名 `docker`，容易与其他放在
> `docker/` 目录的 compose 栈共用同一项目命名空间）。项目名决定卷/网络的默认前缀，
> 因此实际卷名形如 `vts_vts_data` / `vts_vts_output`（网络名为 `vts_default`）。

本机实际位置：`docker volume inspect vts_vts_data` 查看输出的 `Mountpoint`——Linux 宿主
一般在 `/var/lib/docker/volumes/` 下；macOS/Windows（Docker Desktop）存放在其虚拟机内，
宿主机上没有对应路径。想直接映射宿主目录，把 compose 里的 `vts_data:/data` 改成
`/path/on/host:/data`（注意目录权限：容器内以 UID 10001 运行）。

### 附录：从旧 `docker_` 前缀卷迁移（仅历史部署需要）

> 本项目尚未发布、无存量用户，此次固定项目名对全新部署是零迁移成本；下述步骤仅供
> 内部/早期部署者对照。**若你之前用未固定项目名的版本跑过**，旧卷前缀为 `docker_`
> （即 `docker_vts_data` / `docker_vts_output`），请先迁移数据、确认无误后再删旧卷——
> 直接删除会静默丢掉 `/data` 卷里已配置的 LLM 凭据（SQLite + `enc_key`）与全部历史任务。
> 删除条件：待早期部署窗口过去、确认已无 `docker_` 前缀卷存量后，本节可整体移除。

```bash
# 1. 先停掉旧容器（避免迁移过程中写入）
docker compose -f docker/docker-compose.yml down
# 2. 建新卷，把旧卷内容复制过去（cp -a 保留文件属主，容器内 UID 10001 不受影响）
docker volume create vts_vts_data
docker volume create vts_vts_output
docker run --rm -v docker_vts_data:/from -v vts_vts_data:/to alpine sh -c 'cp -av /from/. /to/'
docker run --rm -v docker_vts_output:/from -v vts_vts_output:/to alpine sh -c 'cp -av /from/. /to/'
# 3. 启动并确认配置与历史都在（Web 控制台能看到之前的任务与 LLM 配置）
docker compose -f docker/docker-compose.yml up -d
# 4. 确认无误后再删旧卷
docker volume rm docker_vts_data docker_vts_output
```

---

## 网络与风控（B 站 412 等）

**根因（服务器实测）**：B 站边缘 WAF（站点防火墙）按「UA × IP 信誉」对 `/video/` HTML 页
发起挑战，yt-dlp 默认（浏览器式）UA 必现 `HTTP Error 412: Precondition Failed`；换成
非浏览器 UA（如 `Wget/1.21.3` 稳定通过）即恢复。Referer 等请求头无效；B 站的数据接口
路径（API 端点）不受影响，被挑战的只有 `/video/` 网页。

三套解法（按需任选其一；Docker 部署把变量写进 compose 的 `environment:`，或改用
`docker run -e`）：

1. **自定义 User-Agent**（最快，一行配置）：

   ```yaml
   services:
     vts:
       environment:
         - VTS_USER_AGENT=Wget/1.21.3
   ```

   非空即作为 yt-dlp 的 `user_agent` 传入并写入请求头 `http_headers["User-Agent"]`
   （CLI / Web / Docker 三条路径都生效）。为什么两个位置都要注入：B 站提取路径上
   仅设 `user_agent` 实测不生效——请求头仍是默认 UA，仍 412。不设置时保持 yt-dlp
   默认 UA——绝大多数站点用默认 UA 是正常的，**不要**全局默认换成非浏览器 UA。

2. **提供登录 cookies**：B 站 CC/AI 字幕需要登录态，而 Web/Docker 形态没有本机浏览器
   （「浏览器 cookies」不可用）。把 cookies.txt 挂进容器并设 `VTS_COOKIES_FILE`：

   ```yaml
   services:
     vts:
       volumes:
         - ./cookies.txt:/data/cookies.txt:ro
       environment:
         - VTS_COOKIES_FILE=/data/cookies.txt
   ```

   `VTS_COOKIES_FILE` 等价 CLI `--cookies`：显式 cookies 文件**优先于**浏览器 cookies
   （复用同一互斥逻辑，绝不并存）；文件不存在时应用会 WARN 并说明路径，不会静默
   当作已生效。同一份配置同时解锁 B 站 CC/AI 字幕与 YouTube 自动字幕匿名限流（429）
   场景。

3. **配置代理**：在 Web 控制台「设置 → 网络与访问」填代理并保存（等价 CLI `--proxy`），
   任务默认配置会随任务带入。

### 临时自救：挂载 yt-dlp 配置文件

VTS 调用 yt-dlp 时未禁用配置文件（未传 `--ignoreconfig`），因此容器内 yt-dlp 保持默认
行为，会读取用户配置路径 `/home/vts/.config/yt-dlp/config`（用户 `vts` 的家目录，XDG
默认路径）。**无需改代码**的临时解法是把该配置文件挂载进去——产品级一等入口仍是上面的
`VTS_USER_AGENT` / `VTS_COOKIES_FILE`，这份配置文件是过渡期自救手段（升级后两种方式
可并存，配置文件的生效优先级由 yt-dlp 自己决定）。

把下面片段存为 `docker/docker-compose.override.yml`（与主 compose 同目录，Compose 自动
合并；也可用 `-f` 显式指定），再重新 `up -d` 即可：

```yaml
# docker/docker-compose.override.yml
services:
  vts:
    volumes:
      - ./yt-dlp.config:/home/vts/.config/yt-dlp/config:ro
```

```bash
docker compose -f docker/docker-compose.yml up -d --build   # override 自动生效
# 或显式指定：docker compose -f docker/docker-compose.yml -f docker/docker-compose.override.yml up -d
```

`yt-dlp.config` 内容示例（cookies 文件挂载见上面解法 2，`--cookies` 路径要与挂载点一致）：

```ini
# 临时自救：B 站 412 挑战（产品级入口见 VTS_USER_AGENT / VTS_COOKIES_FILE）
--user-agent Wget/1.21.3
--cookies /data/cookies.txt
```

---

## 加速构建（可选）

默认构建走**官方源**（npm registry / PyPI / Debian apt），海外部署无需任何改动。国内云
服务器拉取依赖可能很慢：stage1 `pnpm install`、stage2 `pip install` 拉
fastapi/yt-dlp/cryptography 等大包，而 `apt-get update` 默认走 `deb.debian.org` 源，
**单独跑 5 分钟以上仍未完成是常事**（国内首次构建的最大瓶颈）。可通过构建参数切换到
镜像源加速——**只影响构建期**，不改变默认行为（参数留空 = 官方源，行为与未传参数时
完全一致）：

```bash
# 国内配方（镜像源参数默认空 = 官方源，仅在非空时生效）
APT_MIRROR=https://mirrors.tuna.tsinghua.edu.cn \
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
NPM_REGISTRY=https://registry.npmmirror.com \
VTS_PORT=8080 docker compose -f docker/docker-compose.yml up -d --build
```

> 需要 sudo 时写法同「快速开始·注意事项」：变量放在 `sudo` 之后，或先 `sudo -E`
> 保留环境。

| 构建参数 | 作用 | 默认 |
|---|---|---|
| `APT_MIRROR` | stage2 `apt-get update`/`install` 的 Debian apt 镜像源。**建议只填 host**（如 `https://mirrors.tuna.tsinghua.edu.cn`，镜像会把 `/debian` 路径自动拼回），也接受带 `/debian` 后缀或尾部 `/` 的地址——由 `docker/setup-apt-mirror.sh` 构建期自动归一化（去尾部 `/`、再去尾部 `/debian`），不会产生 `debian/debian` 路径重复 | 空 = Debian 官方源 |
| `PIP_INDEX_URL` | stage2 `pip install` 的 `--index-url`（PyPI 镜像源） | 空 = PyPI 官方源 |
| `NPM_REGISTRY` | stage1 npm/pnpm 的 `--registry`（npm 镜像源） | 空 = npm 官方源 |
| `VIDEO_TO_SUMMARY_VERSION` | 注入镜像的**版本号**（构建期 build-arg，同时固化为运行期 env；`version.py` 的 env 优先级最高，非空即返回）。发布流水线传 git tag（如 `v0.3.0`），本地构建留空 | 空 = 回落包元数据/开发态 |

---

## 升级

预构建镜像（方式一）：拉新镜像后重建容器，数据卷不受影响（无需迁移步骤）：

```bash
docker pull ghcr.io/soloworktop/vts:latest
docker rm -f vts
docker run -d --name vts -p 8080:8080 \
  -v vts_data:/data -v vts_output:/output \
  --restart unless-stopped \
  ghcr.io/soloworktop/vts:latest
```

源码构建（方式二）：

```bash
git pull                                # 拉取新代码
docker compose -f docker/docker-compose.yml up -d --build
```

`--build` 会用新代码重建镜像；数据卷不受影响（无需迁移步骤）。若镜像层缓存未命中
需要重新拉 base 镜像，请确保构建机可访问 Docker Hub。

---

## 高级

- **镜像构建测试**（无 compose）：

  ```bash
  docker build -t vts:local -f docker/Dockerfile .
  docker run --rm -p 8080:8080 \
    -v vts_data:/data -v vts_output:/output vts:local
  ```

- **手工在容器外构建前端**（非 Docker 部署 / 单独调试 `web-src`）：需要 **Node 22+ 与
  pnpm 11**，版本要求与构建步骤见 `web-src/README.md`「开发」（单点维护，不在此重复）。
  容器内构建由 Dockerfile stage1（`node:22-slim`）自动完成，无需在宿主机装 Node。

- **不需要 Docker 时**：`python3 -m venv .venv && source .venv/bin/activate &&
  pip install -e . && bash scripts/web.sh`（见根 `README.md`「快速开始」）。

- **容器内权限**：容器内以非 root 用户（UID 10001）运行，`/data` 与 `/output` 由 named
  volume 管理，无需在镜像内保留写权限。

---

## 卸载与清理

```bash
docker compose -f docker/docker-compose.yml down      # 停止并移除容器（数据卷保留）
docker volume rm vts_vts_data vts_vts_output          # 删除数据卷（不可恢复）
docker image rm vts:local                             # 删除镜像
```

`volume rm` 会**不可恢复地删除**全部任务历史与加密存储的 LLM 配置（`app.db` +
`enc_key`），执行前确认不再需要这些数据（或先从卷拷出备份）。
