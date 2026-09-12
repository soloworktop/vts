"""通用下载/探测辅助。

注意：音频下载逻辑统一收敛到 ``sources/url.py::download_audio``（支持音频格式参数、
返回 AudioMeta），此处不再保留重复的下载实现，仅提供时长探测等通用工具。
"""

import subprocess
from pathlib import Path
from typing import Optional

from video_to_summary.utils import subprocess_no_window_kwargs


def probe_duration_seconds(path: Path) -> Optional[float]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        # Windows GUI 形态（桌面 App）下必须带无窗口标志，否则弹 ffprobe 控制台
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=True, **subprocess_no_window_kwargs()
        )
        value = result.stdout.strip()
        return float(value) if value else None
    except Exception:
        return None
