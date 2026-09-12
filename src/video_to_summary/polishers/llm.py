from pathlib import Path
from typing import Callable, Optional

from video_to_summary.schemas import TranscriptResult, TranscriptSegment
from video_to_summary.polishers.base import TranscriptPolisher
from video_to_summary.utils import clean_advisor_artifacts


class LLMTranscriptPolisher:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: Optional[str] = None,
        system_prompt: Optional[str] = None,
        prompt_preset: str = "default",
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.system_prompt = system_prompt
        self.prompt_preset = prompt_preset
        self._client = None

    def _get_client(self):
        if self._client is None:
            self._client = _make_client(self.api_key, self.base_url)
        return self._client

    def close(self) -> None:
        """释放实例级 OpenAI 客户端（含连接池）。"""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响主流程
                pass
            self._client = None

    def __enter__(self) -> "LLMTranscriptPolisher":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def polish(
        self,
        transcript_result: TranscriptResult,
        title: str = "",
        cancel_check: Optional[Callable[[], None]] = None,
    ) -> TranscriptResult:
        if not transcript_result.text.strip():
            return transcript_result

        client = self._get_client()
        messages = [
            {
                "role": "system",
                "content": self.system_prompt or _default_system_prompt(self.prompt_preset),
            },
            {
                "role": "user",
                "content": _build_prompt(title, transcript_result.text, self.prompt_preset),
            },
        ]

        polished_text = _polish_once(client, self.model, messages, transcript_result.text, cancel_check)

        segments = _rebuild_segments(transcript_result.segments, polished_text)
        return TranscriptResult(text=polished_text, segments=segments)


def _make_client(api_key: str, base_url: Optional[str]):
    from openai import OpenAI  # type: ignore

    client_kwargs = {"api_key": api_key, "timeout": 120.0, "max_retries": 0}
    if base_url:
        client_kwargs["base_url"] = base_url
    return OpenAI(**client_kwargs)


def _polish_once(client, model: str, messages, original_text: str, cancel_check) -> str:
    """执行一次 LLM polish 调用，异常时保留原文（容错）。支持中途取消。"""
    from video_to_summary.cancel_utils import call_with_cancel

    def _call():
        try:
            completion = client.chat.completions.create(model=model, messages=messages)
            content = completion.choices[0].message.content or ""
            return _clean_polished_text(content.strip())
        except Exception as exc:
            logging.getLogger("video_to_summary.polishers").warning(
                "polish failed (size=%d), keeping original: %s: %s",
                len(original_text), type(exc).__name__, exc,
            )
            return original_text

    if cancel_check is None:
        return _call()

    return call_with_cancel(
        _call,
        cancel_check,
        on_cancel=client.close,
        cancel_log_msg=f"polish cancelled by user (size={len(original_text)})",
    )


def _default_system_prompt(preset: str) -> str:
    if preset == "light":
        return (
            "你是一个中文语音转写文本校对助手。修正明显的 ASR 识别错误和错别字，"
            "清理口语冗余词（如\"呃\"\"啊\"\"对吧\"等语气词），保持原意不变。"
            "不要添加解释、总结或评论。"
        )

    return (
        "你是一个中文语音转写文本校对助手。你的任务是修正 ASR 识别错误，"
        "清理口语冗余，输出通顺可读的文本。\n\n"
        "必须遵守：\n"
        "1. 修正明显的 ASR 错误：同音/近音错别字、专有名词错误（如\"街月星辰\"→\"阶跃星辰\"、\"灵异万物\"→\"零一万物\"）。\n"
        "2. 清理口语冗余词：\"呃\"\"啊\"\"对吧\"\"就是说\"\"然后然后\"等无意义语气词和重复词。\n"
        "3. 合理断句合并：把被错误切分的短句合并为完整句子，一句话一行。\n"
        "4. 保持原意和说话顺序，不要添加、总结或评论任何内容。\n"
        "5. 只输出修正后的文本，不要输出其他任何内容。\n"
    )


def _build_prompt(title: str, transcript: str, preset: str) -> str:
    # 与 summarizer 一致：全文传入不截断，由模型 context window 承载
    if preset == "light":
        return (
            "修正明显 ASR 错误和错别字，清理口语冗余词，保持原意。\n"
            f"（参考标题：{title}）\n\n"
            "以下是待修正的转写文本，请直接输出修正后的内容：\n\n"
            f"{transcript}"
        )

    return (
        "请修正以下转写文本的 ASR 错误和口语冗余，输出通顺可读的文本。\n"
        f"（参考标题：{title}，仅供理解上下文，不要输出标题）\n\n"
        "以下是待修正的转写文本：\n"
        f"{transcript}\n\n"
        "输出要求：\n"
        "- 修正同音/近音错别字和专有名词错误\n"
        "- 清理\"呃\"\"啊\"\"对吧\"等无意义语气词和重复词\n"
        "- 合并被错误切分的短句，一句话一行\n"
        "- 保持原意和说话顺序\n"
        "- 不要输出标题、总结或评论\n"
        "- 只输出修正后的文本\n"
    )


def _clean_polished_text(text: str) -> str:
    # 与 summarizer 共用同一份 advisor 残留清理规则
    return clean_advisor_artifacts(text)


def _rebuild_segments(
    original_segments: list[TranscriptSegment],
    polished_text: str,
) -> list[TranscriptSegment]:
    rebuilt: list[TranscriptSegment] = []
    polished_lines = [line for line in polished_text.splitlines() if line.strip()]
    for idx, line in enumerate(polished_lines):
        start, end = _guess_line_time(original_segments, idx, len(polished_lines))
        rebuilt.append(TranscriptSegment(start=start, end=end, text=line.strip()))
    if not rebuilt and polished_text:
        rebuilt.append(TranscriptSegment(start=0.0, end=0.0, text=polished_text.strip()))
    return rebuilt


def _guess_line_time(
    original_segments: list[TranscriptSegment],
    index: int,
    total_lines: int,
) -> tuple[float, float]:
    if original_segments:
        if index < len(original_segments):
            seg = original_segments[index]
            return float(seg.start), float(seg.end)
        last = original_segments[-1]
        return float(last.end), float(last.end) + 1.0
    if total_lines <= 1:
        return 0.0, 0.0
    start = float(index)
    end = float(index + 1)
    return start, end


__all__ = ["TranscriptPolisher", "LLMTranscriptPolisher"]
