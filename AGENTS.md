# VTS 仓库约定

视频 URL → 字幕/转写 → LLM 摘要 → Markdown 笔记。自部署、单用户、**BYOK**（自备任意
OpenAI 兼容端点）。本文件是**约定与铁律**，不是教程；用户文档见 `README.md`。

## 目录结构

```
src/video_to_summary/            import 包名（发行名是 vts，见 README「命名」）
├─ main.py                       CLI 入口（`vts` script 指向 main:main）
├─ config.py constants.py        配置默认值 / 字符串常量命名空间
├─ pipeline.py subtitles.py      编排与字幕解析
├─ cancel_utils.py version.py    阻塞调用中途取消 / 版本解析
├─ sources/                      URL（yt-dlp）+ 本地文件
├─ transcribers/                 转写引擎（唯一：OpenAI 兼容 Whisper API）
├─ summarizers/ polishers/       摘要与文本优化
├─ db.py crypto.py utils.py      存储 / 加密 / 公共工具
├─ downloader.py log_export.py   时长探测 / 脱敏诊断日志
└─ web/                          FastAPI 服务
   ├─ app.py                    全部 HTTP 接口
   ├─ tasks.py                  任务状态机（并发/恢复/取消/重试/删除）
   ├─ hooks.py                  插件挂载点
   ├─ capabilities.py           ★ 能力声明（/api/v1/capabilities）
   ├─ llm_store.py settings_store.py template_store.py label_store.py
   ├─ fts_index.py history_io.py
   └─ static/                   React 前端构建产物（不入库，见 web-src/README.md）
web-src/                         React 前端源码（React + TS + Vite，pnpm；改前读 web-src/AGENTS.md）
tests/                          离线全绿为基线；e2e 在 tests/e2e/
scripts/                        run.sh（CLI）/ web.sh（Web）/ e2e.sh / docker.sh / fetch_ffmpeg.sh
                                + banned_words_lint.py（禁词门禁）/ snapshot_openapi.py（接口快照）
docker/                          Docker 多阶段部署（Dockerfile / compose / README，见 docker/README.md）
examples/                        用法示例（基础 / 本地音频 / 自定义后端等）
.github/workflows/oss-guard.yml  CI：构建前端 → pytest → 禁词 lint
.github/workflows/release.yml    发布：tag 触发 → 打包 wheel/sdist（先构建前端 + 注入 tag 版本）+ wheel 冒烟 → GitHub Release + ghcr 镜像
```

## 铁律

1. **不散落魔数**：状态/事件/用途用 `constants.py`；默认值与默认 LLM 槽位用 `config.py`。
2. **不做门控**：核心永不含授权校验、功能开关、试用限制、用量上报。无插件时必须是
   一个功能完整的 BYOK 产品。
3. **插件只能经 `entry_points(group="vts.plugins")` 挂载**，核心代码**不得出现任何具体
   插件包名**（硬耦合写法会被 `scripts/banned_words_lint.py` 禁词门禁拦下；扫描范围是
   仓库内容，gitignored 的 `.env` 不参与、`.env.example` 参与；新增扫描模式时同步登记
   豁免，豁免登记见 `scripts/oss_leak_allowlist.txt`；fail-closed：条目存在但加载失败
   = 拒绝启动，见 `web/hooks.py`）。
4. **`/api/v1` 是 canonical**：所有端点只注册 `/api/v1/...`。过渡别名 `/api/*`
   已随 M1（React 前端切换）删除，客户端一律走 `/api/v1`。
5. **不破坏体验语义**：字幕两段式提取（先取元信息 → 只拉最优单文件）、cookies 互斥
   （`cookiefile` 与 `cookiesfrombrowser` 不同时设置）、标题回填守卫（用户显式标题永不覆盖）、
   **无 Key 降级**（仍出 `.txt`/`.srt` + `summarize_skipped` 事件 + 配置引导，不允许静默缺产物）。
6. **密钥不落盘明文**：API Key 一律经 `crypto.py`（Fernet）加密入库，接口只回掩码值；
   `.env` / `data/` / `output*/` 一律 gitignore。
7. **改接口先改代码，再同步 `docs/api.md` 的接口表**（`README.md`「参考」只留指引）——
   `tests/test_doc_consistency.py` 锚点聚合读 `README.md` + `docs/api.md` 强制双向一致；
   `tests/test_openapi_contract.py` 用快照锁住路径集合。
8. **静态目录覆盖点**：环境变量 `VTS_STATIC_DIR` 指向**含 `index.html` 的目录**时，
   首页 `/` 与 `/static/*` 由该目录提供；未设置或无效（不存在/不是目录/缺 index.html）
   时回落包内 `web/static/` 并打一次 warning（实现见 `web/app.py::_static_dir`）。
   `_guide_html_path()` 与 `/api/v1` 不受影响。
9. **数据路径不走捷径**：SQLite 路径解析一律经 `db.py::resolve_database_path`
   （`VIDEO_TO_SUMMARY_DB` 覆盖 → 源码检出布局 → 平台用户数据目录），
   不得在模块顶层散落平台判断或 CWD 相对路径。
10. **下载访问通道一等入口（环境变量）**：`VTS_USER_AGENT`（非空 → yt-dlp
    `user_agent` + `http_headers["User-Agent"]` 同时注入——B 站提取路径实测仅设
    `user_agent` 不会替换请求头里的 UA，仍 412，两者都设才生效，合并不覆盖既有
    `http_headers`，见 `sources/url._resolve_user_agent`；空/未设置 = yt-dlp 默认 UA，
    **绝不改默认行为**，只按需设置）与
    `VTS_COOKIES_FILE`（非空且文件存在 → yt-dlp `cookiefile`，等价 CLI `--cookies`；
    与浏览器 cookies 互斥、显式文件优先——复用 `sources/url._resolve_cookie_opts`
    语义，不另写一套；文件不存在必须 WARN 并说明路径，不静默忽略）。两者都经
    `config.Settings` 在 CLI（`main.py`）与 Web（`web/tasks._build_source`）两条路径
    生效。站点风控错误文案（412/429，`web/tasks._user_facing_error`）只引导
    UA / cookies / 代理三个解法，不暴露上游内部报文。

## 测试

```bash
python -m pytest -q                      # 必须全绿（默认全离线）
VTS_NETWORK_TESTS=1 python -m pytest -q  # 含联网集成用例
bash scripts/e2e.sh                      # 端到端（真实 uvicorn + fakes；浏览器层需 playwright）
VTS_LIVE_E2E=1 bash scripts/e2e.sh live  # 真实边界端到端（真实下载/转写/LLM，读 .env；默认跳过，CI 不跑）
```

- 用例隔离：DB 一律 monkeypatch 到 `tmp_path` 后 `db.reset()`，不得污染真实 `data/`
- 不触网：转录/LLM/yt-dlp 边界全部打桩
- **测试套件假定"0 插件"**：`tests/conftest.py` 的 session 级 `_no_plugins` fixture
  把 `web/hooks.py` 的插件发现替换为返回空——即使运行 venv 里装了声明
  `vts.plugins` 入口点的插件包，整套测试仍按干净构建（0 插件）执行，
  插件不得影响本仓测试结论。插件相关行为一律用
  `load_plugins(entries=...)` 显式注入测试（`tests/test_hooks.py`）
- **接口契约快照**：`tests/test_openapi_contract.py` 断言 OpenAPI 路径集合与
  `tests/openapi_paths.json` 完全一致（路径参数归一；别名/幽灵路由残留会被拦下）。
  有意的接口增删改后更新快照并人工 review diff：
  `VTS_UPDATE_OPENAPI_SNAPSHOT=1 python -m pytest tests/test_openapi_contract.py`
  或 `python scripts/snapshot_openapi.py`
- 新增端点/模板/事件时，`docs/api.md`（接口表）/ `README.md`（模板清单）与对应
  `AGENTS.md` 必须同步（有防漂移测试）

## 存储与数据位置

- SQLite 库路径解析（`db.py::resolve_database_path`，纯函数可单测）：
  1. `VIDEO_TO_SUMMARY_DB` 非空 → 直接用（Docker 已默认 `/data/app.db`）；
  2. 源码检出（`db.py` 向上两级含 `pyproject.toml` + `src/video_to_summary/`）→ `<检出根>/data/app.db`；
  3. 安装为包 → 平台用户数据目录（macOS `~/Library/Application Support/VTS/`、
     Windows `%LOCALAPPDATA%\VTS\`、其它 `$XDG_DATA_HOME/vts/` 或 `~/.local/share/vts/`）。
  目录缺失自动创建；创建失败报错提示设置 `VIDEO_TO_SUMMARY_DB`。解析结果经
  `init_db()` 的 `sqlite database ready at ...` INFO 日志可见。
- 表：`meta`/`llm_profiles`/`llm_routing`/`summary_templates`/`jobs`/`labels`/`job_labels`/
  `jobs_fts`（v8 全文索引，存产物**派生文本**、可由 `web/fts_index.py` 随时重建）；
  当前 `SCHEMA_VERSION = 9`
- **产物**（不入库）：`output/<job_id>/`（Web；`VIDEO_TO_SUMMARY_OUTPUT_DIR` 可覆盖
  基目录）或 `output/`（CLI），含 `.txt`/`.segments.json`/`.srt`/`.polished.txt`
  （启用文本优化时）`/.summary.md`；pipeline 按「文件存在即命中缓存」读写，DB 只存路径
- **上传源文件**（不入库）：浏览器直传的本地文件（`POST /api/v1/jobs/upload`）存
  `<产物基目录>/uploads/<uuid>/<安全文件名>`（`VIDEO_TO_SUMMARY_UPLOAD_DIR` 可覆盖；
  **不得**放进 `output/<job_id>/`——retry 清空产物目录会误删源）。归服务端托管：
  `delete_job` 检测 `audio_path` 在 uploads 内则连带回收；启动时
  `sweep_orphan_uploads()` 清扫无任务引用的残留目录
- **加密**：API Key 明文不落盘；`api_key` 存 Fernet 密文（`enc:v1:` 前缀），密钥独立
  `enc_key` 文件；数据库文件启动时 chmod 0o600
- 版本号：`get_version()` 优先读 `VIDEO_TO_SUMMARY_VERSION`（外部构建可注入版本号，
  避免写 site-packages 的 `_version.py`），回落 `_version.py` → git describe → 包元数据 → `"dev"`。

## 关键约定与坑（改代码前必读）

- **Web 单进程（single-process / single-worker）**：任务调度（`web/tasks.py` 的进程内
  `_jobs`/`_cancel_flags`/线程池/信号量）与 `resume_pending_jobs()` 启动恢复都要求单一
  进程；数据目录锁文件（`acquire_scheduler_lock`，flock/msvcrt，随进程退出自动释放）
  在启动期以明确错误拒绝第二个进程。`uvicorn --workers>1`、多副本容器、共用同一
  `VIDEO_TO_SUMMARY_DB` 目录的多实例都是不受支持的配置（`scripts/web.sh` 与 docker
  CMD 默认均无 `--workers`）。
- **Job 状态写入必须走 `transition_job`**：`UPDATE jobs SET status=? WHERE job_id=?
  AND status=expected` 按 affected rows 判胜负——cancel/retry/worker 收尾在状态竞争下
  绝不互相覆盖（输家放弃写入或重读 DB 分支）。禁止用「读 status → Python 判断 → 盲写
  `Job.save()`」推进状态；`save()` 仅用于非状态字段的普通落库。
- **迁移**：`db._apply_migrations` 执行后**必须无条件写回 schema_version**（否则新库会
  重复 ALTER）；唯一例外：数据库 schema **高于**本 App 时不回写（防旧版把版本号降级，
  升级回跳时新版重跑已执行的迁移），经 `db_newer_version()` 暴露给 `/api/v1/health`。
- **enqueue_job** 需运行中的 asyncio 循环（`asyncio.create_task`，API 上下文中调用），
  持有强引用防 GC；测试里要 patch。
- **产物一律原子写**：pipeline 统一 `_atomic_write_text`——「文件存在即命中缓存」语义下
  半写的文件会被静默当作有效产物，绕过原子写会制造脏缓存。
- **旧 JSON 迁移路径锚定**：`LEGACY_LLM_FILE`/`LEGACY_TEMPLATE_FILE` 默认 `None` 哨兵 =
  运行时按数据库目录推导；绝不能改回 CWD 相对路径（曾导致换目录启动时静默继承工作
  目录下含明文 Key 的同名文件）。导入 Key 一律先 `encrypt_secret`。
- **打包 glob 不递归**：`pyproject.toml` 的 `package-data` 里 `web/static/*` 只匹配顶层
  文件，`assets/` 子目录必须显式列出（`web/static/assets/*`）——漏了会导致非 editable
  安装（如 Docker）下 `/static/assets/*` 404 → 首页白屏；`tests/test_packaging.py`
  有静态一致性门禁，新增 `web/static` 子目录时同步模式。
- **字幕提取的两个门控**：① yt-dlp 仅在 `writesubtitles`/`writeautomaticsub`（或
  `listsubtitles`）开启时才在 extract_info 里返回字幕清单，漏传则清单恒空、所有视频
  "no usable subtitle"；② B 站轨道内容内联在 `data` 字段（无 `url`），`_pick_track`
  两种形态都要接受。勿改回 `subtitleslangs` 通配批量下载（YouTube timedtext 对连发
  请求 429）。
- **Windows 子进程必带无窗口标志**：GUI 形态 spawn 任何控制台程序（ffmpeg/yt-dlp 内部
  音频提取的 ffmpeg——库本身不带创建标志）都会弹黑色控制台窗口。包内
  `subprocess.run/Popen` 一律展开 `utils.subprocess_no_window_kwargs()`；
  `install_windows_subprocess_guard()` 给全部 `Popen` 兜底注入 `CREATE_NO_WINDOW`
  （幂等、OR 合并不覆盖显式标志、非 Windows 直通）；`tests/test_windows_no_console.py`
  防回归。
- **`[dev]` extra 必含 `requests`**：tests/e2e 顶层 import requests，缺了
  `python -m pytest -q`（testpaths 含 tests/e2e）在**收集期**即 ModuleNotFoundError。

## Git 工作流

- 一个小提交只做一件事；提交信息用中文，格式 `type: 说明`
  （`feat`/`fix`/`refactor`/`docs`/`test`/`chore`），详见 `CONTRIBUTING.md`
- `git add -A` 前检查暂存内容：`.env` / `data/` / `output*/` / `*.db` 不得入库
  （`.gitignore` 已排除，仍需过目）

## 安全与合规

- 真实 API Key 只放 `.env`（gitignore）；Web 配置的 Key 加密入库，接口只回掩码（铁律 6）
- 对外暴露前可选设置 `VIDEO_TO_SUMMARY_TOKEN` Bearer 鉴权；CORS 要求写方法带 Origin
  时与 Host 同源；产物读取有路径逃逸防护（详见 README「HTTP API」）
- 需要登录态的站点内容由用户自行提供 cookies（CLI `--cookies` / 浏览器 cookies /
  `VTS_COOKIES_FILE`），仅限个人使用，遵守平台协议
