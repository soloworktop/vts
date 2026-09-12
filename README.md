# VTS

**视频 URL → 结构化 Markdown 笔记。** 输入一条视频链接，自动取字幕/转写、调用你自己配置的
LLM 生成结构化笔记，并落地为可编辑、可检索、可导出的 Markdown 文件。自带 Web 控制台与 CLI。

> **头条卖点：字幕优先。** 视频自带字幕（人工字幕或平台自动字幕）时，VTS 直接使用字幕文本
> 出笔记——**不下载音频、不调用任何转写接口、零 API 成本**，也完全不需要配置 API Key。
> 只有视频确实没有字幕时，才需要转写能力。

- **BYOK（Bring Your Own Key）**：不绑定任何模型供应商。任意 **OpenAI 兼容**端点都可以：
  OpenAI、DeepSeek、Moonshot、自建 vLLM/Ollama 网关……填 `base_url` / `api_key` / `model` 即可。
- **Web 控制台**：任务列表、实时进度、产物浏览与在线编辑、标签、全文检索、模板管理。
- **MIT 许可**：核心功能全部开源，无门控、无试用限制、无功能阉割。
- 状态：**M0/M1 已完成**——服务端开源化与 React 前端重写均已落地；当前处于发布准备
  （Docker 一键部署 / 双语文档）。

---

## Docker 一键自部署（推荐）

不需要本机装 Python / Node：一条命令构建并启动 Web 控制台（多阶段构建，镜像内已含
`ffmpeg`，运行期非 root 用户）：

```bash
docker compose -f docker/docker-compose.yml up -d --build
# 或
bash scripts/docker.sh up
```

- 打开 <http://127.0.0.1:8080>，健康检查 <http://127.0.0.1:8080/api/v1/health>
- 数据持久化：SQLite 库（含加密后的 Key）在 `vts_data` 卷（`/data`），任务产物在
  `vts_output` 卷（`/output`）——`down` 不丢数据
- 环境变量配置（`LLM_API_KEY` 等 BYOK 接入、`VIDEO_TO_SUMMARY_TOKEN` 可选鉴权、
  `TZ` 时区）与升级方式见 **`docker/README.md`**
- 构建上下文 `.dockerignore` 已排除 `node_modules`/`data`/`output*`/`.git`

---

## 数据位置

SQLite 数据库（`app.db`：任务历史、标签、模板、加密后的 LLM Key）的默认位置取决于运行形态：

- **源码检出 / editable 安装**（`pip install -e .`）：`<检出根>/data/app.db`
  （现有自部署用户的数据位置，保持不变）
- **安装为包**（`pip install vts` 等非 editable）：平台用户数据目录——
  - macOS：`~/Library/Application Support/VTS/app.db`
  - Windows：`%LOCALAPPDATA%\VTS\app.db`
  - Linux 等：`$XDG_DATA_HOME/vts/app.db`（未设置 `XDG_DATA_HOME` 时为 `~/.local/share/vts/app.db`）

判定依据：包内 `db.py` 所在目录向上两级存在 `pyproject.toml` 与 `src/video_to_summary/`
即视为源码检出，否则按安装形态落到平台用户目录——数据不会写进 site-packages
（venv 重建/升级即丢，系统 Python 无写权限时还会启动失败）。

任意形态都可用 `VIDEO_TO_SUMMARY_DB` 覆盖（Docker 已默认设为 `/data/app.db`）。
目录不存在时自动创建；创建失败会抛出提示设置 `VIDEO_TO_SUMMARY_DB` 的报错，
不会静默崩在无权限上。解析结果在启动日志的 `sqlite database ready at ...` 中可见。

**版本号覆盖（外部构建友好）**：`get_version()` 优先读取环境变量 `VIDEO_TO_SUMMARY_VERSION`
（非空即返回，例如 `VIDEO_TO_SUMMARY_VERSION=1.2.3 python -m video_to_summary.main ...`），
外部构建可通过该环境变量注入自己的版本号（展示于 `/api/v1/health` 的 `version` 与 CLI `--version`），
避免写入包目录下的 `_version.py`。

---

## 自带前端（自定义静态目录）

默认情况下，Web 控制台直接服务包内构建好的前端（`src/video_to_summary/web/static/`，
由 `web-src/` 的构建产物生成）。如果你希望用**自己构建的前端产物**替换它（例如按
自己的偏好重新构建界面，或把前端与后端合并部署到同一个服务进程），可以通过环境变量
`VTS_STATIC_DIR` 指到一个自定义静态目录：

```bash
VTS_STATIC_DIR=/path/to/my-frontend bash scripts/web.sh
# Docker：在 compose 的 environment 里设置，并把宿主目录挂载进容器
#   environment: { VTS_STATIC_DIR: /frontend }
```

语义：

- **生效**：`VTS_STATIC_DIR` 指向一个**包含 `index.html` 的目录**时，首页 `/` 与
  `/static/*` 都改由该目录提供，缓存语义不变（`Cache-Control: no-cache` + ETag/304）。
  请把完整的构建产物（`index.html` + `assets/` 等）放进该目录。
- **回落**：未设置，或设置的值无效（目录不存在 / 不是目录 / 缺 `index.html`）时，
  自动回落到包内 `web/static/`，行为与不设置完全一致，并在服务日志里打一条
  warning 提示——配错路径不会白屏。
- **不受影响**：`/api/v1` 接口、`/guide` 使用手册、健康检查等仍由后端提供，
  与静态目录无关。

---

## 快速开始

需要 **Python 3.10+**。下载视频需要系统里有 `ffmpeg`（`bash scripts/fetch_ffmpeg.sh` 可自动获取）。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
bash scripts/web.sh                      # 打开 http://127.0.0.1:8080
```

命令行用法：

```bash
python -m video_to_summary.main "<视频链接>"                        # 字幕优先，零 API 成本
python -m video_to_summary.main "<视频链接>" --summary-template 学术笔记
python -m video_to_summary.main "<视频链接>" \
    --llm-key sk-xxx --llm-base-url https://api.deepseek.com/v1 --llm-model deepseek-chat
bash scripts/run.sh "<视频链接>"                                     # 一键（自动建 venv + 装依赖）
```

或安装后直接用命令 `vts "<视频链接>"`。

### 配置 LLM（可选——不配也能出转写文本）

把配置写进仓库根的 `.env`（可从 `.env.example` 复制）：

```ini
LLM_API_KEY=sk-xxx
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
```

或走 HTTP 接口（Key 以 Fernet 加密落库，接口只回掩码值）。LLM 配置只有两个槽位：
**推理模型**（总结与文本润色共用）与**语音识别模型**（视频没有字幕时转写音频）：

```bash
curl -X PUT localhost:8080/api/v1/llm -H 'Content-Type: application/json' \
  -d '{"summary":{"base_url":"https://api.deepseek.com/v1","api_key":"sk-xxx","model":"deepseek-chat"},
       "asr":{"base_url":"https://api.openai.com/v1","api_key":"sk-xxx","model":"whisper-1"}}'
```

**没有配置 Key 时任务不会失败**：仍产出转写原文（`.txt` / `.srt`），跳过总结阶段并在处理
过程中标注 `summarize_skipped`；补上 Key 后点「重新生成」即可拿到完整笔记。

### 视频没有字幕时

需要 Whisper 兼容的转写接口，二选一：

```ini
OPENAI_API_KEY=sk-xxx          # 直接用 OpenAI 的 whisper-1
ASR_BASE_URL=https://your-gateway/v1   # 或任意实现 /audio/transcriptions 的兼容端点
ASR_MODEL=whisper-1
```

### 需要登录的内容

B 站 AI/CC 字幕、YouTube 自动字幕与会员内容通常需要登录态。本版不内置站点登录集成，两种方式：

1. `--cookies cookies.txt`（CLI）或环境变量 `VTS_COOKIES_FILE`；
2. Web「设置 → 网络与访问」选择「浏览器 cookies」——直接读取本机浏览器的登录态
   （Chrome 系首次读取会弹 macOS 钥匙串授权；Docker 内不可用，请用方式 1）。

两者互斥，显式 cookies 文件优先。

---

## 特性

| 能力 | 说明 |
|---|---|
| 字幕优先 | `auto`（默认）/ `manual_only` / `off`，语言可指定（`auto` 时中文优先） |
| 转写 | OpenAI 兼容 Whisper API |
| LLM 摘要 | OpenAI 兼容 + 10 种内置模板（通用 / 精简笔记 / 详细笔记 / 教程笔记 / 学术笔记 / 会议纪要 / 商业分析 / 小红书笔记 / 生活随笔 / 任务清单）+ 自定义模板 CRUD |
| 任务管理 | 创建 / 列表 / 详情 / 实时进度 / 取消 / 重试（可换模板）/ 删除 / 重启恢复 / 事件回放 |
| 历史检索 | 标签体系（重命名/合并/删除）+ SQLite FTS5 全文检索（正文 + 元数据，命中高亮） |
| 产物 | `.summary.md`（可在线编辑，写回原子替换）+ `.txt` / `.srt` / `.segments.json` |
| 导出与迁移 | 单任务导出 Markdown（真实 attachment）；历史任务整体导出/导入 zip |
| 诊断 | `GET /api/v1/logs/export` 导出脱敏诊断日志（API Key / Bearer / cookie / 家目录伪名化） |
| 安全 | API Key Fernet 加密落库；产物路径防逃逸；可选 Bearer Token 鉴权；静态资源 no-cache |

### 能力协商

`GET /api/v1/capabilities` 返回插件声明的可选能力集合；本构建不含任何插件，响应为 `{}`。
安装了插件的部署中，前端据此渲染插件提供的可选功能。

---

## HTTP API

canonical 前缀为 **`/api/v1`**；过渡别名 `/api/*` 已随 M1（React 前端切换）删除，
所有客户端一律走 `/api/v1`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/health` | 服务状态、版本、LLM 是否已配置、DB schema 提示 |
| GET | `/api/v1/capabilities` | 能力集合（见上） |
| POST | `/api/v1/jobs` | 创建任务 |
| GET | `/api/v1/jobs` | 列表（`label` / `unlabeled` / `limit` / `offset` / `q` 全文检索） |
| GET | `/api/v1/jobs/export` | 导出历史数据 zip |
| POST | `/api/v1/jobs/import` | 导入历史数据 zip |
| GET | `/api/v1/jobs/{job_id}` | 任务详情（含完整进度事件链） |
| GET | `/api/v1/jobs/{job_id}/progress` | 进度事件 |
| POST | `/api/v1/jobs/{job_id}/cancel` | 取消 |
| POST | `/api/v1/jobs/{job_id}/retry` | 重试（可换模板） |
| DELETE | `/api/v1/jobs/{job_id}` | 删除（仅终态） |
| GET | `/api/v1/jobs/{job_id}/result` | 产物路径 |
| GET | `/api/v1/jobs/{job_id}/file` | 读取单个产物内容 |
| GET, HEAD | `/api/v1/jobs/{job_id}/export` | 导出单个产物（`format=md`，真实 attachment） |
| PUT | `/api/v1/jobs/{job_id}/summary` | 保存编辑后的总结（覆盖写回产物） |
| PUT | `/api/v1/jobs/{job_id}/labels` | 设置任务标签 |
| GET | `/api/v1/settings` | 任务默认配置 |
| PUT | `/api/v1/settings` | 修改任务默认配置 |
| GET | `/api/v1/templates` | 模板列表（含内置只读名单） |
| GET | `/api/v1/templates/{template_name}` | 单个模板 |
| PUT | `/api/v1/templates/{template_name}` | 保存自定义模板 |
| DELETE | `/api/v1/templates/{template_name}` | 删除自定义模板（内置只读） |
| GET | `/api/v1/llm` | LLM 配置（推理模型 / 语音识别模型两槽位，Key 掩码） |
| PUT | `/api/v1/llm` | 更新 LLM 槽位配置（summary / asr） |
| POST | `/api/v1/llm/import` | 从 `.env` 导入 LLM 配置 |
| GET | `/api/v1/labels` | 标签列表 |
| POST | `/api/v1/labels` | 新建标签 |
| PUT | `/api/v1/labels/{label_id}` | 重命名标签 |
| DELETE | `/api/v1/labels/{label_id}` | 删除标签 |
| POST | `/api/v1/labels/merge` | 合并标签 |
| POST | `/api/v1/cookies/test` | 预检浏览器 cookies 可读性（含 YouTube / B 站登录态提示） |
| GET | `/api/v1/fs/browse` | 浏览本机目录（本地文件源） |
| POST | `/api/v1/fs/pick` | 系统原生文件选择框（macOS / Windows） |
| GET | `/api/v1/logs/export` | 导出脱敏诊断日志 |

设置 `VIDEO_TO_SUMMARY_TOKEN` 后，所有 `/api/*` 请求需带 `Authorization: Bearer <TOKEN>`
或 `X-Auth-Token`。暴露到局域网/公网前**必须**配置。

---

## 命名

发行名与项目名是 **VTS**，但 Python import 包名保持 **`video_to_summary`**：

```python
from video_to_summary import Settings, run
```

先有包名、后定项目名，重命名会打断所有既有用法与下游依赖，收益不抵成本。安装时用发行名：

```bash
pip install vts          # 或本地开发 pip install -e .
```

---

## 插件挂载点

核心只定义挂载点，第三方插件通过标准 entry point 挂载，**核心代码不引用任何具体插件包名**：

```toml
[project.entry-points."vts.plugins"]
my-plugin = "my_plugin:plugin"
```

插件实现 `register(hooks)`，可注册三类挂载点：`RouteProvider`（额外路由）、
`JobSideEffect`（任务完成副作用）、`SettingsProvider`（额外设置项），并可用
`hooks.declare_capabilities({...})` 声明可选能力（`/api/v1/capabilities` 返回各插件
声明能力的并集）。

判据是 **fail-closed** 的：查到 0 个插件 = 本构建的预期状态，产品按完整的 BYOK 形态运行；
查到条目但加载失败 → **抛致命错误拒绝启动**，绝不静默降级。

---

## 开发

```bash
pip install -e ".[dev]"
python -m pytest -q                    # 默认全离线；network 用例需 VTS_NETWORK_TESTS=1
pip install -e ".[e2e]" && playwright install chromium
bash scripts/e2e.sh                    # 端到端（真实 uvicorn + 进程内 fakes）
```

仓库约定见 `AGENTS.md`；提交范围与流程见 `CONTRIBUTING.md`。

---

## 常见问题（自部署 FAQ）

### B 站视频报 412 / 取不到字幕怎么办？

**412（`HTTP Error 412: Precondition Failed`）** 是 B 站边缘 WAF 按「UA × IP 信誉」对
`/video/` HTML 页发起的挑战：yt-dlp 默认（浏览器式）UA 会被拦截，换成非浏览器 UA 即恢复。
**取不到字幕**通常是 B 站 CC/AI 字幕需要登录态，而 Web/Docker 形态没有本机浏览器
（「浏览器 cookies」不可用）。Docker 部署的完整 compose 示例见
**`docker/README.md`「B 站 412 风控」**，核心解法如下（任选其一）：

1. **自定义 User-Agent**：设置环境变量 `VTS_USER_AGENT=Wget/1.21.3` 后重试
   （CLI / Web / Docker 三条路径均生效；不设置时保持 yt-dlp 默认 UA，不影响其它站点）；
2. **提供登录 cookies**：设置环境变量 `VTS_COOKIES_FILE` 指向 cookies.txt
   （等价 CLI `--cookies`，显式文件优先于浏览器 cookies；同时解锁 B 站 CC/AI 字幕）；
3. **配置代理**：Web「设置 → 网络与访问」填代理并保存，或 CLI 传 `--proxy`。

---

## 免责声明

本工具仅用于个人学习与研究。请遵守目标平台的服务协议与版权规定：不要用它下载或传播
你无权使用的内容。项目不包含任何 DRM/付费墙绕过能力，也不内置任何登录凭据。

## 许可

[MIT](LICENSE) © 2026 VTS contributors
