# 配置参考（configuration）

VTS 的配置参考：数据位置实现判据、版本号注入、环境变量全量语义。入口文档见仓库根
`README.md`；Docker 部署语境（卷映射、容器端口、镜像源）见 `docker/README.md`「配置」。

## 数据位置实现判据

SQLite 库路径解析（`db.py::resolve_database_path`，纯函数、可单测）按固定顺序：

1. **环境变量覆盖**：`VIDEO_TO_SUMMARY_DB` 非空 → 直接使用（Docker 镜像已默认
   `/data/app.db`）；
2. **源码检出判定**：包内 `db.py` 所在目录向上两级若同时存在 `pyproject.toml` 与
   `src/video_to_summary/`，视为源码检出 → `<检出根>/data/app.db`（源码自部署形态的
   既有数据位置，保持不变）；
3. **安装为包**（非 editable）→ 平台用户数据目录：
   - macOS：`~/Library/Application Support/VTS/`
   - Windows：`%LOCALAPPDATA%\VTS\`
   - Linux 等：`$XDG_DATA_HOME/vts/`（未设置 `XDG_DATA_HOME` 时为 `~/.local/share/vts/`）

目录缺失自动创建；创建失败会抛出提示设置 `VIDEO_TO_SUMMARY_DB` 的报错，不静默崩在
无权限上。解析结果经 `init_db()` 的 `sqlite database ready at ...` INFO 日志可见。
数据绝不写进 site-packages——venv 重建/升级即丢，系统 Python 无写权限时还会启动失败。

## 版本号注入（外部构建友好）

`get_version()` 的解析顺序：

1. 环境变量 `VIDEO_TO_SUMMARY_VERSION` **非空即返回**——外部构建用它注入自己的版本号，
   例如 `VIDEO_TO_SUMMARY_VERSION=1.2.3 python -m video_to_summary.main ...`；
2. 回落链：包目录 `_version.py` → `git describe` → 包元数据 → `"dev"`。

注入的版本号展示于 `/api/v1/health` 的 `version` 字段与 CLI `--version`。设计目的：
外部构建不必写 site-packages 里的 `_version.py`。

## 环境变量全量参考

以下变量**全部可选**；带注释的示例见 `.env.example`，Docker 部署语境见
`docker/README.md`「配置」。兜底关系与默认值以 `config.py` / `web/tasks.py` 为准。

| 变量 | 语义 |
|---|---|
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | 推理模型槽位（总结与文本润色共用）；也可在 Web 设置页配置。Key 兜底序：`OPENAI_API_KEY` → `LLM_API_KEY` |
| `OPENAI_API_KEY` | 转写 Key（无独立 ASR Key 变量）；未配置 `LLM_API_KEY` 时也作为推理 Key 兜底 |
| `ASR_BASE_URL` / `ASR_MODEL` | 转写端点与模型（视频没有字幕时；端点需实现 `/audio/transcriptions`） |
| `ASR_RESPONSE_FORMAT` | 转写响应格式（`json` / `text` / `verbose_json`）。默认先 `verbose_json`（可产出分段/SRT）；端点以 400+`response_format` 拒绝时自动降级 `json` 并打 WARNING；非法值告警后回落默认 |
| `SUMMARY_TEMPLATE` | 总结模板名的环境变量兜底（默认 `通用`；CLI `--summary-template` 显式传入优先） |
| `POLISH_TRANSCRIPT` | 文本润色全局开关（默认关；CLI `--polish-transcript` / `--no-polish-transcript` 显式传入优先） |
| `POLISH_MODEL` / `POLISH_BASE_URL` / `POLISH_PRESET` | 润色模型 / 端点 / 预设；端点缺省跟随推理端点（`LLM_BASE_URL`） |
| `VIDEO_TO_SUMMARY_DB` | SQLite 库路径覆盖（见上文「数据位置实现判据」） |
| `VIDEO_TO_SUMMARY_OUTPUT_DIR` | 任务产物基目录覆盖（Web 形态，默认 `output/`，实际产物在 `output/<job_id>/`） |
| `VIDEO_TO_SUMMARY_MAX_CONCURRENT` | Web 同时运行任务数上限（信号量，默认 2，最小 1） |
| `VIDEO_TO_SUMMARY_JOB_TIMEOUT` | 单任务超时秒数（默认 0 = 不限） |
| `VIDEO_TO_SUMMARY_TOKEN` | 可选 Bearer 鉴权：设置后所有 `/api/v1` 请求需带 `Authorization: Bearer <TOKEN>` 或 `X-Auth-Token`；暴露局域网/公网前**必须**配置（见 `docs/api.md`） |
| `VIDEO_TO_SUMMARY_VERSION` | 版本号注入（见上文） |
| `VTS_STATIC_DIR` | 自定义前端静态目录：指向**含 `index.html` 的目录**时首页与 `/static/*` 改由该目录提供；无效时回落包内目录并打一次 warning（见 `README.md`「进阶」） |
| `VTS_USER_AGENT` | 自定义 yt-dlp User-Agent：非空时同时注入 `user_agent` 与请求头 `User-Agent`（两者都设才对 B 站 412 生效）；空 = yt-dlp 默认 UA，不改默认行为。见 `docker/README.md`「网络与风控」 |
| `VTS_COOKIES_FILE` | 登录 cookies 文件（等价 CLI `--cookies`，显式文件优先于浏览器 cookies，两者互斥）；文件不存在时 WARN 并说明路径，不静默忽略 |
| `HOST` / `PORT` | `scripts/web.sh` 的监听地址与端口（默认 `127.0.0.1:8080`；`--port` 参数优先） |
| `VTS_NETWORK_TESTS` | 测试开关：置 1 后运行联网集成用例（默认跳过，不影响运行期行为） |
