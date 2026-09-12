"""公共工具：时间戳格式化、advisor 残留清理、日志辅助、子进程无窗口辅助。

advisor 清理逻辑此前在 summarizer 与 polisher 各维护一份且已出现规则漂移，
现统一收敛到本模块，保证两个输出路径行为一致。
"""

import logging
import os
import subprocess

logger = logging.getLogger("video_to_summary")

# advisor 残留标记：两个模块共用同一份，避免漂移
ADVISOR_MARKERS = (
    "[Advisor consultation",
    "[Advisor review]",
    "<advisor>",
    "</advisor",
    "[End of advisor consultation",
)


def format_timestamp(seconds: float) -> str:
    seconds = int(seconds)
    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


# Windows 子进程控制台抑制。GUI 形态（桌面 App = PyInstaller --windowed / pythonw）
# spawn 任何控制台程序（ffmpeg/ffprobe/powershell、yt-dlp 内部音频提取的 ffmpeg）
# 时，系统会为每个子进程新开黑色控制台窗口（Win11 默认终端托管下进程退出后还可能
# 停留显示退出码），必须带 CREATE_NO_WINDOW 创建标志。等值 0x08000000；
# subprocess.CREATE_NO_WINDOW 仅 Windows 定义，用 getattr 兜底。
_WINDOWS_CREATE_NO_WINDOW = 0x08000000


def _merge_no_window_flags(current) -> int:
    """把 CREATE_NO_WINDOW 并入给定创建标志（None 视为 0）；非 Windows 原样返回。"""
    if os.name != "nt":
        return current or 0
    return (current or 0) | _WINDOWS_CREATE_NO_WINDOW


def subprocess_no_window_kwargs() -> dict:
    """``subprocess.run/Popen`` 的 Windows 无控制台 kwargs；其它平台返回空 dict。

    包内所有 spawn 点都必须 ``**subprocess_no_window_kwargs()`` 展开
    （防漂移扫描见 tests/test_windows_no_console.py），否则 Windows 桌面形态
    每次音频压缩/时长探测/文件选择都会弹控制台窗口。
    """
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", _WINDOWS_CREATE_NO_WINDOW)}
    return {}


def install_windows_subprocess_guard() -> None:
    """进程级兜底：给所有 ``subprocess.Popen`` 默认注入 CREATE_NO_WINDOW（仅 Windows 生效）。

    覆盖第三方库的内部 spawn——yt-dlp 的 ffmpeg/ffprobe 调用不带创建标志，
    桌面任务在下载音频提取阶段同样弹窗。幂等；显式传入的 creationflags 按 OR
    合并不被覆盖；非 Windows 平台安装后等价直通（Popen 调用时才判断 os.name）。
    """
    if getattr(subprocess.Popen, "_vts_no_window_guard", False):
        return
    origin = subprocess.Popen.__init__

    def _guarded_init(self, *args, **kwargs):  # noqa: ANN001,ANN002,ANN003 - 透传签名
        kwargs["creationflags"] = _merge_no_window_flags(kwargs.get("creationflags"))
        origin(self, *args, **kwargs)

    subprocess.Popen.__init__ = _guarded_init
    subprocess.Popen._vts_no_window_guard = True


def infer_source_platform(url: str) -> str:
    """从 URL 推断视频来源平台；无 URL（本地文件）返回 ``local``。"""
    if not url:
        return "local"
    try:
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001 - 解析失败按未知处理
        host = ""
    for marker, platform in (
        ("bilibili", "bilibili"),
        ("youtube", "youtube"),
        ("youtu.be", "youtube"),
        ("twitter", "twitter"),
        ("x.com", "twitter"),
        ("douyin", "douyin"),
        ("weibo", "weibo"),
        ("xiaohongshu", "xiaohongshu"),
    ):
        if marker in host:
            return platform
    return host or "unknown"


def _is_advisor_artifact(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith("#") and stripped.endswith("]"):
        return True
    for prefix in (
        "调用`advisor`",
        "战略指导：",
        "## 现状判断",
        "## 关键决策点",
        "## 必须执行的检查项",
    ):
        if stripped.startswith(prefix):
            return True
    return False


def clean_advisor_artifacts(text: str) -> str:
    """清理 LLM 输出中的 advisor 残留：截断到最后一个标记之后，再过滤 artifact 行。"""
    for marker in ADVISOR_MARKERS:
        if marker in text:
            text = text.split(marker)[-1]
    lines = [line.rstrip() for line in text.splitlines()]
    lines = [line for line in lines if not any(marker in line for marker in ADVISOR_MARKERS)]
    lines = [line for line in lines if not _is_advisor_artifact(line)]
    while lines and not lines[0].strip():
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    return "\n".join(lines).strip()


def configure_logging(level: str | None = None) -> None:
    """为 CLI / Web 提供统一的根日志配置（重复调用幂等）。

    同时挂载内存 ring buffer（`log_export.attach_ring_buffer`，幂等），
    供 /api/logs/export 导出诊断包；uvicorn logger 在其内部单独挂载
    （handler 内按记录对象去重传播链重复）。
    """
    if level is None:
        import os

        level = os.environ.get("VIDEO_TO_SUMMARY_LOG_LEVEL", "INFO")
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    from video_to_summary.log_export import attach_ring_buffer

    attach_ring_buffer()
