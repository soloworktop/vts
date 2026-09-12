# VTS Docker 部署

本目录提供 VTS 的**多阶段 Docker 构建**：构建期只需要 Node（编译 React 前端），
运行期只需要 Python 运行时 + 预构建的静态资源 + `ffmpeg`（yt-dlp 抽音频需要）。

```
docker/
├── Dockerfile              多阶段构建（node → python）
├── docker-compose.yml      一键起服务（端口 / 数据卷 / 可选配置）
├── setup-apt-mirror.sh     APT_MIRROR 归一化 + 替换 Debian 源（stage2 构建期）
└── README.md               本文档
```

仓库根另有 `.dockerignore`（构建上下文排除 `node_modules`/`data`/`output*`/`.git` 等）。

---

## 快速开始

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
停止/删除：`docker compose -f docker/docker-compose.yml down`

> **⚠️ 安全提示：不要对本项目使用 `--remove-orphans`。** `down`/`up` 均**不带**
> `--remove-orphans` 标志（该标志会删除同一项目名下不属于本服务栈的"孤儿"容器，
> 若本机有其他栈恰好与 VTS 共用项目命名空间，会被一并误删）。同理，删除前请确认
> `docker compose -f docker/docker-compose.yml ps` 列出的容器都属于本栈。

> 端口可用环境变量覆盖：`VTS_PORT=9000 docker compose -f docker/docker-compose.yml up -d`。
>
> **`sudo` 会清空环境变量**（实测）：`VTS_PORT=18080 sudo docker compose ...` **不生效**，
> 容器仍映射到 8080。需要 sudo 时请用下面两种写法之一：
>
> ```bash
> # 写法一：把变量放在 sudo 之后，作为被执行的命令的一部分
> sudo VTS_PORT=18080 docker compose -f docker/docker-compose.yml up -d --build
> # 写法二：先 sudo -E 保留调用者环境，再按普通方式传变量
> VTS_PORT=18080 sudo -E docker compose -f docker/docker-compose.yml up -d --build
> ```
>
> 宿主 8080 被占用时**必须**用上面的写法显式指定 `VTS_PORT`（换成未被占用的端口）。

---

## 加速构建（可选）

默认构建走**官方源**（npm registry / PyPI / Debian apt），海外部署无需任何改动。国内云服务器
拉取依赖可能很慢：stage1 `pnpm install`、stage2 `pip install` 拉 fastapi/yt-dlp/cryptography
等大包，而 `apt-get update` 默认走 `deb.debian.org` 源，**单独跑 5 分钟以上仍未完成是常事**
（实测即国内首次构建的最大瓶颈，直接拖垮整次 `timeout 900` 构建）。可通过构建参数切换到
镜像源加速——**只影响构建期**，不改变默认行为（参数为空 = 官方源，与非空时完全等价于原构建）：

```bash
# 国内配方（镜像源参数默认空 = 官方源，仅在非空时生效）
APT_MIRROR=https://mirrors.tuna.tsinghua.edu.cn \
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
NPM_REGISTRY=https://registry.npmmirror.com \
VTS_PORT=8080 docker compose -f docker/docker-compose.yml up -d --build
```

> `APT_MIRROR` **建议只填 host**（如 `https://mirrors.tuna.tsinghua.edu.cn`，镜像会把
> `/debian` 路径自动拼回）；也可传带 `/debian` 的地址（如
> `https://mirrors.tuna.tsinghua.edu.cn/debian` 或带尾部 `/` 的写法），构建期由
> `docker/setup-apt-mirror.sh` **自动归一化**（去尾部 `/`、再去尾部 `/debian`），不会产生
> `debian/debian` 路径重复。

> **`sudo` 会清空环境变量**（实测，见上方「快速开始」）：需要 sudo 时把变量放在
> `sudo` **之后**，作为被执行的命令的一部分：
>
> ```bash
> sudo APT_MIRROR=https://mirrors.tuna.tsinghua.edu.cn \
>      PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
>      NPM_REGISTRY=https://registry.npmmirror.com \
>      VTS_PORT=8080 docker compose -f docker/docker-compose.yml up -d --build
> # 或先 sudo -E 保留调用者环境：
> # APT_MIRROR=... PIP_INDEX_URL=... NPM_REGISTRY=... sudo -E docker compose -f docker/docker-compose.yml up -d --build
> ```

首次构建通常数分钟，视网络而定。

| 构建参数 | 作用 | 默认 |
|---|---|---|
| `APT_MIRROR` | stage2 `apt-get update`/`install` 的 Debian 源（apt 镜像源；**建议只填 host**，如 `https://mirrors.tuna.tsinghua.edu.cn`；也接受带 `/debian` 后缀的地址，由 `docker/setup-apt-mirror.sh` 自动归一化） | 空 = Debian 官方源 |
| `PIP_INDEX_URL` | stage2 `pip install` 的 `--index-url`（PyPI 镜像源） | 空 = PyPI 官方源 |
| `NPM_REGISTRY` | stage1 npm/pnpm 的 `--registry`（npm 镜像源） | 空 = npm 官方源 |

> 以上三个参数只影响**构建期**（compose `build.args` 透传宿主环境变量），运行期不生效。

---

## 数据持久化

> **项目名固定为 `vts`**（compose 顶层 `name: vts`，不再随目录名走——本文件位于
> `docker/` 目录，若不固定则项目名默认取目录名 `docker`，容易与其他放在
> `docker/` 目录的 compose 栈共用同一项目命名空间）。项目名决定卷/网络的默认
> 前缀，因此**实际卷名形如 `vts_vts_data` / `vts_vts_output`**（网络名为
> `vts_default`）。本项目尚未发布、无存量用户，此次固定项目名为零迁移成本；
> **若你之前用未固定项目名的版本跑过，旧卷前缀为 `docker_`（即
> `docker_vts_data` / `docker_vts_output`），请先迁移数据、确认无误后再删旧卷**——
> 直接删除会静默丢掉 `/data` 卷里已配置的 LLM 凭据（SQLite + `enc_key`）与全部历史任务：

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

| 卷 | 挂载点 | 内容 |
|---|---|---|
| `vts_data` | `/data` | SQLite 库 `app.db`（含任务、标签、加密后的 LLM Key）、`enc_key` |
| `vts_output` | `/output` | 任务产物（`.txt` / `.srt` / `.segments.json` / `.summary.md`） |

两个都是 Docker **named volume**：`docker compose down` 不会删除数据；
需要彻底清空时用 `docker volume rm vts_vts_data vts_vts_output`。

本机实际位置：`docker volume inspect vts_vts_data`（macOS/Linux 一般在
`/var/lib/docker/volumes/` 下）。想直接映射宿主目录，把 compose 里的
`vts_data:/data` 改成 `/path/on/host:/data`（注意目录权限：容器内以 UID 10001 运行）。

---

## 配置

服务端可配置项均通过环境变量（compose `environment:` 或 `docker run -e`）传入：

| 变量 | 说明 | 默认 |
|---|---|---|
| `PORT` | 容器内监听端口 | `8080` |
| `VIDEO_TO_SUMMARY_DB` | SQLite 库路径 | `/data/app.db` |
| `VIDEO_TO_SUMMARY_OUTPUT_DIR` | 产物目录 | `/output` |
| `VIDEO_TO_SUMMARY_TOKEN` | 可选鉴权 Token（暴露到公网前**必须**配置） | 空 |
| `VTS_STATIC_DIR` | 自带前端：指向含 `index.html` 的目录时，首页与 `/static/*` 由该目录提供（无效则回落包内前端） | 空 |
| `TZ` | 时区（镜像内置 tzdata） | `Asia/Shanghai` |
| `VTS_USER_AGENT` | 自定义 yt-dlp User-Agent：非空即作为 `user_agent` 传入并写入请求头 `http_headers["User-Agent"]`（B 站提取路径实测仅设 `user_agent` 不会真正替换请求头里的 UA，仍 412，两者都设才生效）；空 = yt-dlp 默认 UA（不改默认行为）。B 站 412 风控解法之一，见下文「B 站 412 风控」 | 空 |
| `VTS_COOKIES_FILE` | 登录 cookies 文件路径（等价 CLI `--cookies`；显式 cookies 文件优先于浏览器 cookies，互斥不并存；文件不存在会 WARN 并说明路径，不会静默当作已生效） | 空 |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | BYOK：任意 OpenAI 兼容端点的默认接入信息 | 空 |
| `OPENAI_API_KEY` / `ASR_BASE_URL` / `ASR_MODEL` | 转写接口（仅当视频没有字幕时需要） | 空 |
| `ASR_RESPONSE_FORMAT` | 转写响应格式：默认先请求 `verbose_json`（含分段/时间戳，可产出 `.srt` 字幕），若端点以 HTTP 400 且错误信息提及 `response_format` 拒绝则自动降级 `json`（仅文本，无 SRT）并打 WARNING；部分兼容端点只支持 `json`/`text`，可固定格式省去每次先失败一次。取值 `json`/`text`/`verbose_json`，非法值告警并回落默认 | 自动降级 |

> 上表均为**运行期**环境变量。构建期另有可选参数 `APT_MIRROR` / `PIP_INDEX_URL` /
> `NPM_REGISTRY`（仅影响镜像构建时拉取依赖的源，默认官方源），见「加速构建（可选）」。

### 配置 LLM（BYOK）

不配置也能用「字幕优先」路径（零 API 成本）。需要 LLM 摘要时，任选其一：

1. 编辑 `docker/docker-compose.yml`，取消注释并填写 `LLM_*` 变量后重启。
2. 或通过 Web 控制台界面配置（`设置 → LLM`），Key 以 Fernet 加密后存入 `/data` 的 SQLite 库，
   只返回掩码值。重启容器后配置仍在（数据在卷里）。

没有配置 Key 时任务不会失败：仍产出转写原文（`.txt`/`.srt`），跳过总结阶段；
补上 Key 后对任务点「重新生成」即可拿到完整笔记。

---

## B 站 412 风控

**根因（服务器实测）**：B 站边缘 WAF 按「UA × IP 信誉」对 `/video/` HTML 页发起挑战，
yt-dlp 默认（浏览器式）UA 必现 `HTTP Error 412: Precondition Failed`；换成非浏览器 UA
（实测 `Wget/1.21.3` 稳定通过）即恢复。Referer 等请求头无效；API 端点不受影响。

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
   （CLI / Web / Docker 三条路径都生效；仅设 `user_agent` 在 B 站提取路径上实测
   不生效——请求头仍是默认 UA，仍 412）；
   不设置时保持 yt-dlp 默认 UA——绝大多数站点用默认 UA 是正常的，**不要**全局
   默认换成非浏览器 UA。

2. **提供登录 cookies**：B 站 CC/AI 字幕需要登录态，而 Web/Docker 形态没有本机
   浏览器（「浏览器 cookies」不可用）。把 cookies.txt 挂进容器并设 `VTS_COOKIES_FILE`：

   ```yaml
   services:
     vts:
       volumes:
         - ./cookies.txt:/data/cookies.txt:ro
       environment:
         - VTS_COOKIES_FILE=/data/cookies.txt
   ```

   `VTS_COOKIES_FILE` 等价 CLI `--cookies`：显式 cookies 文件**优先于**浏览器
   cookies（复用同一互斥逻辑，绝不并存）；文件不存在时应用会 WARN 并说明路径，
   不会静默当作已生效。同一份配置同时解锁 B 站 CC/AI 字幕与 YouTube 自动字幕
   匿名限流（429）场景。

3. **配置代理**：在 Web 控制台「设置 → 网络与访问」填代理并保存（等价 CLI
   `--proxy`），任务默认配置会随任务带入。

### 临时自救：挂载 yt-dlp 配置文件

当前版本的 VTS 应用**不会**向 yt-dlp 传 `--ignoreconfig`，因此容器内 yt-dlp 会读取
`/home/vts/.config/yt-dlp/config`（用户 `vts` 的家目录，XDG 默认路径）。**无需改代码**
的临时解法是把该配置文件挂载进去——产品级一等入口仍是上面的 `VTS_USER_AGENT` /
`VTS_COOKIES_FILE`，这份配置文件是过渡期自救手段（升级后两种方式可并存，配置文件的
生效优先级由 yt-dlp 自己决定）。

把下面片段存为 `docker/docker-compose.override.yml`（与主 compose 同目录，
Compose 自动合并；也可用 `-f` 显式指定），再重新 `up -d` 即可：

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

## 升级

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
  pnpm 11**（仓库以 pnpm 11.8.0 开发；pnpm 11 要求 Node 22+，Node 20 下
  `pnpm install` 实测报 `ERR_UNKNOWN_BUILTIN_MODULE`）。步骤：
  `cd web-src && pnpm install --frozen-lockfile && pnpm build`。容器内构建由
  Dockerfile stage1（`node:22-slim`）自动完成，无需在宿主机装 Node。

- **不需要 Docker 时**：`python3 -m venv .venv && source .venv/bin/activate &&
  pip install -e . && bash scripts/web.sh`（见根 `README.md`「快速开始」）。

- 容器内以非 root 用户（UID 10001）运行，`/data` 与 `/output` 由 named volume 管理，
  无需在镜像内保留写权限。
