"""字幕解析与「字幕优先」流水线路径测试。

覆盖：
- VTT / SRT / JSON3 三种格式解析与标签/实体清理；
- pipeline 用视频自带字幕时跳过「下载音频 + 转写」，并正确产出文件；
- 无可用字幕时回退到「下载 + 转写」。
"""

from pathlib import Path

import pytest

from video_to_summary.config import SubtitleConfig
from video_to_summary.constants import SubtitlePreference
from video_to_summary.pipeline import run
from video_to_summary.schemas import AudioMeta, TranscriptResult, TranscriptSegment
from video_to_summary.subtitles import (
    SubtitleResult,
    parse_json3,
    parse_srt,
    parse_subtitle_file,
    parse_vtt,
)

SAMPLE_VTT = """WEBVTT

00:00:00.500 --> 00:00:02.000
大家好，欢迎收看本期视频

00:00:02.500 --> 00:00:05.000 align:start position:0%
<v 主播>这里是<00:00:03.000>带标签的</c>文本&nbsp;&amp;

NOTE 这是注释，不应被解析

00:00:06.000 --> 00:00:08.000
第三句
"""

SAMPLE_SRT = """1
00:00:00,500 --> 00:00:02,000
你好，世界

2
00:00:02,500 --> 00:00:04,000
第二行字幕
"""

SAMPLE_JSON3 = """{
  "events": [
    {"tStartMs": 500, "tEndMs": 2000, "segs": [{"utf8": "hello "}, {"utf8": "world"}]},
    {"tStartMs": 2500, "tEndMs": 4000, "segs": [{"utf8": "second line"}]}
  ]
}
"""


def test_parse_vtt_basic() -> None:
    segments = parse_vtt(SAMPLE_VTT)
    assert len(segments) == 3
    assert segments[0].start == pytest.approx(0.5)
    assert segments[0].end == pytest.approx(2.0)
    assert segments[0].text == "大家好，欢迎收看本期视频"
    # 标签/实体清理：<v 主播>、行内时间戳、&nbsp;、&amp; 都被处理
    assert segments[1].text == "这里是带标签的文本 &"
    assert segments[2].text == "第三句"


def test_parse_srt_basic() -> None:
    segments = parse_srt(SAMPLE_SRT)
    assert len(segments) == 2
    assert segments[0].start == pytest.approx(0.5)
    assert segments[0].text == "你好，世界"
    assert segments[1].text == "第二行字幕"


def test_parse_json3_basic() -> None:
    segments = parse_json3(SAMPLE_JSON3)
    assert len(segments) == 2
    assert segments[0].start == pytest.approx(0.5)
    assert segments[0].text == "hello world"
    assert segments[1].text == "second line"


def test_parse_subtitle_file_dispatches_by_ext(tmp_path: Path) -> None:
    vtt = tmp_path / "id.zh-Hans.vtt"
    vtt.write_text(SAMPLE_VTT, encoding="utf-8")
    srt = tmp_path / "id.en.srt"
    srt.write_text(SAMPLE_SRT, encoding="utf-8")
    j3 = tmp_path / "id.en.json3"
    j3.write_text(SAMPLE_JSON3, encoding="utf-8")

    assert parse_subtitle_file(vtt).segments
    assert parse_subtitle_file(srt).segments
    assert parse_subtitle_file(j3).text == "hello world\nsecond line"


# ---------- pipeline 字幕优先路径 ----------

class FakeSubtitleSource:
    """带 extract_subtitle 的伪 Source：resolve 不应被调用（字幕路径跳过下载）。"""

    def __init__(self, meta: AudioMeta, result: SubtitleResult | None) -> None:
        self.meta = meta
        self._result = result
        self.resolve_called = False

    def resolve(self) -> tuple[Path, AudioMeta]:
        self.resolve_called = True
        return Path("/nonexistent/audio.wav"), self.meta

    def extract_subtitle(self, config: SubtitleConfig) -> SubtitleResult | None:
        return self._result


class FakeTranscriber:
    def __init__(self, result: TranscriptResult) -> None:
        self.result = result
        self.transcribe_called = False

    def transcribe(self, audio_path: Path, cancel_check=None) -> TranscriptResult:
        self.transcribe_called = True
        return self.result


class FakeSummarizer:
    def __init__(self, summary: str) -> None:
        self.summary = summary
        self.model = "fake-summarizer"

    def summarize(self, transcript: str, title: str = "", cancel_check=None) -> str:
        return self.summary


def _subtitle_result(tmp_path: Path, lang: str = "zh-Hans") -> SubtitleResult:
    sub_file = tmp_path / f"video1.{lang}.vtt"
    sub_file.write_text(SAMPLE_VTT, encoding="utf-8")
    meta = AudioMeta(
        source_id="video1",
        title="字幕视频",
        source_url="https://example.test/v",
        duration=60,
        audio_path=sub_file,
    )
    return SubtitleResult(
        transcript=parse_subtitle_file(sub_file),
        meta=meta,
        subtitle_path=sub_file,
        language=lang,
        automatic=False,
    )


def test_pipeline_uses_subtitle_and_skips_download_transcribe(tmp_path: Path) -> None:
    result = _subtitle_result(tmp_path)
    source = FakeSubtitleSource(result.meta, result)
    transcriber = FakeTranscriber(TranscriptResult(text="不应被使用", segments=[]))
    summarizer = FakeSummarizer("summary")

    events: list[tuple[str, dict]] = []
    summary_path = run(
        source,
        transcriber,
        summarizer,
        tmp_path,
        on_event=lambda e, p: events.append((e, p)),
        subtitle_config=SubtitleConfig(preference=SubtitlePreference.AUTO, language="auto"),
    )

    # 未触发下载与转写
    assert source.resolve_called is False
    assert transcriber.transcribe_called is False

    event_names = [e for e, _ in events]
    assert "subtitle_start" in event_names
    assert "subtitle_done" in event_names
    assert "download_start" not in event_names
    assert "transcribe_start" not in event_names

    # 转写文本已落盘，字幕文本进入摘要流程
    assert (tmp_path / "video1.txt").exists()
    content = summary_path.read_text(encoding="utf-8")
    assert "字幕视频" in content
    assert "summary" in content


def test_pipeline_falls_back_when_no_subtitle(tmp_path: Path) -> None:
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF....WAVEfmt ")
    meta = AudioMeta(source_id="video1", title="无字幕视频", source_url="https://example.test/v", audio_path=audio)
    source = FakeSubtitleSource(meta, None)  # extract_subtitle 返回 None
    transcriber = FakeTranscriber(TranscriptResult(text="转写文本", segments=[]))
    summarizer = FakeSummarizer("summary")

    events: list[tuple[str, dict]] = []
    run(
        source,
        transcriber,
        summarizer,
        tmp_path,
        on_event=lambda e, p: events.append((e, p)),
        subtitle_config=SubtitleConfig(preference=SubtitlePreference.AUTO, language="auto"),
    )

    event_names = [e for e, _ in events]
    assert "subtitle_skipped" in event_names
    assert "download_start" in event_names
    assert "transcribe_start" in event_names
    assert transcriber.transcribe_called is True
    assert (tmp_path / "video1.txt").read_text(encoding="utf-8") == "转写文本"


def test_pipeline_subtitle_off_skips_extraction(tmp_path: Path) -> None:
    result = _subtitle_result(tmp_path)
    source = FakeSubtitleSource(result.meta, result)
    transcriber = FakeTranscriber(TranscriptResult(text="转写文本", segments=[]))
    summarizer = FakeSummarizer("summary")

    events: list[tuple[str, dict]] = []
    run(
        source,
        transcriber,
        summarizer,
        tmp_path,
        on_event=lambda e, p: events.append((e, p)),
        subtitle_config=SubtitleConfig(preference=SubtitlePreference.OFF, language="auto"),
    )
    event_names = [e for e, _ in events]
    assert "subtitle_start" not in event_names
    assert "download_start" in event_names


# ---------- URLAudioSource.extract_subtitle（伪 yt-dlp） ----------

def _patch_ydl(monkeypatch, tmp_path: Path, video_id: str, files: list[tuple[str, str, bool]], *, fail_langs: set | None = None, inline: bool = False):
    """伪 yt-dlp：extract_info(download=False) 返回语言清单，urlopen 返回字幕内容。

    files: [(lang, ext, is_auto), ...] —— 语言清单按 is_auto 归类到
    subtitles / automatic_captions，轨道 ext 原样保留；
    fail_langs 中的语言在 urlopen 时抛 429 HTTPError（模拟平台限流）。
    inline=True 时轨道以 data 内联内容形态给出（B 站提取器形态，无 url）。
    返回 (captured, fetched)：captured.opts 为收到的 ydl_opts，fetched 为按序
    被拉取的语言列表（断言「单次只拉一个语言」用）。
    """
    captured: dict = {}
    fetched: list[str] = []
    fail_langs = fail_langs or set()

    class FakeHTTPError(Exception):
        def __init__(self, status: int) -> None:
            super().__init__(f"HTTP Error {status}: Too Many Requests")
            self.status = status

    class FakeResponse:
        def read(self) -> bytes:
            return SAMPLE_VTT.encode("utf-8")

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts
            captured["opts"] = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            # 新实现两段式：元信息阶段绝不触发下载副作用
            assert download is False, "字幕提取的 extract_info 不应触发下载"
            captured["invoked"] = True

            def tracks(langs):
                if inline:
                    # B 站形态：SRT 文本直接内联在 data 字段，无 url
                    return {
                        lang: [{"ext": ext, "data": SAMPLE_SRT}]
                        for lang, ext, _ in langs
                    }
                return {
                    lang: [{"ext": ext, "url": f"https://sub.test/{video_id}/{lang}"}]
                    for lang, ext, _ in langs
                }

            # 复刻真实 yt-dlp 的门控（common.py::extract_subtitles/_automatic_captions）：
            # 仅当 ydl_opts 开启 writesubtitles / writeautomaticsub 时才返回轨道清单，
            # 否则恒为空 dict——曾经的回归正是漏了这两个开关，导致所有视频 "no usable subtitle"
            return {
                "id": video_id,
                "title": "测试视频",
                "subtitles": tracks([f for f in files if not f[2]]) if self.opts.get("writesubtitles") else {},
                "automatic_captions": tracks([f for f in files if f[2]]) if self.opts.get("writeautomaticsub") else {},
            }

        def urlopen(self, url):
            lang = str(url).rsplit("/", 1)[-1]
            fetched.append(lang)
            if lang in fail_langs:
                raise FakeHTTPError(429)
            return FakeResponse()

    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    # 候选间退避在测试里不需要真实等待
    monkeypatch.setattr("video_to_summary.sources.url._SUBTITLE_FETCH_SLEEP", 0.0)
    return captured, fetched


def _make_source(tmp_path: Path) -> "object":
    from video_to_summary.sources.url import URLAudioSource

    return URLAudioSource("https://example.test/v", output_dir=tmp_path)


def test_extract_subtitle_excludes_danmaku_and_picks_zh(tmp_path: Path, monkeypatch) -> None:
    _, fetched = _patch_ydl(monkeypatch, tmp_path, "v1", [("danmaku", "vtt", False), ("zh-Hans", "vtt", False)])
    res = _make_source(tmp_path).extract_subtitle(
        SubtitleConfig(preference=SubtitlePreference.AUTO, language="auto")
    )
    assert res is not None
    assert res.language == "zh-Hans"
    assert res.automatic is False
    assert "大家好" in res.transcript.text
    assert fetched == ["zh-Hans"]


def test_extract_subtitle_opts_enable_subtitle_listing(tmp_path: Path, monkeypatch) -> None:
    """回归：yt-dlp 仅在 writesubtitles/writeautomaticsub 开启时返回字幕清单。

    yt-dlp（common.py::extract_subtitles/extract_automatic_captions）对 extract_info
    的两份字幕清单做了参数门控；两段式重写时漏传这两个开关曾导致所有平台
    字幕提取恒空（表现为任务回退「下载+转写」）。_patch_ydl 的伪 extract_info
    已同步复刻该门控，因此上面的常规用例本身也是本回归的守卫。
    """
    captured, _ = _patch_ydl(monkeypatch, tmp_path, "v1", [("zh-Hans", "vtt", False)])
    res = _make_source(tmp_path).extract_subtitle(
        SubtitleConfig(preference=SubtitlePreference.AUTO, language="auto")
    )
    assert res is not None
    assert captured["opts"]["writesubtitles"] is True
    assert captured["opts"]["writeautomaticsub"] is True


def test_extract_subtitle_bilibili_inline_data_track(tmp_path: Path, monkeypatch) -> None:
    """B 站形态：字幕内容内联在轨道 data 字段（无 url），应直接采用、零额外请求。

    回归背景：_pick_track 曾只认 url 轨道（YouTube 形态），B 站所有字幕轨道
    被静默过滤，即使登录也 "no usable subtitle"。
    """
    _, fetched = _patch_ydl(
        monkeypatch, tmp_path, "BV1test", [("ai-zh", "srt", True)], inline=True,
    )
    res = _make_source(tmp_path).extract_subtitle(
        SubtitleConfig(preference=SubtitlePreference.AUTO, language="auto")
    )
    assert res is not None
    assert res.language == "ai-zh"
    assert res.automatic is True
    assert fetched == []  # 内联内容不应触发任何 urlopen 请求
    assert "你好，世界" in res.transcript.text
    assert res.subtitle_path.suffix == ".srt"


def test_extract_subtitle_bilibili_no_cookies_skip_reason(tmp_path: Path, monkeypatch) -> None:
    """B 站无 cookies 时，跳过原因应点名「配置 cookies」（而非泛泛的 no usable subtitle）。

    开源版不提供扫码登录，引导必须指向本版真实可用的解法。
    """
    from video_to_summary.sources.url import URLAudioSource

    _patch_ydl(monkeypatch, tmp_path, "v1", [])
    source = URLAudioSource("https://www.bilibili.com/video/BV1xx", output_dir=tmp_path)
    res = source.extract_subtitle(
        SubtitleConfig(preference=SubtitlePreference.AUTO, language="auto")
    )
    assert res is None
    assert "cookies" in source.subtitle_skip_reason


def test_extract_subtitle_skip_reason_default_for_non_bilibili(tmp_path: Path, monkeypatch) -> None:
    _patch_ydl(monkeypatch, tmp_path, "v1", [])
    source = _make_source(tmp_path)
    res = source.extract_subtitle(
        SubtitleConfig(preference=SubtitlePreference.AUTO, language="auto")
    )
    assert res is None
    assert source.subtitle_skip_reason == "no usable subtitle"


def test_extract_subtitle_manual_beats_auto(tmp_path: Path, monkeypatch) -> None:
    # 人工英文字幕 vs 自动中文字幕：人工优先，且只拉取最终选中的那一个语言
    _, fetched = _patch_ydl(monkeypatch, tmp_path, "v1", [("en", "vtt", False), ("zh-Hans", "vtt", True)])
    res = _make_source(tmp_path).extract_subtitle(
        SubtitleConfig(preference=SubtitlePreference.AUTO, language="auto")
    )
    assert res is not None
    assert res.language == "en"
    assert res.automatic is False
    assert fetched == ["en"]


def test_extract_subtitle_single_fetch_no_burst(tmp_path: Path, monkeypatch) -> None:
    """核心回归：多语言可用时也只拉取最优单个字幕（YouTube timedtext 批量连发会 429）。"""
    _, fetched = _patch_ydl(
        monkeypatch, tmp_path, "v1",
        [("zh-HK", "vtt", False), ("en", "vtt", False), ("zh-Hans", "vtt", True), ("ja", "vtt", False)],
    )
    res = _make_source(tmp_path).extract_subtitle(
        SubtitleConfig(preference=SubtitlePreference.AUTO, language="auto")
    )
    assert res is not None
    assert res.language == "zh-HK"
    assert fetched == ["zh-HK"]  # en/ja/自动 zh-Hans 均不发起请求


def test_extract_subtitle_auto_rate_limited_falls_back(tmp_path: Path, monkeypatch) -> None:
    """首选自动字幕被 429 限流 → 退避后换下一个候选继续尝试。"""
    _, fetched = _patch_ydl(
        monkeypatch, tmp_path, "v1",
        [("zh-Hans", "vtt", True), ("en", "vtt", True)],
        fail_langs={"zh-Hans"},
    )
    res = _make_source(tmp_path).extract_subtitle(
        SubtitleConfig(preference=SubtitlePreference.AUTO, language="auto")
    )
    assert res is not None
    assert res.language == "en"
    assert res.automatic is True
    assert fetched == ["zh-Hans", "en"]


def test_extract_subtitle_all_rate_limited_returns_none(tmp_path: Path, monkeypatch) -> None:
    """全部候选限流 → 返回 None（pipeline 回退下载+转写），且尝试数有上限。"""
    _, fetched = _patch_ydl(
        monkeypatch, tmp_path, "v1",
        [("zh-Hans", "vtt", True), ("en", "vtt", True), ("zh-HK", "vtt", True), ("ja", "vtt", True)],
        fail_langs={"zh-Hans", "en", "zh-HK", "ja"},
    )
    res = _make_source(tmp_path).extract_subtitle(
        SubtitleConfig(preference=SubtitlePreference.AUTO, language="auto")
    )
    assert res is None
    assert len(fetched) == 3  # _SUBTITLE_MAX_ATTEMPTS 上限，不无限重试


def test_extract_subtitle_language_preference(tmp_path: Path, monkeypatch) -> None:
    # 显式偏好英文：人工英文优先于人工中文
    _, fetched = _patch_ydl(monkeypatch, tmp_path, "v1", [("zh-Hans", "vtt", False), ("en", "vtt", False)])
    res = _make_source(tmp_path).extract_subtitle(
        SubtitleConfig(preference=SubtitlePreference.AUTO, language="en")
    )
    assert res is not None
    assert res.language == "en"
    assert fetched == ["en"]


def test_extract_subtitle_manual_only_rejects_auto_captions(tmp_path: Path, monkeypatch) -> None:
    captured, fetched = _patch_ydl(monkeypatch, tmp_path, "v1", [("en", "vtt", True)])
    res = _make_source(tmp_path).extract_subtitle(
        SubtitleConfig(preference=SubtitlePreference.MANUAL_ONLY, language="auto")
    )
    assert res is None
    # manual_only 时自动字幕轨道完全不发请求
    assert fetched == []
    assert captured.get("invoked") is True


def test_extract_subtitle_off_does_not_invoke_ytdl(tmp_path: Path, monkeypatch) -> None:
    captured, fetched = _patch_ydl(monkeypatch, tmp_path, "v1", [("zh-Hans", "vtt", False)])
    res = _make_source(tmp_path).extract_subtitle(
        SubtitleConfig(preference=SubtitlePreference.OFF, language="auto")
    )
    assert res is None
    assert "invoked" not in captured
    assert fetched == []


def test_extract_subtitle_danmaku_only_returns_none(tmp_path: Path, monkeypatch) -> None:
    _patch_ydl(monkeypatch, tmp_path, "v1", [("danmaku", "vtt", False)])
    res = _make_source(tmp_path).extract_subtitle(
        SubtitleConfig(preference=SubtitlePreference.AUTO, language="auto")
    )
    assert res is None


# ---------- cookies 配置解析（cookiefile 与 cookiesfrombrowser 互斥） ----------

def test_resolve_cookie_opts_priority(tmp_path: Path) -> None:
    """显式 cookies 文件 > 浏览器 cookies；都未配置时不设置任何 cookie 键。"""
    from video_to_summary.sources.url import _resolve_cookie_opts

    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

    opts = _resolve_cookie_opts("https://www.youtube.com/watch?v=1", cookie_file, "chrome")
    assert opts == {"cookiefile": str(cookie_file)}

    # 浏览器名大小写归一，yt-dlp API 形态为 1 元组
    assert _resolve_cookie_opts("https://www.youtube.com/watch?v=1", None, "Chrome") == {
        "cookiesfrombrowser": ("chrome",)
    }
    assert _resolve_cookie_opts("https://www.youtube.com/watch?v=1", None, "") == {}


def test_resolve_cookie_opts_explicit_file_beats_browser(tmp_path: Path) -> None:
    """显式 cookies 文件优先于浏览器 cookies（两者互斥，绝不并存）。"""
    from video_to_summary.sources.url import _resolve_cookie_opts

    cookie_file = tmp_path / "bili_cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

    opts = _resolve_cookie_opts("https://www.bilibili.com/video/BV1xx", cookie_file, "chrome")
    assert opts == {"cookiefile": str(cookie_file)}

    # 未给文件时按浏览器 cookies（大小写归一为 yt-dlp 的 1 元组形态）
    opts = _resolve_cookie_opts("https://www.bilibili.com/video/BV1xx", None, "chrome")
    assert opts == {"cookiesfrombrowser": ("chrome",)}


def test_resolve_cookies_never_touches_site_session(tmp_path: Path) -> None:
    """开源版不做站点会话注入：_resolve_cookies 是纯透传（无 cookies 参数即空）。"""
    from video_to_summary.sources.url import _resolve_cookies

    assert _resolve_cookies("https://www.bilibili.com/video/BV1xx", None) is None
    cookie_file = tmp_path / "c.txt"
    assert _resolve_cookies("https://www.bilibili.com/video/BV1xx", cookie_file) == cookie_file


def test_extract_subtitle_passes_browser_cookies_to_ydl(tmp_path: Path, monkeypatch) -> None:
    """cookies_browser 透传进 ydl_opts：cookiesfrombrowser 形态且不与 cookiefile 并存。"""
    from video_to_summary.sources.url import URLAudioSource

    captured, _ = _patch_ydl(monkeypatch, tmp_path, "v1", [("zh-Hans", "vtt", True)])
    src = URLAudioSource(
        "https://www.youtube.com/watch?v=v1", output_dir=tmp_path, cookies_browser="Chrome"
    )
    res = src.extract_subtitle(
        SubtitleConfig(preference=SubtitlePreference.AUTO, language="auto")
    )
    assert res is not None
    assert captured["opts"]["cookiesfrombrowser"] == ("chrome",)
    assert "cookiefile" not in captured["opts"]


def test_extract_subtitle_passes_proxy_to_ydl(tmp_path: Path, monkeypatch) -> None:
    """显式代理透传进 ydl_opts（YouTube 直连被墙时字幕阶段超时的解法）。"""
    from video_to_summary.sources.url import URLAudioSource

    captured, _ = _patch_ydl(monkeypatch, tmp_path, "v1", [("zh-HK", "vtt", False)])
    src = URLAudioSource(
        "https://www.youtube.com/watch?v=v1",
        output_dir=tmp_path,
        proxy="http://127.0.0.1:7897",
    )
    res = src.extract_subtitle(
        SubtitleConfig(preference=SubtitlePreference.AUTO, language="auto")
    )
    assert res is not None
    assert captured["opts"]["proxy"] == "http://127.0.0.1:7897"


# ---------- User-Agent 配置解析（VTS_USER_AGENT → ydl_opts.user_agent + http_headers） ----------


def test_resolve_user_agent_opts() -> None:
    """非空同时产出 user_agent 与 http_headers["User-Agent"]；空/未设置返回 {}。

    http_headers 是必须的（服务器实测，勿再"简化"回去）：yt-dlp 在 B 站提取路径上
    仅设 user_agent 不足以替换请求头里的 UA（真实请求仍 HTTP 412），CLI --user-agent
    有效正是因为还会写进 http_headers。只断言 user_agent 字段会漏掉这个缺陷
    （上一轮正是如此——参数被设置了、请求头却没带上）。
    """
    from video_to_summary.sources.url import _resolve_user_agent

    assert _resolve_user_agent("Wget/1.21.3") == {
        "user_agent": "Wget/1.21.3",
        "http_headers": {"User-Agent": "Wget/1.21.3"},
    }
    assert _resolve_user_agent("  Wget/1.21.3  ") == {
        "user_agent": "Wget/1.21.3",
        "http_headers": {"User-Agent": "Wget/1.21.3"},
    }
    assert _resolve_user_agent(None) == {}
    assert _resolve_user_agent("") == {}
    assert _resolve_user_agent("   ") == {}


def test_resolve_user_agent_merges_existing_http_headers() -> None:
    """与既有 http_headers **合并而非覆盖**：只写 User-Agent，不动别人设的其它请求头。"""
    from video_to_summary.sources.url import _resolve_user_agent

    opts = _resolve_user_agent("Wget/1.21.3", {"Accept-Language": "zh-CN,zh;q=0.9"})
    assert opts["user_agent"] == "Wget/1.21.3"
    assert opts["http_headers"] == {
        "User-Agent": "Wget/1.21.3",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }


def test_extract_subtitle_passes_user_agent_to_ydl(tmp_path: Path, monkeypatch) -> None:
    """自定义 UA 透传进 ydl_opts（字幕提取路径，VTS_USER_AGENT 通道生效）。

    断言重点是 http_headers["User-Agent"]：B 站提取路径实测仅设 user_agent 不生效
    （请求头仍是默认 UA，HTTP 412），真正替换请求头的是 http_headers。
    """
    from video_to_summary.sources.url import URLAudioSource

    captured, _ = _patch_ydl(monkeypatch, tmp_path, "v1", [("zh-Hans", "vtt", False)])
    src = URLAudioSource(
        "https://www.youtube.com/watch?v=v1", output_dir=tmp_path, user_agent="Wget/1.21.3"
    )
    res = src.extract_subtitle(
        SubtitleConfig(preference=SubtitlePreference.AUTO, language="auto")
    )
    assert res is not None
    assert captured["opts"]["user_agent"] == "Wget/1.21.3"
    assert captured["opts"]["http_headers"]["User-Agent"] == "Wget/1.21.3"


def test_extract_subtitle_default_no_user_agent_key(tmp_path: Path, monkeypatch) -> None:
    """未配置 UA 时 ydl_opts 不出现 user_agent / http_headers 键（yt-dlp 用默认 UA，不影响其它站点）。"""
    captured, _ = _patch_ydl(monkeypatch, tmp_path, "v1", [("zh-Hans", "vtt", False)])
    res = _make_source(tmp_path).extract_subtitle(
        SubtitleConfig(preference=SubtitlePreference.AUTO, language="auto")
    )
    assert res is not None
    assert "user_agent" not in captured["opts"]
    assert "http_headers" not in captured["opts"]


def test_resolve_passes_user_agent_to_download_audio(tmp_path: Path, monkeypatch) -> None:
    """下载路径：URLAudioSource.resolve 把 user_agent 传给 download_audio（CLI/Web 共用）。"""
    from video_to_summary.schemas import AudioMeta
    from video_to_summary.sources.url import URLAudioSource

    captured: dict = {}

    def fake_download_audio(url, output_dir, **kwargs):
        captured.update(kwargs)
        audio_path = tmp_path / "v1.wav"
        audio_path.write_bytes(b"RIFF....WAVEfmt ")
        return audio_path, AudioMeta(source_id="v1", title="t", source_url=url, audio_path=audio_path)

    monkeypatch.setattr("video_to_summary.sources.url.download_audio", fake_download_audio)
    src = URLAudioSource("https://example.test/v", output_dir=tmp_path, user_agent="Wget/1.21.3")
    src.resolve()
    assert captured["user_agent"] == "Wget/1.21.3"
