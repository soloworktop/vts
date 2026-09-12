"""VTS：视频 URL → 转写 → 结构化 Markdown 笔记（开源核心）。

import 包名为 ``video_to_summary``，发行名为 ``vts``（见 README「命名」一节）。
"""

from .config import Settings
from .pipeline import run
from .schemas import AudioMeta, SummaryOutput, TranscriptResult, TranscriptSegment
from .sources.base import Source
from .sources.local import LocalAudioSource
from .sources.url import URLAudioSource
from .summarizers.base import Summarizer
from .summarizers.openai import OpenAISummarizer, get_summary_template, SUMMARY_TEMPLATES
from .transcribers.base import Transcriber
from .transcribers.openai_whisper_api import OpenAIWhisperAPITranscriber
from .polishers.base import TranscriptPolisher
from .polishers.llm import LLMTranscriptPolisher

__all__ = [
    "Settings",
    "run",
    "AudioMeta",
    "SummaryOutput",
    "TranscriptResult",
    "TranscriptSegment",
    "Source",
    "LocalAudioSource",
    "URLAudioSource",
    "Transcriber",
    "OpenAIWhisperAPITranscriber",
    "Summarizer",
    "OpenAISummarizer",
    "get_summary_template",
    "SUMMARY_TEMPLATES",
    "TranscriptPolisher",
    "LLMTranscriptPolisher",
]
