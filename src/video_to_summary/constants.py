"""集中定义任务状态、进度事件、LLM 用途等字符串常量。

使用「常量命名空间类」（Python 3.10 兼容，无需 StrEnum），
值即数据库/API 中使用的字面量，避免魔数散落各处造成拼写漂移。
"""

# 历史任务标签系统的保留名：无任何标签关联的任务虚拟呈现为「未分类」，
# 不落库（见 web/label_store.py）；创建同名真实标签被拒绝
UNCATEGORIZED_LABEL = "未分类"

# 浏览器上传源文件的托管目录名（位于 upload_base 下，每个上传一个 uuid 子目录）。
# 上传文件归服务端托管：任务删除时连带回收，区别于用户自己的本地文件（永不删除）
UPLOADS_DIR_NAME = "uploads"


class JobStatus:
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobEvent:
    STARTED = "started"
    DOWNLOAD_START = "download_start"
    # 下载实时进度（yt-dlp progress_hook，源侧节流：≥1s 或 ≥5% 步进）；
    # payload: {percent(None=总大小未知), downloaded_bytes, total_bytes,
    #           speed(None=未知), eta(None=未知)}
    DOWNLOAD_PROGRESS = "download_progress"
    DOWNLOAD_DONE = "download_done"
    # 字幕优先：视频自带可用字幕时跳过「下载音频 + 转写」
    SUBTITLE_START = "subtitle_start"
    SUBTITLE_DONE = "subtitle_done"
    SUBTITLE_SKIPPED = "subtitle_skipped"
    TRANSCRIBE_START = "transcribe_start"
    TRANSCRIBE_DONE = "transcribe_done"
    # 分片转写进度（转写器分片并发时每完成一片推一条，payload: {done, total}）
    TRANSCRIBE_PROGRESS = "transcribe_progress"
    POLISH_START = "polish_start"
    POLISH_DONE = "polish_done"
    # 因缺少可用 API Key 而跳过对应 LLM 阶段（前端据此展示降级说明而非静默缺产物）
    POLISH_SKIPPED = "polish_skipped"
    SUMMARIZE_START = "summarize_start"
    SUMMARIZE_DONE = "summarize_done"
    SUMMARIZE_SKIPPED = "summarize_skipped"
    FINALIZED = "finalized"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"
    CANCEL_REQUESTED = "cancel_requested"  # 用户点击停止、等待阶段边界中止时触发



class SubtitlePreference:
    """视频自带字幕的使用策略（全局设置）。"""
    AUTO = "auto"  # 有字幕就用（人工字幕优先，其次平台自动字幕）；没有则回退下载+转写
    MANUAL_ONLY = "manual_only"  # 仅使用人工上传字幕；没有则回退
    OFF = "off"  # 关闭字幕优先，始终下载音频+转写


class SourceType:
    URL = "url"
    LOCAL = "local"
