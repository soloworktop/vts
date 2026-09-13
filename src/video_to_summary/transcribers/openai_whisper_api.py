"""OpenAI 兼容 Whisper API 转写适配器。

默认请求 ``response_format=verbose_json``：返回分段/时间戳，用于
``.segments.json`` 与 ``.srt`` 字幕产物。部分 OpenAI 兼容端点（如部分
国内厂商网关）不接受 ``verbose_json``、只支持 ``json``/``text``，会以
HTTP 400 + 错误信息提及 ``response_format`` 拒绝请求。

处理策略（自动降级）：
- 首选仍是 ``verbose_json``（保留分段与 SRT 能力）；
- 仅当端点明确拒绝该参数（HTTP 400 且错误信息提到 ``response_format``）
  时，自动改用 ``json`` 重试一次并打 WARNING——结果无分段、仅整段文本
  （SRT 不产出）；其它错误（401/403 认证、429 限流、5xx、超时等）原样
  抛出，绝不吞掉或误降级；
- 可用环境变量 ``ASR_RESPONSE_FORMAT`` 固定格式（``json``/``text``/
  ``verbose_json``），省去每次先失败一次的往返；非法值会 WARN 并回落
  自动降级行为。
"""

import logging
import os
from pathlib import Path
from typing import Callable, List, Optional

from video_to_summary.schemas import TranscriptResult, TranscriptSegment
from video_to_summary.transcribers.base import Transcriber
from video_to_summary.utils import format_timestamp as _format_timestamp

logger = logging.getLogger("video_to_summary.transcribers.openai_whisper_api")

# OpenAI 兼容 /audio/transcriptions 端点实际支持的 response_format 子集：
# verbose_json = 分段/时间戳（默认首选）；json/text = 仅文本（无分段）。
_ASR_RESPONSE_FORMAT_ENV = "ASR_RESPONSE_FORMAT"
_ASR_RESPONSE_FORMATS = ("json", "text", "verbose_json")


def _resolve_response_format(explicit: Optional[str]) -> Optional[str]:
    """解析 response_format：显式传入优先，否则读环境变量 ASR_RESPONSE_FORMAT。

    返回 None = 自动模式（先 verbose_json，被端点拒绝时降级 json）。
    非法值不静默丢弃：记录 WARNING 说明发生了什么并回落自动模式。
    """
    raw = explicit if explicit is not None else os.environ.get(_ASR_RESPONSE_FORMAT_ENV)
    value = (raw or "").strip().lower()
    if not value:
        return None
    if value in _ASR_RESPONSE_FORMATS:
        return value
    logger.warning(
        "%s=%r 非法，已忽略（仅支持 %s）；转写走默认行为（先 verbose_json，被拒后降级 json）",
        _ASR_RESPONSE_FORMAT_ENV,
        raw,
        "/".join(_ASR_RESPONSE_FORMATS),
    )
    return None


def _is_response_format_rejection(exc: Exception) -> bool:
    """判定是否为「端点不支持该 response_format」的明确参数错误，是才允许降级重试。

    判定条件：HTTP 400（openai SDK 的 BadRequestError 带 status_code=400）
    且错误信息里提到 response_format。其它错误（认证/限流/5xx/超时等）不匹配，
    原样抛出。
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "status", None)
    if status != 400:
        return False
    return "response_format" in str(exc)


class MissingASRCredentialsError(RuntimeError):
    """无字幕可回退且 Whisper API 未配置凭据：转写无法继续。

    CLI 字幕优先语义下，构造时 api_key 为空是合法状态（有自带字幕的视频
    完全不需要转写器）；只有视频无字幕、转写真正发生时才抛本错误。
    消息直接面向用户展示（CLI stderr / Web 任务失败原因），给可行动解法。
    """


class OpenAIWhisperAPITranscriber:
    def __init__(
        self,
        api_key: str,
        model: str = "whisper-1",
        language: Optional[str] = None,
        base_url: Optional[str] = None,
        response_format: Optional[str] = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.language = language
        self.base_url = base_url
        # None = 自动：先 verbose_json，被端点拒绝时降级 json（仅文本）
        self.response_format = _resolve_response_format(response_format)

    def transcribe(
        self,
        audio_path: Path,
        cancel_check: Optional[Callable[[], None]] = None,
    ) -> TranscriptResult:
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("openai package is required for --whisper-api") from exc

        # 无 Key 显式快速失败，不等 openai SDK 抛 "Missing credentials" 的
        # 裸 traceback：走到这里说明视频没有可用字幕，转写是必经阶段。
        # （settings 已回落过环境变量，此处空 = CLI 参数与环境变量确实都没有）
        if not self.api_key:
            raise MissingASRCredentialsError(
                "该视频没有可用的自带字幕，转写需要 Whisper API Key："
                "CLI 传 --openai-key / --llm-key（或设置 OPENAI_API_KEY 环境变量），"
                "Web 在「设置 → LLM 配置」填写语音识别模型的 Key"
            )

        client_kwargs: dict = {"api_key": self.api_key, "timeout": 300.0, "max_retries": 0}
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        client = OpenAI(**client_kwargs)

        from ..downloader import probe_duration_seconds

        duration = probe_duration_seconds(audio_path)
        client_max_seconds = 23 * 60 * 60
        if duration and duration > client_max_seconds:
            raise ValueError(
                f"audio duration {duration:.2f}s exceeds supported length for Whisper API"
            )

        base_params: dict = {"model": self.model}
        if self.language:
            base_params["language"] = self.language

        def _call():
            return self._request_with_fallback(client, audio_path, base_params)

        # 转写是最长最贵的阶段：必须可中途取消（on_cancel 经 client.close 中断 HTTP），
        # 并显式设 timeout/max_retries（SDK 默认 600s×2 重试会让停止请求挂到超时）
        if cancel_check is None:
            return self._parse_response(_call())

        from video_to_summary.cancel_utils import call_with_cancel

        return self._parse_response(
            call_with_cancel(
                _call,
                cancel_check,
                on_cancel=client.close,
                cancel_log_msg="transcription cancelled by user",
            )
        )

    def _request_with_fallback(self, client, audio_path: Path, base_params: dict):
        """发送一次转录请求；端点明确拒绝 verbose_json 时用 json 重试一次并 WARNING。

        仅自动模式（response_format=None）或显式 verbose_json 时回退；
        显式 json/text 与其它错误（401/429/5xx/超时等）原样抛出，绝不误降级。
        """
        first_format = self.response_format or "verbose_json"
        fallback_allowed = self.response_format in (None, "verbose_json")
        try:
            with audio_path.open("rb") as f:
                params = dict(base_params, response_format=first_format, file=f)
                return client.audio.transcriptions.create(**params)
        except Exception as exc:
            if fallback_allowed and _is_response_format_rejection(exc):
                logger.warning(
                    "该 OpenAI 兼容端点不支持 response_format=%r（HTTP 400：%s），"
                    "已自动降级为 response_format=json 重试：仅文本结果，"
                    "分段/字幕（SRT）可能不可用",
                    first_format,
                    exc,
                )
                with audio_path.open("rb") as f:
                    params = dict(base_params, response_format="json", file=f)
                    return client.audio.transcriptions.create(**params)
            raise

    @staticmethod
    def _parse_response(response) -> TranscriptResult:
        segments: List[TranscriptSegment] = []
        lines: List[str] = []

        if hasattr(response, "segments"):
            raw_segments = response.segments or []
        elif isinstance(response, dict):
            raw_segments = response.get("segments", [])
        else:
            raw_segments = []

        for seg in raw_segments:
            start = getattr(seg, "start", 0) or 0
            end = getattr(seg, "end", 0) or 0
            text = getattr(seg, "text", "") or ""
            if isinstance(seg, dict):
                start = seg.get("start", 0) or 0
                end = seg.get("end", 0) or 0
                text = seg.get("text", "") or ""
            segments.append(TranscriptSegment(start=start, end=end, text=text.strip()))
            lines.append(f"[{_format_timestamp(start)}] {text.strip()}")

        if isinstance(response, str):
            # response_format=text（或部分兼容端点对 json 直接返回字符串）
            full_text = response
        elif hasattr(response, "text"):
            full_text = getattr(response, "text", "") or ""
        elif isinstance(response, dict):
            full_text = response.get("text", "") or ""
        else:
            full_text = ""

        if not lines and full_text:
            lines = [full_text]

        return TranscriptResult(text="\n".join(lines), segments=segments)
