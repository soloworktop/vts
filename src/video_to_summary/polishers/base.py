from pathlib import Path
from typing import Callable, Optional, Protocol, runtime_checkable

from video_to_summary.schemas import TranscriptResult


@runtime_checkable
class TranscriptPolisher(Protocol):
    def polish(
        self,
        transcript_result: TranscriptResult,
        title: str = "",
        cancel_check: Optional[Callable[[], None]] = None,
    ) -> TranscriptResult:
        ...
