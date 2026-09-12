from pathlib import Path
from typing import Callable, Optional, Protocol, runtime_checkable

from video_to_summary.schemas import TranscriptResult


@runtime_checkable
class Transcriber(Protocol):
    def transcribe(
        self,
        audio_path: Path,
        cancel_check: Optional[Callable[[], None]] = None,
    ) -> TranscriptResult:
        ...


__all__ = ["Transcriber"]
