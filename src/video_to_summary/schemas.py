from pathlib import Path
from typing import Optional


class AudioMeta:
    def __init__(
        self,
        *,
        source_id: str,
        title: Optional[str],
        source_url: Optional[str] = None,
        duration: Optional[int] = None,
        uploader: Optional[str] = None,
        upload_date: Optional[str] = None,
        audio_path: Path,
    ) -> None:
        self.source_id = source_id
        self.title = title or source_id
        self.source_url = source_url
        self.duration = duration
        self.uploader = uploader
        self.upload_date = upload_date
        self.audio_path = audio_path


class TranscriptSegment:
    def __init__(self, start: float, end: float, text: str) -> None:
        self.start = float(start)
        self.end = float(end)
        self.text = (text or "").strip()


class TranscriptResult:
    def __init__(self, text: str, segments: list[TranscriptSegment]) -> None:
        self.text = text
        self.segments = segments


class SummaryOutput:
    def __init__(
        self,
        *,
        title: str,
        source_url: Optional[str],
        duration: Optional[int],
        summary: str,
        transcript: str,
    ) -> None:
        self.title = title
        self.source_url = source_url
        self.duration = duration
        self.summary = summary
        self.transcript = transcript

    def to_markdown(self) -> str:
        lines = [
            f"# {self.title}",
            "",
            "- **来源**：{url}".format(url=self.source_url or ""),
            "- **时长**：{duration}".format(duration=_fmt_duration(self.duration)),
            "",
            "## 摘要",
            "",
            self.summary or "_未生成摘要（未配置 LLM Key）_",
            "",
        ]
        return "\n".join(lines)


def _fmt_duration(seconds: Optional[int]) -> str:
    if not seconds:
        return "未知"
    seconds = int(seconds)
    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)
    if h:
        return f"{h}小时{m}分钟{s}秒"
    return f"{m}分钟{s}秒"
