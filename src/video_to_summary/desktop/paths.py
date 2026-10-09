"""Desktop paths, derived through the canonical database resolver."""
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from ..db import resolve_database_path


@dataclass(frozen=True)
class DesktopPaths:
    database: Path
    output: Path
    logs: Path
    webview: Path

    @classmethod
    def resolve(cls):
        default = resolve_database_path(use_user_data=True).parent / "desktop"
        database = resolve_database_path(env=os.environ.get("VIDEO_TO_SUMMARY_DB") or str(default / "app.db"))
        data = database.expanduser().resolve().parent
        return cls(database.expanduser().resolve(),
                   Path(os.environ.get("VIDEO_TO_SUMMARY_OUTPUT_DIR") or data / "output").expanduser().resolve(),
                   data / "logs", data / "webview")

    def prepare(self):
        for directory in (self.database.parent, self.output, self.logs, self.webview):
            directory.mkdir(parents=True, exist_ok=True)


def resource_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))


def binary_path(name: str) -> str:
    suffix = ".exe" if sys.platform == "win32" else ""
    bundled = resource_root() / "bin" / (name + suffix)
    if bundled.is_file():
        return str(bundled)
    if getattr(sys, "frozen", False):
        raise RuntimeError(f"安装包缺少 {name}，请重新安装 VTS")
    import shutil
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"找不到 {name}；源码开发请安装 FFmpeg")
    return found
