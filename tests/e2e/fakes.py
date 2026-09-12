"""E2E 专用进程内 fakes：替换三个网络边界（yt-dlp 下载 / Whisper 转写 / OpenAI LLM）。

只实现在 `pipeline.run` 与 `web/tasks._run_job_inner` 的 Protocol 接缝上真正会被
调用的方法，保证零网络、零模型、确定性输出。可变性（fail/delay 开关）供
失败、取消、重试等场景在同一次测试内翻转。
"""

import time
from pathlib import Path
from typing import Callable, List, Optional

from video_to_summary.schemas import (
    AudioMeta,
    SummaryOutput,
    TranscriptResult,
    TranscriptSegment,
)

# 刻意覆盖 _STYLE_CONTRACT 的全部受限排版元素：
# 引用块 + emoji 小标题 + 加粗 + 表格 + 受限 color span（SAFE_COLOR_SPAN 白名单内）
STYLE_RICH_BODY = (
    "> 🎯 **核心结论**：这是一条端到端验证用的总结正文。\n\n"
    "## 📌 关键要点\n\n"
    "- **要点一**：转写与摘要链路贯通\n"
    "- **要点二**：产物文件齐备\n\n"
    "| 阶段 | 状态 |\n"
    "| --- | --- |\n"
    "| 转写 | ✅ |\n"
    "| 摘要 | ✅ |\n\n"
    '<span style="color:#ff6752">珊瑚色强调文本</span>'
)

# summary 正文用于断言的关键子串（避免整段全等带来的脆弱断言）
STYLE_RICH_MARKERS = ("核心结论", "关键要点", "珊瑚色强调文本")


class FakeTranscriber:
    """同步 Transcriber 假体：delay>0 时按 50ms 切片 sleep 并响应 cancel_check。"""

    model = "fake-whisper"

    def __init__(
        self,
        *,
        text: str = "fake transcript line one\nfake transcript line two",
        segments: Optional[List[TranscriptSegment]] = None,
        delay: float = 0.0,
        fail: Optional[BaseException] = None,
    ) -> None:
        self.text = text
        self.segments = segments if segments is not None else [
            TranscriptSegment(0.0, 1.0, "fake segment one"),
            TranscriptSegment(1.0, 2.0, "fake segment two"),
        ]
        self.delay = delay
        self.fail = fail
        self.calls = 0
        # 镜像 StepASRTranscriber 的进度回调接口（web 层按 hasattr 接线）
        self.on_progress = None
        # 设置后转写开始即回报 on_progress(0, total)，结束时回报 (1, total)
        self.progress_total = None

    def transcribe(
        self,
        audio_path: Path,
        cancel_check: Optional[Callable[[], None]] = None,
    ) -> TranscriptResult:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        if self.on_progress is not None and self.progress_total:
            self.on_progress(0, self.progress_total)
        remaining = self.delay
        while remaining > 0:
            step = min(0.05, remaining)
            time.sleep(step)
            remaining -= step
            if cancel_check is not None:
                cancel_check()
        if self.on_progress is not None:
            self.on_progress(1, self.progress_total or 1)
        return TranscriptResult(text=self.text, segments=list(self.segments))


class FakeSummarizer:
    """同步 Summarizer 假体：summarize 返回 str（pipeline 契约）。"""

    model = "fake-llm"

    def __init__(self, body: str = STYLE_RICH_BODY, *, fail: Optional[BaseException] = None) -> None:
        self.body = body
        self.fail = fail
        self.calls = 0

    def summarize(
        self,
        transcript: str,
        title: str = "",
        cancel_check: Optional[Callable[[], None]] = None,
    ) -> str:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return f"## 总结正文（{title}）\n\n{self.body}"


class FakePolisher:
    """polish(transcript_result, title=, cancel_check=) -> TranscriptResult；close() 幂等。"""

    model = "fake-polish"

    def __init__(self) -> None:
        self.closed = False
        self.calls = 0

    def polish(
        self,
        transcript_result: TranscriptResult,
        title: str = "",
        cancel_check: Optional[Callable[[], None]] = None,
    ) -> TranscriptResult:
        self.calls += 1
        return TranscriptResult(
            text=f"polished::{transcript_result.text}",
            segments=transcript_result.segments,
        )

    def close(self) -> None:
        self.closed = True


class FakeUrlSource:
    """URL 源假体：跳过 yt-dlp，直接返回预置音频与 AudioMeta（触发标题回写链路）。"""

    def __init__(self, audio_path: Path, title: str = "E2E 视频标题", duration: int = 42) -> None:
        self.audio_path = audio_path
        self._title = title
        self._duration = duration
        self.meta: Optional[AudioMeta] = None
        self.resolve_calls = 0

    def resolve(self) -> tuple[Path, AudioMeta]:
        self.resolve_calls += 1
        meta = AudioMeta(
            source_id="e2e-fake-video",
            title=self._title,
            source_url="https://example.test/watch?v=e2e",
            duration=self._duration,
            uploader="e2e-uploader",
            audio_path=self.audio_path,
        )
        self.meta = meta
        return self.audio_path, meta


def build_fake_summary(title: str, body: str) -> SummaryOutput:
    """供需要直接构造 SummaryOutput 的场景使用（保持与 FakeSummarizer 一致的形状）。"""
    return SummaryOutput(
        title=title,
        source_url=None,
        duration=None,
        summary=body,
        transcript="fake transcript",
    )


__all__ = [
    "STYLE_RICH_BODY",
    "STYLE_RICH_MARKERS",
    "FakeTranscriber",
    "FakeSummarizer",
    "FakePolisher",
    "FakeUrlSource",
    "build_fake_summary",
]
