"""E2E 专用进程内 fakes：替换三个网络边界（yt-dlp 下载与字幕 / Whisper 转写 / OpenAI LLM）。

只实现在 `pipeline.run` 与 `web/tasks._run_job_inner` 的 Protocol 接缝上真正会被
调用的方法，保证零网络、零模型、确定性输出。可变性（fail/delay 开关）供
失败、取消、重试等场景在同一次测试内翻转。
"""

import time
from pathlib import Path
from typing import Callable, List, Optional

from video_to_summary.config import SubtitleConfig
from video_to_summary.schemas import (
    AudioMeta,
    SummaryOutput,
    TranscriptResult,
    TranscriptSegment,
)
from video_to_summary.subtitles import SubtitleResult

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


class FakeSubtitleUrlSource(FakeUrlSource):
    """字幕两段式的 URL 源假体：存在 `extract_subtitle` 属性即被 pipeline 走字幕优先路径。

    - usable=True（默认）：返回预置 SubtitleResult，`resolve()` 永不被调
      （用例断言 `resolve_calls == 0` 证明未下载音频）；
    - usable=False：`extract_subtitle` 返回 None（镜像真实源「无可用字幕」），
      pipeline 走 subtitle_skipped → 下载+转写回退链。
    真实源在无字幕时会把 `subtitle_skip_reason` 置为可读提示（SUBTITLE_SKIPPED
    payload 的 reason），这里镜像同一契约。config（语言/偏好）不影响假体返回——
    OFF 偏好的门控在 pipeline 侧，假体不重复实现。
    """

    def __init__(
        self, audio_path: Path, title: str = "E2E 字幕标题", duration: int = 42, *, usable: bool = True
    ) -> None:
        super().__init__(audio_path, title=title, duration=duration)
        self.usable = usable
        self.subtitle_calls = 0
        self.subtitle_skip_reason = "" if usable else "no usable subtitle"

    def extract_subtitle(self, config: SubtitleConfig) -> Optional[SubtitleResult]:
        self.subtitle_calls += 1
        if not self.usable:
            return None
        meta = AudioMeta(
            source_id="e2e-fake-sub",
            title=self._title,
            source_url="https://example.test/watch?v=e2e-sub",
            duration=self._duration,
            uploader="e2e-uploader",
            audio_path=self.audio_path,
        )
        segments = [
            TranscriptSegment(0.0, 1.5, "字幕段落一"),
            TranscriptSegment(1.5, 3.0, "字幕段落二"),
        ]
        # 真实源（sources/url.py）在字幕成功路径同样回填 self.meta——web 层
        # result_paths 的 source_id 取自 source.meta（此时 resolve 未被调过）
        self.meta = meta
        return SubtitleResult(
            transcript=TranscriptResult(text="字幕段落一\n字幕段落二", segments=segments),
            meta=meta,
            # pipeline 只读 transcript/meta，不触碰 subtitle_path 本体
            subtitle_path=self.audio_path,
            language="zh-Hans",
            automatic=False,
        )


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
    "FakeSubtitleUrlSource",
    "build_fake_summary",
]
