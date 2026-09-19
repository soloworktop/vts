from pathlib import Path

import pytest

from video_to_summary.schemas import AudioMeta, TranscriptResult, TranscriptSegment
from video_to_summary.sources.base import Source
from video_to_summary.pipeline import run
from video_to_summary.polishers.llm import LLMTranscriptPolisher
from video_to_summary.summarizers.openai import OpenAISummarizer, get_summary_template, SUMMARY_TEMPLATES


class FakeSource:
    def __init__(self, audio_path: Path, meta: AudioMeta) -> None:
        self.audio_path = audio_path
        self.meta = meta

    def resolve(self) -> tuple[Path, AudioMeta]:
        return self.audio_path, self.meta


class FakeTranscriber:
    def __init__(self, result: TranscriptResult) -> None:
        self.result = result

    def transcribe(self, audio_path: Path, cancel_check=None) -> TranscriptResult:
        return self.result


def test_pipeline_generates_markdown(tmp_path: Path) -> None:
    fake_audio = tmp_path / "fake.wav"
    fake_audio.write_bytes(b"RIFF....WAVEfmt ")

    meta = AudioMeta(
        source_id="video123",
        title="Test Video",
        source_url="https://example.test/video",
        duration=60,
        uploader="tester",
        upload_date="20250101",
        audio_path=fake_audio,
    )

    transcript = TranscriptResult(
        text="hello world",
        segments=[TranscriptSegment(start=0.0, end=1.0, text="hello world")],
    )

    source = FakeSource(fake_audio, meta)
    transcriber = FakeTranscriber(transcript)
    summarizer = FakeSummarizer("summary")

    result = run(source, transcriber, summarizer, tmp_path)

    assert result.exists()
    assert result.suffix == ".md"
    content = result.read_text(encoding="utf-8")
    assert "Test Video" in content
    assert "summary" in content
    assert "hello world" not in content


def test_pipeline_writes_polished_transcript(tmp_path: Path) -> None:
    fake_audio = tmp_path / "fake.wav"
    fake_audio.write_bytes(b"RIFF....WAVEfmt ")

    meta = AudioMeta(
        source_id="video123",
        title="Test Video",
        source_url="https://example.test/video",
        duration=60,
        uploader="tester",
        upload_date="20250101",
        audio_path=fake_audio,
    )

    raw_text = "横止Beta是正点0.02，冯小刚电影"
    transcript = TranscriptResult(
        text=raw_text,
        segments=[TranscriptSegment(start=0.0, end=1.0, text=raw_text)],
    )

    source = FakeSource(fake_audio, meta)
    transcriber = FakeTranscriber(transcript)
    summarizer = FakeSummarizer("summary")

    polished_path = tmp_path / "video123.polished.txt"
    polisher = LLMTranscriptPolisher(api_key="sk-test", model="test-model")
    polisher.polish = lambda transcript_result, title="", cancel_check=None: TranscriptResult(
        text="恒指Beta是正点0.02，冯小刚电影",
        segments=[TranscriptSegment(start=0.0, end=1.0, text="恒指Beta是正点0.02，冯小刚电影")],
    )

    run(source, transcriber, summarizer, tmp_path, polisher=polisher)

    assert polished_path.exists()
    assert polished_path.read_text(encoding="utf-8") == "恒指Beta是正点0.02，冯小刚电影"


def test_pipeline_writes_srt_subtitle(tmp_path: Path) -> None:
    """转写阶段应额外输出标准 SRT 字幕文件，基于原始 segments 时间戳。"""
    fake_audio = tmp_path / "fake.wav"
    fake_audio.write_bytes(b"RIFF....WAVEfmt ")

    meta = AudioMeta(
        source_id="vid_srt",
        title="SRT Test",
        source_url="https://example.test/srt",
        duration=120,
        uploader="tester",
        upload_date="20250101",
        audio_path=fake_audio,
    )

    segments = [
        TranscriptSegment(start=0.0, end=2.5, text="第一句"),
        TranscriptSegment(start=2.5, end=5.0, text="第二句"),
        TranscriptSegment(start=61.0, end=62.0, text="一分钟后"),
    ]
    transcript = TranscriptResult(text="第一句\n第二句\n一分钟后", segments=segments)

    source = FakeSource(fake_audio, meta)
    transcriber = FakeTranscriber(transcript)
    summarizer = FakeSummarizer("summary")

    run(source, transcriber, summarizer, tmp_path)

    srt_path = tmp_path / "vid_srt.srt"
    assert srt_path.exists(), "SRT 字幕文件未生成"
    content = srt_path.read_text(encoding="utf-8")
    # 标准 SRT 结构：序号 + 时间轴 + 文本 + 空行
    assert content.startswith("1\n")
    assert "00:00:00,000 --> 00:00:02,500" in content
    assert "00:00:02,500 --> 00:00:05,000" in content
    assert "00:01:01,000 --> 00:01:02,000" in content
    assert "第一句" in content
    assert "第二句" in content
    assert "一分钟后" in content
    # 每条 cue 后应有空行分隔
    assert content.rstrip().endswith("一分钟后")


def test_pipeline_srt_skipped_when_no_segments(tmp_path: Path) -> None:
    """segments 为空时不生成 SRT 文件。"""
    fake_audio = tmp_path / "fake.wav"
    fake_audio.write_bytes(b"RIFF....WAVEfmt ")

    meta = AudioMeta(
        source_id="vid_empty",
        title="Empty",
        source_url=None,
        duration=10,
        uploader=None,
        upload_date=None,
        audio_path=fake_audio,
    )
    transcript = TranscriptResult(text="纯文本无分段", segments=[])

    source = FakeSource(fake_audio, meta)
    transcriber = FakeTranscriber(transcript)
    summarizer = FakeSummarizer("summary")

    run(source, transcriber, summarizer, tmp_path)
    assert not (tmp_path / "vid_empty.srt").exists()


def test_openai_summarizer_returns_required_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    required_sections = ["核心观点", "关键信息", "逻辑与观察"]

    class FakeMessage:
        content = (
            "## 核心观点\n"
            "- 观点1\n"
            "- 观点2\n\n"
            "## 关键信息\n"
            "- 信息1\n\n"
            "## 逻辑与观察\n"
            "- 观察1\n\n"
            "## 结论/行动\n"
            "- 结论1\n"
        )

    class FakeChoice:
        message = FakeMessage()

    class FakeCompletion:
        choices = [FakeChoice()]

    class FakeCompletions:
        def create(self, **kwargs):  # type: ignore
            return FakeCompletion()

    class FakeOpenAI:
        def __init__(self, **kwargs):  # type: ignore
            self.chat = type("chat", (), {"completions": FakeCompletions()})()

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)

    summarizer = OpenAISummarizer(api_key="sk-test", model="test-model")
    summary = summarizer.summarize("any transcript", title="any title")

    for section in required_sections:
        assert section in summary


def test_openai_summarizer_supports_custom_template_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    custom_template = {
        "sections": ["自定义章节"],
        "optional_sections": [],
        "hints": {"自定义章节": "测试"},
    }

    class FakeMessage:
        content = "## 自定义章节\n- 内容1\n"

    class FakeChoice:
        message = FakeMessage()

    class FakeCompletion:
        choices = [FakeChoice()]

    class FakeCompletions:
        def create(self, **kwargs):  # type: ignore
            return FakeCompletion()

    class FakeOpenAI:
        def __init__(self, **kwargs):  # type: ignore
            self.chat = type("chat", (), {"completions": FakeCompletions()})()

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)

    summarizer = OpenAISummarizer(api_key="sk-test", model="test-model", template=custom_template)
    summary = summarizer.summarize("any transcript", title="any title")

    assert "自定义章节" in summary


def test_summary_template_default_is_universal_prompt() -> None:
    """默认模板「通用」= 单段提示词（提炼结论/主题分节/保留关键信息），排版由契约保证；
    历史名 "default" 作为兼容别名仍解析到同一模板。"""
    template = get_summary_template("通用")
    assert template["prompt"].startswith("用通用笔记风格总结")
    assert "不要流水账" in template["prompt"]
    assert get_summary_template("default") == template
    assert get_summary_template("") == template


def test_default_system_prompt_uses_prompt_with_style_contract() -> None:
    from video_to_summary.summarizers.openai import _build_system_prompt

    prompt = _build_system_prompt(get_summary_template("通用"))
    assert "任务：用通用笔记风格总结" in prompt
    assert "不要流水账" in prompt
    # 排版契约关键要素：emoji 小标题 / 加粗 / 引用块结论 / 受限行内着色
    assert "emoji" in prompt
    assert "**加粗**" in prompt
    assert "> 🎯" in prompt
    assert '<span style="color:#d1544a">' in prompt
    # 单段提示词模型不应出现章节编号结构
    assert "输出必须严格按下列结构整理" not in prompt


def test_legacy_sections_system_prompt_keeps_structure_and_contract() -> None:
    """旧三层结构（sections/hints）仍受支持（兼容未迁移 dict 与旧调用方），且追加排版契约。"""
    from video_to_summary.summarizers.openai import _build_system_prompt

    prompt = _build_system_prompt({"sections": ["持仓与交易"], "optional_sections": [], "hints": {"持仓与交易": "整理持仓变动"}})
    assert "输出必须严格按下列结构整理" in prompt
    assert "持仓与交易" in prompt
    assert "排版要求" in prompt


def test_custom_prompt_template_flows_into_system_prompt() -> None:
    """自定义模板（一段提示词）直接注入 system prompt。"""
    from video_to_summary.summarizers.openai import _build_system_prompt

    prompt = _build_system_prompt({"prompt": "用投资备忘录风格总结，重点写持仓变化"})
    assert "任务：用投资备忘录风格总结，重点写持仓变化" in prompt
    assert "emoji" in prompt


def test_builtin_templates_cover_bilinote_styles() -> None:
    """内置模板 = default + 借鉴 BiliNote 的九种笔记风格，均为单段提示词模型。

    声明序即展示序（list_templates 按此返回）：通用/摘要高频在前，
    学习类（教程/学术/会议）居中，创作与垂类（商业/小红书/生活/任务）靠后。
    """
    from video_to_summary.summarizers.openai import SUMMARY_TEMPLATES

    assert list(SUMMARY_TEMPLATES.keys()) == [
        "通用", "精简笔记", "详细笔记", "教程笔记", "学术笔记",
        "会议纪要", "商业分析", "小红书笔记", "生活随笔", "任务清单",
    ]
    for template in SUMMARY_TEMPLATES.values():
        assert set(template.keys()) == {"prompt"}
        assert template["prompt"].strip()


def test_get_summary_template_raises_for_invalid_name() -> None:
    with pytest.raises(ValueError):
        get_summary_template("not_exist")


def test_pipeline_emits_stage_events(tmp_path: Path) -> None:
    fake_audio = tmp_path / "fake.wav"
    fake_audio.write_bytes(b"RIFF....WAVEfmt ")

    meta = AudioMeta(
        source_id="video123",
        title="Test Video",
        source_url="https://example.test/video",
        duration=60,
        uploader="tester",
        upload_date="20250101",
        audio_path=fake_audio,
    )
    transcript = TranscriptResult(text="hello world", segments=[])

    polisher = LLMTranscriptPolisher(api_key="sk-test", model="test-model")
    polisher.polish = lambda transcript_result, title="", cancel_check=None: TranscriptResult(
        text="polished text", segments=[]
    )

    events: list[tuple[str, dict]] = []
    run(
        FakeSource(fake_audio, meta),
        FakeTranscriber(transcript),
        FakeSummarizer("summary"),
        tmp_path,
        polisher=polisher,
        on_event=lambda event, payload: events.append((event, payload)),
    )

    assert [e for e, _ in events] == [
        "download_start",
        "download_done",
        "transcribe_start",
        "transcribe_done",
        "polish_start",
        "polish_done",
        "summarize_start",
        "summarize_done",
    ]
    download_done = next(p for e, p in events if e == "download_done")
    assert download_done["title"] == "Test Video"
    assert download_done["duration"] == 60

    # 本次真实消耗字段：fresh 转写带真实送 ASR 的音频时长（假音频探测失败为 None）；
    # 总结带输入/输出字数（本用例含 polish 环节，总结输入 = 优化后文本）
    transcribe_done = next(p for e, p in events if e == "transcribe_done")
    assert "audio_seconds" in transcribe_done  # 探测失败回落 None，键必须存在
    summarize_done = next(p for e, p in events if e == "summarize_done")
    assert summarize_done["input_chars"] == len("polished text")
    assert summarize_done["output_chars"] == len("summary")

    # 二次运行命中缓存，事件仍完整且带 cached 标记
    events_cached: list[tuple[str, dict]] = []
    run(
        FakeSource(fake_audio, meta),
        FakeTranscriber(transcript),
        FakeSummarizer("summary"),
        tmp_path,
        polisher=polisher,
        on_event=lambda event, payload: events_cached.append((event, payload)),
    )
    transcribe_done = next(p for e, p in events_cached if e == "transcribe_done")
    polish_done = next(p for e, p in events_cached if e == "polish_done")
    assert transcribe_done.get("cached") is True
    assert polish_done.get("cached") is True
    # 缓存命中未走 ASR：不带音频时长（本次真实消耗口径）
    assert "audio_seconds" not in transcribe_done


def test_pipeline_event_callback_error_does_not_break_run(tmp_path: Path) -> None:
    fake_audio = tmp_path / "fake.wav"
    fake_audio.write_bytes(b"RIFF....WAVEfmt ")
    meta = AudioMeta(
        source_id="video123",
        title="Test Video",
        source_url=None,
        duration=None,
        uploader=None,
        upload_date=None,
        audio_path=fake_audio,
    )
    transcript = TranscriptResult(text="hello world", segments=[])

    def bad_callback(event: str, payload: dict) -> None:
        raise RuntimeError("callback boom")

    result = run(
        FakeSource(fake_audio, meta),
        FakeTranscriber(transcript),
        FakeSummarizer("summary"),
        tmp_path,
        on_event=bad_callback,
    )
    assert result.exists()


class FakeSummarizer:
    def __init__(self, summary: str) -> None:
        self.summary = summary
        self.model = "fake-summarizer"

    def summarize(self, transcript: str, title: str = "", cancel_check=None) -> str:
        return self.summary


def test_url_scheme_allowlist():
    """只允许 http/https：拒绝 file:// 与空 scheme，防止本地协议注入。"""
    from video_to_summary.sources.url import URLAudioSource

    for bad in ("file:///etc/passwd", "ftp://x.com/a.mp4", "javascript:alert(1)", "  ", ""):
        try:
            URLAudioSource(bad, output_dir=".", audio_format="wav")
            raised = False
        except ValueError:
            raised = True
        assert raised, f"应拒绝 scheme: {bad!r}"

    # 合法输入不受影响
    URLAudioSource("https://www.bilibili.com/video/BV1xx", output_dir=".", audio_format="wav")
    URLAudioSource("http://example.test/a.mp3", output_dir=".", audio_format="wav")


def test_pipeline_subtitle_path_emits_title(tmp_path: Path) -> None:
    """字幕优先路径：subtitle_done 事件携带 title/duration（供 Web 层回写任务标题），
    且不再触发 download_done（跳过下载）。"""
    from video_to_summary.config import SubtitleConfig
    from video_to_summary.subtitles import SubtitleResult

    fake_audio = tmp_path / "fake.wav"
    fake_audio.write_bytes(b"RIFF....WAVEfmt ")

    meta = AudioMeta(
        source_id="vid_sub",
        title="字幕路径真实标题",
        source_url="https://example.test/sub",
        duration=300,
        uploader="tester",
        upload_date=None,
        audio_path=fake_audio,
    )
    transcript = TranscriptResult(
        text="字幕文本",
        segments=[TranscriptSegment(start=0.0, end=1.0, text="字幕文本")],
    )

    class FakeSubtitleSource:
        def __init__(self) -> None:
            self.meta = meta

        def resolve(self):  # pragma: no cover - 字幕命中时不应被调用
            raise AssertionError("字幕路径不应回退到 resolve()")

        def extract_subtitle(self, config):
            return SubtitleResult(
                transcript=transcript,
                meta=meta,
                subtitle_path=fake_audio,
                language="zh-Hans",
                automatic=False,
            )

    events: list[tuple[str, dict]] = []
    result = run(
        FakeSubtitleSource(),
        FakeTranscriber(transcript),
        FakeSummarizer("summary"),
        tmp_path,
        on_event=lambda event, payload: events.append((event, payload)),
        subtitle_config=SubtitleConfig(),
    )

    assert result.exists()
    names = [e for e, _ in events]
    assert "subtitle_done" in names
    assert "download_done" not in names
    payload = dict(events)["subtitle_done"]
    assert payload["title"] == "字幕路径真实标题"
    assert payload["duration"] == 300
    # 总结内容不含模型信息（转写/摘要模型行已移除）
    content = result.read_text(encoding="utf-8")
    assert "字幕路径真实标题" in content
    assert "转写模型" not in content
    assert "摘要模型" not in content


# ---------------------------------------------------------------- 缓存语义（P1-4）

class _CountingTranscriber:
    """带调用计数的转写器（验证「文件存在即命中缓存」不重复调 ASR）。"""

    def __init__(self, result: TranscriptResult) -> None:
        self.result = result
        self.calls = 0

    def transcribe(self, audio_path: Path, cancel_check=None) -> TranscriptResult:
        self.calls += 1
        return self.result


class _CountingSummarizer:
    """带调用计数的总结器。"""

    def __init__(self, summary: str) -> None:
        self.summary = summary
        self.calls = 0

    def summarize(self, transcript: str, title: str = "", cancel_check=None) -> str:
        self.calls += 1
        return self.summary


def _make_meta(audio_path: Path) -> AudioMeta:
    return AudioMeta(
        source_id="cachevid",
        title="Cache Video",
        source_url="https://example.test/cache",
        duration=30,
        uploader="tester",
        audio_path=audio_path,
    )


def test_pipeline_transcript_cached_summary_regenerated(tmp_path: Path) -> None:
    """缓存语义矩阵（P1-4）：
    - 转写产物（.txt）存在 → 转写器不再调用（transcribe_done 带 cached=True）；
    - summary **无缓存语义** → 总结器每次真实重跑——模板/配置变化后重跑绝不会
      误用旧 summary（pipeline 始终重新生成并覆盖写盘）。
    """
    fake_audio = tmp_path / "a.wav"
    fake_audio.write_bytes(b"x")
    meta = _make_meta(fake_audio)
    source = FakeSource(fake_audio, meta)

    transcriber = _CountingTranscriber(
        TranscriptResult(text="transcript v1", segments=[TranscriptSegment(0.0, 1.0, "v1")])
    )
    summarizer = _CountingSummarizer("summary v1")

    out_dir = tmp_path / "out"
    summary1 = run(source, transcriber, summarizer, out_dir)
    assert transcriber.calls == 1 and summarizer.calls == 1
    assert "summary v1" in summary1.read_text(encoding="utf-8")

    # 第二次运行（同输出目录）：转写命中缓存，总结重新生成
    summarizer.summary = "summary v2 (fresh config)"
    summary2 = run(source, transcriber, summarizer, out_dir)
    assert transcriber.calls == 1, "转写产物已存在，不得重复调用 ASR"
    assert summarizer.calls == 2, "summary 每次都必须重新生成（无缓存）"
    assert "summary v2 (fresh config)" in summary2.read_text(encoding="utf-8")
    assert "summary v1" not in summary2.read_text(encoding="utf-8"), "旧 summary 不得残留"


def test_pipeline_cache_miss_after_output_dir_cleared(tmp_path: Path) -> None:
    """retry 语义的管线侧保证：输出目录被清空（web 层 retry 行为）后，
    转写与总结都从头执行，不会复用旧 transcript / 旧 summary。"""
    fake_audio = tmp_path / "a.wav"
    fake_audio.write_bytes(b"x")
    source = FakeSource(fake_audio, _make_meta(fake_audio))
    transcriber = _CountingTranscriber(TranscriptResult(text="v1", segments=[]))
    summarizer = _CountingSummarizer("summary v1")
    out_dir = tmp_path / "out"
    run(source, transcriber, summarizer, out_dir)

    # 模拟 web 层 retry：清空输出目录
    import shutil

    shutil.rmtree(out_dir)
    transcriber.result = TranscriptResult(text="v2", segments=[])
    summarizer.summary = "summary v2"
    summary = run(source, transcriber, summarizer, out_dir)

    assert transcriber.calls == 2, "目录清空后不得命中旧转写缓存"
    summary_text = summary.read_text(encoding="utf-8")
    assert "summary v2" in summary_text and "summary v1" not in summary_text
    assert "v2" in (out_dir / "cachevid.txt").read_text(encoding="utf-8")
