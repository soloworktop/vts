from pathlib import Path
from typing import Callable, Optional, Protocol, runtime_checkable

from ..schemas import SummaryOutput


@runtime_checkable
class Summarizer(Protocol):
    def summarize(
        self,
        transcript: str,
        title: str = "",
        cancel_check: Optional[Callable[[], None]] = None,
    ) -> str:
        ...


__all__ = ["Summarizer"]
