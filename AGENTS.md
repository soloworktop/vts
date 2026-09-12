# VTS 仓库约定

视频 URL → 字幕/转写 → LLM 摘要 → Markdown 笔记。自部署、单用户、**BYOK**（自备任意
OpenAI 兼容端点）。本文件是**约定与铁律**，不是教程；用户文档见 `README.md`。

## 目录结构

```
src/video_to_summary/            import 包名（发行名是 vts，见 README「命名」）
├─ config.py constants.py        配置默认值 / 字符串常量命名空间
├─ pipeline.py subtitles.py      编排与字幕解析
├─ sources/                      URL（yt-dlp）+ 本地文件
├─ transcribers/                 转写引擎（唯一：OpenAI 兼容 Whisper API）
├─ summarizers/ polishers/       摘要与文本优化
├─ db.py crypto.py utils.py      存储 / 加密 / 公共工具
├─ log_export.py                 脱敏诊断日志
└─ web/                          FastAPI 服务
   ├─ app.py                    全部 HTTP 接口
   ├─ tasks.py                  任务状态机（并发/恢复/取消/重试/删除）
   ├─ hooks.py                  插件挂载点
   ├─ capabilities.py           ★ 能力声明（/api/v1/capabilities）
   ├─ llm_store.py settings_store.py template_store.py label_store.py
   ├─ fts_index.py history_io.py
   └─ static/                   React 前端构建产物（不入库，见 web-src/README.md）
tests/                          离线全绿为基线；e2e 在 tests/e2e/
scripts/                        run.sh（CLI）/ web.sh（Web）/ e2e.sh / fetch_ffmpeg.sh
docker/                          Docker 多阶段部署（Dockerfile / compose / README，见 docker/README.md）
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
7. **改接口先改代码，再同步 `README.md` 的接口表**——`tests/test_doc_consistency.py` 会强制双向一致。
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
```

- 用例隔离：DB 一律 monkeypatch 到 `tmp_path` 后 `db.reset()`，不得污染真实 `data/`
- 不触网：转录/LLM/yt-dlp 边界全部打桩
- **测试套件假定"0 插件"**：`tests/conftest.py` 的 session 级 `_no_plugins` fixture
  把 `web/hooks.py` 的插件发现替换为返回空——即使运行 venv 里装了声明
  `vts.plugins` 入口点的插件包，整套测试仍按干净构建（0 插件）执行，
  插件不得影响本仓测试结论。插件相关行为一律用
  `load_plugins(entries=...)` 显式注入测试（`tests/test_hooks.py`）
- 新增端点/模板/事件时，`README.md` 与对应 `AGENTS.md` 必须同步（有防漂移测试）

## 存储与数据位置

- SQLite 库路径解析（`db.py::resolve_database_path`，纯函数可单测）：
  1. `VIDEO_TO_SUMMARY_DB` 非空 → 直接用（Docker 已默认 `/data/app.db`）；
  2. 源码检出（`db.py` 向上两级含 `pyproject.toml` + `src/video_to_summary/`）→ `<检出根>/data/app.db`；
  3. 安装为包 → 平台用户数据目录（macOS `~/Library/Application Support/VTS/`、
     Windows `%LOCALAPPDATA%\VTS\`、其它 `$XDG_DATA_HOME/vts/` 或 `~/.local/share/vts/`）。
  目录缺失自动创建；创建失败报错提示设置 `VIDEO_TO_SUMMARY_DB`。解析结果经
  `init_db()` 的 `sqlite database ready at ...` INFO 日志可见。
- 版本号：`get_version()` 优先读 `VIDEO_TO_SUMMARY_VERSION`（外部构建可注入版本号，
  避免写 site-packages 的 `_version.py`），回落 `_version.py` → git describe → 包元数据 → `"dev"`。
