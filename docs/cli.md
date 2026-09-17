# 命令行参考（cli）

`vts` 是安装 VTS 后自带的命令，等价于 `python -m video_to_summary.main`（仓库内无
`__main__.py`，`python -m video_to_summary` 不带 `.main` 不可用）。本文与
`src/video_to_summary/main.py` 的实际参数一一对应；`vts --help` 永远是最新清单。

## 安装

- **源码开发**：`pip install -e .`
- **发行包**（尚未发布 PyPI）：从
  [Release](https://github.com/soloworktop/vts/releases) 附件安装，例如
  `pip install https://github.com/soloworktop/vts/releases/download/v0.1.0/vts-0.1.0-py3-none-any.whl`
- **免手动建环境**：`bash scripts/run.sh "<视频链接>"`——自动建 venv、装依赖再调用，
  参数原样转发

## 基本用法

```bash
vts "<视频链接>"
```

- 视频链接任意 [yt-dlp](https://github.com/yt-dlp/yt-dlp) 支持的 http(s) 站点均可；
- 有字幕 → 直接用字幕出稿（无需任何 Key）；无字幕 → 走 ASR 转写（需转写配置）；
- 未配置 LLM Key → 跳过总结、仅产出转写原文，**任务成功**（退出码 0）；
- 产物默认写在 `./output/`（`--output-dir` 可改），文件以视频 ID 为前缀：
  `.summary.md` / `.txt` / `.srt` / `.segments.json`（后两者含时间轴时生成）；
- 成功标志：终端最后一行 `summary -> output/<视频ID>.summary.md`；
- 重跑同一视频时，已存在的产物直接命中缓存（「文件存在即缓存」），不重复请求。

## 参数分组

### 总结（LLM 推理槽）

| 参数 | 说明 |
|---|---|
| `--summary-key` | LLM API Key；未提供时跳过总结（任务仍成功）。显式传入优先于环境变量 |
| `--summary-base-url` | OpenAI 兼容端点地址；未指定时默认 `https://api.openai.com/v1` 或环境变量 `SUMMARY_BASE_URL` |
| `--summary-model` | 模型名（如 `deepseek-chat`）；未指定时读环境变量 `SUMMARY_MODEL` |
| `--summary-template` | 总结模板名，默认 `通用`；只接受 10 种内置名（Web 自定义模板不适用于 CLI）：`通用` / `精简笔记` / `详细笔记` / `教程笔记` / `学术笔记` / `会议纪要` / `商业分析` / `小红书笔记` / `生活随笔` / `任务清单` |

隐藏兼容别名 `--llm-key` / `--llm-base-url` / `--llm-model` 仍被识别（等价
`--summary-*`），不再对外宣传。

### 转写（ASR 槽，仅视频无字幕时用到）

| 参数 | 说明 |
|---|---|
| `--asr-key` | 转写 API Key；无字幕且完全无 Key 时报「缺少转写凭据」并退出码 2 |
| `--asr-base-url` | 转写端点（需实现 `/audio/transcriptions`）；未指定时回落总结端点 |
| `--asr-model` | 转写模型，默认 `whisper-1` |

隐藏兼容别名 `--openai-key`（等价 `--asr-key`）仍被识别。

`--whisper-api`：历史遗留 flag。显式声明使用 Whisper API 转写路径，当前唯一作用是
在缺少 Key 时**提前**报配置错误（快速失败），不改变默认行为。

### 文本润色（可选）

| 参数 | 说明 |
|---|---|
| `--polish-transcript` / `--no-polish-transcript` | 润色开关；未显式传入时读环境变量 `POLISH_TRANSCRIPT`（默认关） |
| `--polish-model` / `--polish-base-url` | 润色模型 / 端点；未指定时跟随总结槽 |
| `--polish-preset` | 润色预设：`default`（默认）/ `light` |

启用润色且配置了 Key 时，产物额外生成 `.polished.txt`。

### 字幕

| 参数 | 说明 |
|---|---|
| `--subtitle-preference` | 字幕策略：`auto`（默认，优先字幕）/ `manual_only`（仅人工字幕）/ `off`（关字幕，直接走转写） |
| `--subtitle-language` | 字幕语言偏好；未指定时自动（中文优先），可传语言代码如 `en` |

### 网络与登录

| 参数 | 说明 |
|---|---|
| `--cookies` | Netscape 格式 cookies.txt 路径（等价环境变量 `VTS_COOKIES_FILE`；与浏览器 cookies 互斥，显式文件优先） |
| `--proxy` | HTTP/HTTPS 代理 |

环境变量 `VTS_USER_AGENT` / `VTS_COOKIES_FILE` 对 CLI 同样生效，语义见
[`docs/configuration.md`](configuration.md)。

### 其他

| 参数 | 说明 |
|---|---|
| `--output-dir` | 产物目录，默认 `output` |
| `--keep-video` | 保留下载的视频文件（默认转写后清理中间视频，产物不受影响） |
| `--audio-format` | 音频提取格式：`wav`（默认）/ `mp3` |
| `--version` | 显示版本号 |

## 环境变量兜底

CLI 参数显式传入优先；未传时按序回落环境变量（`.env` 从**运行命令时所在目录**向上
查找）：

- `SUMMARY_API_KEY` → `LLM_API_KEY` → `OPENAI_API_KEY`
- `ASR_API_KEY` → `OPENAI_API_KEY`（任务期还会跨槽兜底推理 Key）
- `SUMMARY_BASE_URL` → `LLM_BASE_URL`；`SUMMARY_MODEL` → `LLM_MODEL`
- `SUMMARY_TEMPLATE` / `POLISH_TRANSCRIPT` / `POLISH_MODEL` / `POLISH_BASE_URL` /
  `POLISH_PRESET` / `VTS_USER_AGENT` / `VTS_COOKIES_FILE`

完整兜底序与兼容别名见 [`docs/configuration.md`](configuration.md) 与
[`.env.example`](../.env.example)。

## 退出码

| 退出码 | 含义 |
|---|---|
| `0` | 成功——包括未配 LLM Key 的降级成功（仅产出转写原文） |
| `2` | 配置 / 业务错误：参数错误（argparse 惯例）、`--whisper-api` 缺 Key、无字幕视频缺转写凭据等；错误文案走 stderr，引导下一步配置 |

## 常用示例

```bash
# 有字幕视频，最简出稿（无需任何 Key 也有 .txt/.srt；配 Key 才有总结）
vts "https://www.youtube.com/watch?v=..."

# 接入 DeepSeek 出完整笔记
vts "https://www.bilibili.com/video/BV..." \
  --summary-key sk-xxx --summary-base-url https://api.deepseek.com/v1 --summary-model deepseek-chat

# 无字幕视频：OpenAI 官方转写 + 总结
vts "<视频链接>" --asr-key sk-xxx

# 自建转写网关 + 学术笔记模板
vts "<视频链接>" --asr-key sk-xxx --asr-base-url https://your-gateway/v1 \
  --summary-template 学术笔记

# 仅要逐字稿，不要总结、不要字幕（强制转写）
vts "<视频链接>" --subtitle-preference off --asr-key sk-xxx

# 需要登录态的站点
vts "<视频链接>" --cookies cookies.txt
```
