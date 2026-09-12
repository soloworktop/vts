"""视频自带字幕解析：把 yt-dlp 下载的字幕文件（VTT / SRT / JSON3）转成转写结果。

当视频本身提供字幕时，pipeline 直接用现成字幕跳过「下载音频 + 语音转写」两个阶段，
本模块负责把各种字幕格式解析为与转写器输出一致的 ``TranscriptResult``
（``text`` 为纯文本，``segments`` 带时间戳），从而无缝接入后续的优化/总结流程。

支持格式：
- WebVTT（``.vtt``）：YouTube 自动字幕、多数平台标准输出；
- SubRip（``.srt``）：B 站等平台；
- JSON3（``.json3`` / ``.json``）：YouTube 自动字幕的另一种原始格式。
"""

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .schemas import AudioMeta, TranscriptResult, TranscriptSegment

logger = logging.getLogger("video_to_summary.subtitles")


@dataclass
class SubtitleResult:
    """一次成功的字幕提取结果：转写文本 + 视频元信息 + 来源字幕文件。"""

    transcript: TranscriptResult
    meta: AudioMeta
    subtitle_path: Path
    language: str  # 语言代码，如 zh-Hans / en
    automatic: bool  # True = 平台自动生成字幕，False = 人工上传

# WebVTT 内联标签 / 行内时间戳（如 <c>、<00:00:00.500>、<v Speaker>）
_TAG_RE = re.compile(r"<[^>]*>")
_ENTITY_RE = re.compile(r"&(amp|lt|gt|nbsp|quot|#\d+);")


def _strip_tags(text: str) -> str:
    """去掉 WebVTT 内联标签与时间戳标签，并解码常见实体。"""
    text = _TAG_RE.sub("", text or "")
    text = _ENTITY_RE.sub(lambda m: _decode_entity(m.group(1)), text)
    return text


def _decode_entity(entity: str) -> str:
    if entity == "amp":
        return "&"
    if entity == "lt":
        return "<"
    if entity == "gt":
        return ">"
    if entity == "nbsp":
        return " "
    if entity == "quot":
        return '"'
    if entity.startswith("#"):
        try:
            return chr(int(entity[1:]))
        except (TypeError, ValueError):
            return ""
    return ""


def _parse_timestamp(raw: str) -> float:
    """解析 ``HH:MM:SS.mmm`` / ``MM:SS.mmm`` / ``SS.mmm``（毫秒可用逗号）为秒。"""
    ts = (raw or "").strip().replace(",", ".")
    if not ts:
        return 0.0
    try:
        total = 0.0
        for part in ts.split(":"):
            total = total * 60 + float(part)
        return total
    except (TypeError, ValueError):
        return 0.0


def _normalize_cue_lines(lines: list[str]) -> str:
    """把一条 cue 的多行文本合并为一行，去掉标签后返回；空则返回空串。"""
    text = _strip_tags(" ".join(line.strip() for line in lines if line.strip()))
    return re.sub(r"\s+", " ", text).strip()


def parse_vtt(text: str) -> list[TranscriptSegment]:
    """解析 WebVTT：忽略头部/NOTE，按时间轴 cue 切分。"""
    segments: list[TranscriptSegment] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if "-->" in line:
            left, _, right = line.partition("-->")
            start = _parse_timestamp(left)
            # 右侧可能是 "HH:MM:SS.mmm 位置:50% 行:..."，只取首个时间戳
            end = _parse_timestamp(right.strip().split()[0])
            i += 1
            buf: list[str] = []
            while i < len(lines) and lines[i].strip():
                buf.append(lines[i])
                i += 1
            cue = _normalize_cue_lines(buf)
            if cue:
                segments.append(TranscriptSegment(start=start, end=end, text=cue))
        else:
            i += 1
    return segments


def parse_srt(text: str) -> list[TranscriptSegment]:
    """解析 SubRip：索引行 + ``HH:MM:SS,mmm --> ...`` + 多行文本。"""
    segments: list[TranscriptSegment] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if "-->" in line:
            left, _, right = line.partition("-->")
            start = _parse_timestamp(left)
            end = _parse_timestamp(right.strip().split()[0])
            i += 1
            buf: list[str] = []
            while i < len(lines) and lines[i].strip():
                buf.append(lines[i])
                i += 1
            cue = _normalize_cue_lines(buf)
            if cue:
                segments.append(TranscriptSegment(start=start, end=end, text=cue))
        else:
            i += 1
    return segments


def parse_json3(text: str) -> list[TranscriptSegment]:
    """解析 YouTube JSON3 字幕（events[].segs[].utf8，tStartMs 毫秒）。"""
    segments: list[TranscriptSegment] = []
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return segments
    for event in payload.get("events") or []:
        segs = event.get("segs") or []
        if not segs:
            continue
        start_ms = event.get("tStartMs") or 0
        end_ms = event.get("tEndMs") or start_ms
        cue = "".join(str(s.get("utf8") or "") for s in segs).strip()
        if cue:
            segments.append(
                TranscriptSegment(
                    start=start_ms / 1000.0,
                    end=end_ms / 1000.0,
                    text=cue,
                )
            )
    return segments


def parse_subtitle_file(path: Path) -> TranscriptResult:
    """按扩展名解析字幕文件为 ``TranscriptResult``；无法解析时尽力返回纯文本。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    ext = path.suffix.lower()
    if ext == ".vtt":
        segments = parse_vtt(text)
    elif ext == ".srt":
        segments = parse_srt(text)
    elif ext in (".json", ".json3"):
        segments = parse_json3(text)
    else:
        segments = parse_vtt(text) or parse_srt(text)
    full_text = "\n".join(seg.text for seg in segments)
    return TranscriptResult(text=full_text, segments=segments)


__all__ = [
    "SubtitleResult",
    "parse_subtitle_file",
    "parse_vtt",
    "parse_srt",
    "parse_json3",
]
