"""OpenAIWhisperAPITranscriber 的 response_format 兼容性用例（全离线，打桩 openai 客户端）。

覆盖：verbose_json 成功解析分段（原有行为不回归）；端点 400 拒绝 verbose_json
时自动降级 json 重试（仅文本 + WARNING）；其它错误（401 等）原样抛出不重试；
ASR_RESPONSE_FORMAT 固定格式（json/text/verbose_json）与非法值告警回落；
dict / 纯字符串响应的解析兜底。

打桩方式与 test_pipeline.py 的 openai 替身一致：monkeypatch ``openai.OpenAI``，
客户端结构 ``client.audio.transcriptions.create(**params)`` 与真实 SDK 对齐。
"""

import logging
from pathlib import Path

import pytest

from video_to_summary.transcribers.openai_whisper_api import OpenAIWhisperAPITranscriber

_LOGGER = "video_to_summary.transcribers.openai_whisper_api"


class FakeSeg:
    def __init__(self, start: float, end: float, text: str) -> None:
        self.start = start
        self.end = end
        self.text = text


class FakeVerboseResponse:
    """模拟 response_format=verbose_json 的响应：带 segments + text。"""

    def __init__(self, text: str, segments: list) -> None:
        self.text = text
        self.segments = segments


class FakeTextResponse:
    """模拟 response_format=json 的响应：openai SDK 3.x 返回 Transcription（仅 .text）。"""

    def __init__(self, text: str) -> None:
        self.text = text


class FakeAPIError(Exception):
    """模拟 openai APIStatusError：带 status_code 的普通异常，免真实请求。"""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def make_fake_openai(script: list):
    """按调用脚本构造 Fake OpenAI 客户端，并记录每次 create 的 kwargs。

    script 元素为 ``("ok", response)`` 或 ``("raise", exc)``，按调用顺序消费。
    返回 ``(FakeOpenAI, calls)``，calls 为每次 create 的 kwargs 列表。
    """
    calls: list[dict] = []

    class FakeTranscriptions:
        def create(self, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            action, payload = script.pop(0)
            if action == "ok":
                return payload
            raise payload

    class FakeAudio:
        def __init__(self) -> None:
            self.transcriptions = FakeTranscriptions()

    class FakeOpenAI:
        def __init__(self, **kwargs):  # type: ignore[no-untyped-def]
            self.audio = FakeAudio()

    return FakeOpenAI, calls


def _rejection_400(message: str) -> FakeAPIError:
    return FakeAPIError(f"Error code: 400 - {{'error': {{'message': {message!r}}}}}", 400)


def _audio(tmp_path: Path) -> Path:
    p = tmp_path / "a.wav"
    p.write_bytes(b"RIFF....WAVEfmt ")
    return p


def test_verbose_json_success_parses_segments(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """原有行为不回归：verbose_json 成功 → 解析出分段与带时间戳的行文本。"""
    segments = [FakeSeg(0.0, 1.0, "hello"), FakeSeg(1.0, 2.5, "world")]
    FakeOpenAI, calls = make_fake_openai([("ok", FakeVerboseResponse("hello world", segments))])
    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)

    transcriber = OpenAIWhisperAPITranscriber(api_key="sk-test", model="whisper-1")
    result = transcriber.transcribe(_audio(tmp_path))

    assert calls[0]["response_format"] == "verbose_json"
    assert [seg.text for seg in result.segments] == ["hello", "world"]
    assert result.segments[0].start == 0.0
    assert result.segments[1].end == 2.5
    assert result.text == "[00:00] hello\n[00:01] world"


def test_verbose_json_rejected_400_falls_back_to_json(monkeypatch, tmp_path, caplog) -> None:
    """端点 400 + 错误信息提及 response_format → 自动用 json 重试，返回仅文本结果并 WARNING。"""
    rejection = _rejection_400(
        "Request param: response_format is invalid, recommended val is: only support json,text"
    )
    FakeOpenAI, calls = make_fake_openai(
        [("raise", rejection), ("ok", FakeTextResponse("纯文本无分段"))]
    )
    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        transcriber = OpenAIWhisperAPITranscriber(api_key="sk-test")
        result = transcriber.transcribe(_audio(tmp_path))

    assert [c["response_format"] for c in calls] == ["verbose_json", "json"]
    assert result.text == "纯文本无分段"
    assert result.segments == []
    assert "已自动降级为 response_format=json" in caplog.text


def test_other_errors_are_raised_without_retry(monkeypatch, tmp_path) -> None:
    """401 认证错误 → 原样抛出，不发生重试/降级。"""
    auth_error = FakeAPIError("Error code: 401 - Incorrect API key provided", 401)
    FakeOpenAI, calls = make_fake_openai([("raise", auth_error)])
    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)

    transcriber = OpenAIWhisperAPITranscriber(api_key="sk-test")
    with pytest.raises(FakeAPIError) as excinfo:
        transcriber.transcribe(_audio(tmp_path))

    assert excinfo.value.status_code == 401
    assert len(calls) == 1
    assert calls[0]["response_format"] == "verbose_json"


def test_400_without_response_format_mention_not_downgraded(monkeypatch, tmp_path) -> None:
    """400 但错误信息不含 response_format（非参数格式问题）→ 原样抛出，不降级。"""
    other_400 = _rejection_400("bad language code")
    FakeOpenAI, calls = make_fake_openai([("raise", other_400)])
    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)

    transcriber = OpenAIWhisperAPITranscriber(api_key="sk-test")
    with pytest.raises(FakeAPIError):
        transcriber.transcribe(_audio(tmp_path))
    assert len(calls) == 1


def test_env_asr_response_format_json_uses_json_first(monkeypatch, tmp_path) -> None:
    """ASR_RESPONSE_FORMAT=json → 首次即用 json，不再尝试 verbose_json。"""
    monkeypatch.setenv("ASR_RESPONSE_FORMAT", "json")
    FakeOpenAI, calls = make_fake_openai([("ok", FakeTextResponse("固定 json 文本"))])
    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)

    transcriber = OpenAIWhisperAPITranscriber(api_key="sk-test")
    result = transcriber.transcribe(_audio(tmp_path))

    assert [c["response_format"] for c in calls] == ["json"]
    assert result.text == "固定 json 文本"
    assert result.segments == []


def test_explicit_text_response_format_parses_plain_string(monkeypatch, tmp_path) -> None:
    """显式 response_format=text：兼容端点直接返回字符串，解析为整段文本。"""
    FakeOpenAI, calls = make_fake_openai([("ok", "纯文本整段")])
    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)

    transcriber = OpenAIWhisperAPITranscriber(api_key="sk-test", response_format="text")
    result = transcriber.transcribe(_audio(tmp_path))

    assert calls[0]["response_format"] == "text"
    assert result.text == "纯文本整段"
    assert result.segments == []


def test_explicit_verbose_json_still_falls_back(monkeypatch, tmp_path) -> None:
    """显式 verbose_json 被拒时仍按规则 1 回退 json。"""
    rejection = _rejection_400("response_format is invalid")
    FakeOpenAI, calls = make_fake_openai([("raise", rejection), ("ok", FakeTextResponse("降级文本"))])
    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)

    transcriber = OpenAIWhisperAPITranscriber(api_key="sk-test", response_format="verbose_json")
    result = transcriber.transcribe(_audio(tmp_path))

    assert [c["response_format"] for c in calls] == ["verbose_json", "json"]
    assert result.text == "降级文本"
    assert result.segments == []


def test_invalid_env_response_format_warns_and_uses_default(monkeypatch, tmp_path, caplog) -> None:
    """非法 ASR_RESPONSE_FORMAT → WARN（不静默）并回落默认自动降级行为。"""
    monkeypatch.setenv("ASR_RESPONSE_FORMAT", "bogus")
    rejection = _rejection_400("response_format is invalid")
    FakeOpenAI, calls = make_fake_openai(
        [("raise", rejection), ("ok", FakeTextResponse("自动降级文本"))]
    )
    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        transcriber = OpenAIWhisperAPITranscriber(api_key="sk-test")

    assert transcriber.response_format is None
    assert "非法" in caplog.text

    result = transcriber.transcribe(_audio(tmp_path))
    assert [c["response_format"] for c in calls] == ["verbose_json", "json"]
    assert result.text == "自动降级文本"


def test_dict_response_parsed(monkeypatch, tmp_path) -> None:
    """兼容端点直接返回 dict（旧 SDK 形态）时同样解析分段/文本。"""
    FakeOpenAI, calls = make_fake_openai(
        [
            (
                "ok",
                {
                    "text": "字典文本",
                    "segments": [{"start": 0.0, "end": 1.0, "text": "字典分段"}],
                },
            )
        ]
    )
    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)

    transcriber = OpenAIWhisperAPITranscriber(api_key="sk-test")
    result = transcriber.transcribe(_audio(tmp_path))

    assert result.text == "[00:00] 字典分段"
    assert result.segments[0].text == "字典分段"


def test_language_passed_through(monkeypatch, tmp_path) -> None:
    """language 参数仍透传给转录请求。"""
    FakeOpenAI, calls = make_fake_openai([("ok", FakeTextResponse("文本"))])
    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)

    transcriber = OpenAIWhisperAPITranscriber(api_key="sk-test", language="zh")
    transcriber.transcribe(_audio(tmp_path))

    assert calls[0]["language"] == "zh"
    assert calls[0]["model"] == "whisper-1"
