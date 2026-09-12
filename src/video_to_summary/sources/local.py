from pathlib import Path
from typing import Optional

from video_to_summary.schemas import AudioMeta
from video_to_summary.sources.base import Source


class LocalAudioSource:
    def __init__(self, audio_path: Path, *, title: Optional[str] = None) -> None:
        self.audio_path = audio_path
        self.title = title
        self.meta: Optional[AudioMeta] = None

    def resolve(self) -> tuple[Path, AudioMeta]:
        if not self.audio_path.exists():
            raise FileNotFoundError(f"audio file not found: {self.audio_path}")
        meta = AudioMeta(
            source_id=self.audio_path.stem,
            title=self.title or self.audio_path.stem,
            source_url=None,
            duration=None,
            uploader=None,
            upload_date=None,
            audio_path=self.audio_path,
        )
        self.meta = meta
        return self.audio_path, meta
