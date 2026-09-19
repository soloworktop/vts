import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from video_to_summary.config import SubtitleConfig
from video_to_summary.constants import JobEvent, SubtitlePreference
from video_to_summary.cancel_utils import call_with_cancel
from video_to_summary.schemas import AudioMeta, SummaryOutput, TranscriptResult, TranscriptSegment
from video_to_summary.sources.base import Source
from video_to_summary.summarizers.base import Summarizer
from video_to_summary.transcribers.base import Transcriber
from video_to_summary.polishers.base import TranscriptPolisher

logger = logging.getLogger("video_to_summary.pipeline")


def _atomic_write_text(path: Path, text: str) -> None:
    """tmp + os.replace 原子写：崩溃/断电绝不留下半截文件。

    resume 恢复以「文件存在即缓存」复用产物，半写文件会被静默当作有效
    转写/总结使用，因此主链路所有落盘必须原子（与 web/app.py 总结编辑
    接口的 tmp+os.replace 同一约定）。
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _emit(on_event: Optional[Callable[[str, dict], None]], event: str, payload: dict | None = None) -> None:
    if on_event is None:
        return
    try:
        on_event(event, payload or {})
    except Exception:  # noqa: BLE001 - 进度回调不得影响主流程，但需记录便于排查
        logger.warning("progress callback failed on event %r", event, exc_info=True)


def _maybe_cancel(cancel_check: Optional[Callable[[], None]]) -> None:
    """在阶段边界执行取消检查；用户主动取消时由调用方抛出的异常中止流程。"""
    if cancel_check is not None:
        cancel_check()


def run(
    source: Source,
    transcriber: Transcriber,
    summarizer: Optional[Summarizer],
    output_dir: Path,
    *,
    polisher: Optional[TranscriptPolisher] = None,
    polisher_title: str = "",
    on_event: Optional[Callable[[str, dict], None]] = None,
    cancel_check: Optional[Callable[[], None]] = None,
    subtitle_config: Optional[SubtitleConfig] = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)

    _maybe_cancel(cancel_check)

    # ---- 阶段 1：获取转写文本（视频自带字幕优先，否则下载音频 + 转写） ----
    transcript_result: Optional[TranscriptResult] = None
    meta: Optional[AudioMeta] = None
    transcript_path: Optional[Path] = None
    segments_path: Optional[Path] = None

    extractor = getattr(source, "extract_subtitle", None)
    if (
        subtitle_config is not None
        and subtitle_config.preference != SubtitlePreference.OFF
        and extractor is not None
    ):
        _emit(on_event, JobEvent.SUBTITLE_START)
        _t = time.perf_counter()
        try:
            if cancel_check is not None:
                fetched = call_with_cancel(
                    lambda: extractor(subtitle_config),
                    cancel_check,
                    cancel_log_msg="subtitle extraction cancelled by user",
                )
            else:
                fetched = extractor(subtitle_config)
        except Exception as exc:  # noqa: BLE001 - 字幕失败回退音频，不中断任务
            # 异常文本可能内嵌 URL/proxy 凭据（yt-dlp 报文），统一打码后进日志
            from .log_export import scrub_url_credentials

            logger.warning("subtitle extraction failed, fallback to audio: %s", scrub_url_credentials(str(exc)))
            fetched = None
        _emit(
            on_event,
            JobEvent.SUBTITLE_DONE if fetched is not None else JobEvent.SUBTITLE_SKIPPED,
            (
                # title/duration 供 Web 层回写任务标题（字幕路径跳过了下载，无 download_done）
                {"title": fetched.meta.title, "duration": fetched.meta.duration, "elapsed": time.perf_counter() - _t}
                if fetched is not None
                else {"reason": getattr(source, "subtitle_skip_reason", "") or "no usable subtitle"}
            ),
        )
        if fetched is not None:
            transcript_result = fetched.transcript
            meta = fetched.meta
            transcript_path = output_dir / f"{meta.source_id}.txt"
            segments_path = output_dir / f"{meta.source_id}.segments.json"
            if transcript_path.exists():
                transcript_result = TranscriptResult(
                    text=transcript_path.read_text(encoding="utf-8"),
                    segments=_load_segments(segments_path),
                )
            else:
                _atomic_write_text(transcript_path, transcript_result.text)
                _write_segments(segments_path, meta, transcript_result.segments)
                _write_srt(output_dir / f"{meta.source_id}.srt", transcript_result.segments)
        _maybe_cancel(cancel_check)

    if transcript_result is None:
        _emit(on_event, JobEvent.DOWNLOAD_START)
        _t = time.perf_counter()
        if cancel_check is not None:
            audio_path, meta = call_with_cancel(
                lambda: source.resolve(),
                cancel_check,
                cancel_log_msg="audio download cancelled by user",
            )
        else:
            audio_path, meta = source.resolve()
        _maybe_cancel(cancel_check)
        _emit(on_event, JobEvent.DOWNLOAD_DONE, {"title": meta.title, "duration": meta.duration, "elapsed": time.perf_counter() - _t})

        transcript_path = output_dir / f"{meta.source_id}.txt"
        segments_path = output_dir / f"{meta.source_id}.segments.json"

        _emit(on_event, JobEvent.TRANSCRIBE_START)
        _t = time.perf_counter()
        if transcript_path.exists():
            transcript_text = transcript_path.read_text(encoding="utf-8")
            existing_segments = _load_segments(segments_path)
            transcript_result = TranscriptResult(
                text=transcript_text,
                segments=existing_segments,
            )
            _emit(on_event, JobEvent.TRANSCRIBE_DONE, {"cached": True, "elapsed": time.perf_counter() - _t})
        else:
            transcript_result = transcriber.transcribe(audio_path, cancel_check=cancel_check)
            _atomic_write_text(transcript_path, transcript_result.text)
            _write_segments(segments_path, meta, transcript_result.segments)
            _write_srt(output_dir / f"{meta.source_id}.srt", transcript_result.segments)
            # 任务完成回调指标：真实送 ASR 的音频时长（best-effort，探测失败为 None）
            from .downloader import probe_duration_seconds

            _emit(on_event, JobEvent.TRANSCRIBE_DONE, {
                "cached": False,
                "elapsed": time.perf_counter() - _t,
                "audio_seconds": probe_duration_seconds(audio_path),
            })
        _maybe_cancel(cancel_check)

    if polisher is not None:
        polished_path = output_dir / f"{meta.source_id}.polished.txt"
        _emit(on_event, JobEvent.POLISH_START)
        _t = time.perf_counter()
        if polished_path.exists():
            transcript_result = TranscriptResult(
                text=polished_path.read_text(encoding="utf-8"),
                segments=_load_segments(segments_path),
            )
            _emit(on_event, JobEvent.POLISH_DONE, {"cached": True, "elapsed": time.perf_counter() - _t})
        else:
            transcript_result = polisher.polish(
                transcript_result,
                title=polisher_title or (meta.title or meta.source_id),
                cancel_check=cancel_check,
            )
            _atomic_write_text(polished_path, transcript_result.text)
            _write_segments(segments_path, meta, transcript_result.segments)
            _emit(on_event, JobEvent.POLISH_DONE, {"cached": False, "elapsed": time.perf_counter() - _t})
        _maybe_cancel(cancel_check)

    summary_text = ""
    if summarizer is not None:
        _emit(on_event, JobEvent.SUMMARIZE_START)
        _t = time.perf_counter()
        summary_text = summarizer.summarize(
            transcript_result.text,
            title=meta.title or meta.source_id,
            cancel_check=cancel_check,
        )
        # 任务完成回调指标：总结输入/输出字数（输入 = 转写/字幕文本；输出 = 总结 Markdown）
        _emit(on_event, JobEvent.SUMMARIZE_DONE, {
            "elapsed": time.perf_counter() - _t,
            "input_chars": len(transcript_result.text or ""),
            "output_chars": len(summary_text or ""),
        })
        _maybe_cancel(cancel_check)

    output = SummaryOutput(
        title=meta.title or meta.source_id,
        source_url=meta.source_url,
        duration=meta.duration,
        summary=summary_text,
        transcript=transcript_result.text,
    )

    summary_path = output_dir / f"{meta.source_id}.summary.md"
    _atomic_write_text(summary_path, output.to_markdown())
    return summary_path


def _load_segments(segments_path: Path) -> list:
    if not segments_path.exists():
        return []
    try:
        import json

        payload = json.loads(segments_path.read_text(encoding="utf-8"))
        raw_segments = payload.get("segments", [])
    except Exception:
        return []
    segments: list[TranscriptSegment] = []
    for item in raw_segments:
        if isinstance(item, dict):
            segments.append(
                TranscriptSegment(
                    start=float(item.get("start", 0) or 0),
                    end=float(item.get("end", 0) or 0),
                    text=(item.get("text") or "").strip(),
                )
            )
    return segments


def _write_segments(segments_path: Path, meta: AudioMeta, segments: list) -> None:
    if not segments:
        return
    _atomic_write_text(segments_path, _serialize_segments(meta, segments))


def _serialize_segments(meta: AudioMeta, segments: list) -> str:
    payload = {
        "meta": {
            "id": meta.source_id,
            "title": meta.title,
            "url": meta.source_url,
            "duration": meta.duration,
            "uploader": meta.uploader,
            "upload_date": meta.upload_date,
            "audio_path": str(meta.audio_path),
        },
        "segments": [
            {
                "start": seg.start,
                "end": seg.end,
                "text": seg.text,
            }
            for seg in segments
        ],
    }
    import json

    return json.dumps(payload, ensure_ascii=False, indent=2)


def _format_srt_timestamp(seconds: float) -> str:
    """把秒数格式化为 SRT 时间戳 ``HH:MM:SS,mmm``。"""
    total_ms = int(round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _write_srt(path: Path, segments: list) -> None:
    """把 segments 序列化为标准 SubRip (.srt) 文件。

    基于转写/字幕的原始时间戳，不经过 polisher（优化文本无法对齐时间戳）。
    segments 为空时不写文件。
    """
    if not segments:
        return
    blocks: list[str] = []
    for idx, seg in enumerate(segments, start=1):
        text = (seg.text or "").strip()
        if not text:
            continue
        blocks.append(
            f"{idx}\n"
            f"{_format_srt_timestamp(seg.start)} --> {_format_srt_timestamp(seg.end)}\n"
            f"{text}\n"
        )
    if not blocks:
        return
    _atomic_write_text(path, "\n".join(blocks))
