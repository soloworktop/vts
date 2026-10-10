# VTS 桌面客户端

桌面客户端复用 React 控制台、FastAPI 任务层和现有 Python 处理链。CLI、Web 和 Docker
仍有独立入口。首发目标是 **Windows 11 x64** 与 **macOS 14+ Apple Silicon**；Linux
桌面、Intel Mac、自动更新、托盘和后台常驻暂未提供。

## 安装与使用

在 [GitHub Releases](https://github.com/soloworktop/vts/releases) 中选择带桌面附件的版本：

- Windows：`vts-<tag>-windows-x64-setup.exe`，用户级安装到用户 Programs 目录，
  无需管理员权限。另需 Microsoft **WebView2 Evergreen Runtime**，安装 Edge 浏览器
  不代表一定已有该 Runtime；缺失时启动提示提供 Microsoft 安装地址。
- macOS：`vts-<tag>-macos-arm64.dmg`，挂载后把 `VTS.app` 拖到 Applications。
- Python、前端、FFmpeg/ffprobe 已随桌面包提供，用户不必安装 Python、Node 或系统 FFmpeg。

首版没有 Authenticode / Developer ID 发布者签名，也没有公证；macOS 的 ad-hoc 签名
只满足本机运行要求。Windows 可能显示 SmartScreen 的未知发布者提示；macOS 可能需要
在「系统设置 → 隐私与安全性」明确批准首次打开。先核对下载来源与 `SHA256SUMS`；
本指南不要求关闭系统安全保护。源码构建通过不等于首次下载的系统拦截验证已通过。

第一次启动，在「设置 → LLM 配置」分别填写推理槽和语音识别槽的 OpenAI 兼容
Base URL、API Key 和服务端模型名。润色复用推理槽，不自动下载模型、不假设端点支持
模型发现。首次模型 Key 为空；桌面不会继承启动 shell 的 `SUMMARY_` / `ASR_` / `LLM_` /
`OPENAI_` / `POLISH_` 设置，也不会查找源码目录的 `.env`。

有需要时，可把已有 `.env` 放到桌面数据库所在目录，再显式点击「从 .env 导入」；
Key 加密入库后应妥善移走该明文迁移文件。历史 ZIP 只迁移终态任务、产物、标签和
自定义模板，**不包含 Key 或全局配置**。

「新建总结」支持 URL 与原生文件选择器选择本地音频、视频。原文件不会修改或删除；
桌面在任务输出目录准备 MP3/WAV。无音轨、文件不可读、转换失败时会给出处理提示。
ASR 是一次文件请求，大小和时长限制取决于端点，没有自动分片或本地 Whisper。

有字幕且未配置 Key 时仍可得到文本；摘要 Markdown 会说明未生成摘要并引导配置。
只有存在时间轴才提供 SRT，只有真实生成的润色稿才显示润色产物。结果支持查看、复制、
原生保存、用默认程序打开产物和打开任务输出目录。高级设置提供字幕策略/语言、音频格式、
润色预设、代理、User-Agent、cookies 文件或浏览器 cookies；显式 cookies 文件优先。

## 退出、恢复与数据

窗口关闭先处理未保存 Markdown 编辑稿（保存 / 丢弃 / 取消），再确认未完成任务。
确认退出后停止接受和启动任务，运行任务最多等待 15 秒收尾；超时回收后端进程树。
未完成记录下次启动恢复，已请求取消的任务不会被复活。后端意外退出时提示用户重启，
不会自动重启并再次发起处理请求。

恢复复用已经原子写入的缓存；**重试会清空产物并重新生成**，编辑稿可能丢失，界面会
提醒。突然断电后的进度可能落后于显示，但已有完整缓存可供恢复使用。

默认数据与应用安装目录分离：

| 平台 | 默认数据目录 |
|---|---|
| Windows | `%LOCALAPPDATA%\VTS\desktop\` |
| macOS | `~/Library/Application Support/VTS/desktop/` |

其中包含 `app.db`、独立 `enc_key`、`output/`、`logs/` 和 `webview/`。数据库和密钥必须
一起备份，丢失 `enc_key` 后无法解密 Key。**关闭应用后备份整个目录**；本地任务的原始
媒体仍需另外备份。卸载默认保留此目录；升级替换程序后继续使用数据。不要让两个实例
共用同一目录，运行锁会拒绝第二个调度器。

`VIDEO_TO_SUMMARY_DB`、`VIDEO_TO_SUMMARY_OUTPUT_DIR`、`VIDEO_TO_SUMMARY_UPLOAD_DIR`
仍可显式覆盖；相对覆盖路径在启动时解析为绝对路径。桌面工作目录固定为数据库目录。
源码运行同样使用桌面用户目录，不自动写进源码的 `data/`。下载通道环境变量
`VTS_USER_AGENT` / `VTS_COOKIES_FILE` 保留原语义。

## 本地开发与打包

只在对应原生平台构建，使用 CPython **3.12**、Node **22+**、pnpm **11.8.0**。
以下以 macOS 为例；Windows 将 `macos` 改为 `windows`，venv Python 位于 `Scripts/`：

```bash
python3.12 -m venv .venv
.venv/bin/python scripts/install_desktop_deps.py macos
cd web-src
pnpm install --frozen-lockfile
pnpm build
cd ..
.venv/bin/python -m video_to_summary.desktop
.venv/bin/python scripts/build_desktop.py --installer
.venv/bin/python scripts/desktop_smoke.py desktop-dist/VTS.app --gui
```

Windows 构建需 MSYS2 的 MINGW64 gcc、pkgconf、make、tar、nasm，以及 Inno Setup 6。
本地默认 MSYS2 为 `C:/msys64`，可用 `VTS_MSYS2_ROOT` 指定安装根目录；CI 使用 Action
返回的实际路径。
macOS 构建需 Xcode Command Line Tools。正式包使用干净检出和已有的同源码 Tag：
`python scripts/build_desktop.py --tag vMAJOR.MINOR.PATCH --installer`。
开发构建带 SCM 开发版本；不应作为正式 Release 发布。默认使用平台图标。

冻结为 onedir/windowed，资源只来自显式清单；安装器只携带应用目录。桌面二进制不加入
wheel 或 Docker。构建目录为 `build/desktop/`，应用及安装器为 `desktop-dist/`，
源文件与编译缓存为 `.desktop-cache/`，全部忽略入库。

依赖源清单位于 `packaging/desktop/binaries.json`；两个 Python 锁文件都包含真实哈希。
安装脚本先从同一哈希锁安装构建后端，再关闭隐式隔离构建并安装全部锁依赖。
锁更新需在两个目标平台重新验证，不手写猜测哈希。

### FFmpeg 来源与许可

没有采用来源不完整或启用 nonfree 的滚动预编译包。构建从固定发布源与真实 SHA256
编译 FFmpeg 9.0.2、LAME 3.100、zlib 1.3.2，静态链接必要库，并使用 macOS Secure
Transport / Windows Schannel 提供 HTTPS/HLS。关闭外部库自动探测，不启用 GPL/nonfree。
下载先验哈希，再检查版本、架构、HTTPS 协议和系统动态库闭包。

`build_ffmpeg.sh` 是完整配方；随包 notices 包含源 URL、源哈希、配方、编译信息、
实际二进制 SHA256、Python 依赖清单和许可证。FFmpeg 是独立可替换的可执行文件。
对缓存也重新校验二进制哈希及来源，错误缓存会阻断构建。缺少许可、来源或 Python
Runtime 许可证同样阻断。现有 `fetch_ffmpeg.sh` 仍用于原有开发用途，不用于 Release。

## 版本与发布

正式版本唯一来源是 Git Tag：`vMAJOR.MINOR.PATCH` 或 `vMAJOR.MINOR.PATCH-rc.N`。
`prepare_version.py` 校验 Tag 与 HEAD，生成展示版本、规范化 Python 版本、Windows 数字
版本、macOS 版本和 SHA；生成文件不提交。Python 使用 setuptools-scm，sdist 保留
重建信息。前端版本读取后端。无 Git 的开发构建为 `0.0.0.dev0`，不能正式发布。
Docker 构建显式传入 `VTS_PACKAGE_VERSION` 和 `VIDEO_TO_SUMMARY_VERSION`。

现有 OSS Guard 在每个 PR/main push 上运行离线/UI 门禁和 Windows/macOS 原生冻结 +
真实 WebView smoke。Release 的版本校验、Python 包门禁在先，随后两个平台分别构建
安装器并从安装产物启动真实引擎。45 分钟平台上限，诊断与构建产物保留 14 天。

Tag push 的发布顺序：全部构建通过 → 汇总四个附件 → `SHA256SUMS` → draft 上传 →
下载核验 → 公开 Release → GHCR amd64/arm64。正式附件为：

```text
vts-<package-version>-py3-none-any.whl
vts-<package-version>.tar.gz
vts-<tag>-windows-x64-setup.exe
vts-<tag>-macos-arm64.dmg
SHA256SUMS
```

`workflow_dispatch` 可选择已有 Tag 的 `build`（只构建）或 `draft`（上传核验但不公开）。
不要移动已发布 Tag。失败重跑可补全 draft；已公开 Release 只允许验证相同内容，
不同附件/内容会拒绝覆盖。draft 中额外旧附件也会阻断发布。预发布只推版本镜像，
不改 `latest`。Docker 失败保留 Release，重跑失败镜像 Job。首次正式 Tag 前仍需审查
PR、完成双平台 CI 和干净机器首次安装验收。

## 当前验证记录与待验收项

离线全量回归 516 个用例通过（排除 9 个 live/network 用例），前端类型检查与禁词门禁
通过。两个工作流通过 actionlint；全新哈希锁安装及 wheel/sdist 非 editable smoke 通过。

本机 macOS arm64 已通过 `.app` 和 DMG 挂载复制后的真实 WKWebView、React 初始化、
原生桥/剪贴板、Fernet、SQLite FTS5、health/版本/JS/CSS/手册、FFmpeg 音频转换及退出
回收检查。开发 DMG 约 55 MiB；带 FFmpeg 缓存的冻结加 DMG 约 51 秒，首轮 macOS CI 冷构建（FFmpeg 编译加冻结）为 275 秒，
两者构建范围不同。`build/desktop/metrics.json` 按各产物记录字节数和本次总构建时长。

Windows 原生冻结、EXE 安装卸载、WebView2 和进程回收必须由 Windows CI/机器验证；
干净 Windows 11 与 macOS 14+ 下载首开、SmartScreen/Gatekeeper 的人工验收仍需完成。
联网站点和实际 BYOK 服务受端点变化影响，应另外使用现有 live 测试开关验收。
以上待验收项未通过前，不发布首个正式桌面版本。
