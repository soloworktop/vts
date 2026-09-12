from pathlib import Path
from typing import Protocol, runtime_checkable

from video_to_summary.schemas import AudioMeta


@runtime_checkable
class Source(Protocol):
    def resolve(self) -> tuple[Path, AudioMeta]:
        ...
