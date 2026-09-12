"""全局偏好设置与「文本优化」开关默认值测试。"""

import pytest

from video_to_summary import db as store_db
from video_to_summary.web import settings_store


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(store_db, "DATABASE_PATH", tmp_path / "app.db")
    monkeypatch.setattr(store_db, "LEGACY_LLM_FILE", tmp_path / "m.json")
    monkeypatch.setattr(store_db, "LEGACY_TEMPLATE_FILE", tmp_path / "m_t.json")
    store_db.reset()
    monkeypatch.delenv("POLISH_TRANSCRIPT", raising=False)
    yield


def test_default_polish_off() -> None:
    assert settings_store.get_default_polish() is False


def test_set_default_polish_persists() -> None:
    settings_store.set_default_polish(True)
    assert settings_store.get_default_polish() is True
    settings_store.set_default_polish(False)
    assert settings_store.get_default_polish() is False


def test_env_fallback_and_db_priority(monkeypatch) -> None:
    monkeypatch.setenv("POLISH_TRANSCRIPT", "1")
    assert settings_store.get_default_polish() is True
    # DB 设置优先于环境变量
    settings_store.set_default_polish(False)
    assert settings_store.get_default_polish() is False


def test_settings_polish_default_from_env(monkeypatch) -> None:
    from video_to_summary.config import Settings

    monkeypatch.setenv("POLISH_TRANSCRIPT", "1")
    assert Settings(url="https://x").polish_transcript is True
    # from_mapping 显式 False 覆盖环境默认
    assert Settings.from_mapping({"url": "https://x", "polish_transcript": False}).polish_transcript is False


def test_cli_flag_uses_global_default(monkeypatch) -> None:
    from video_to_summary.config import Settings
    from video_to_summary.main import parse_args

    assert parse_args(["https://x"]).polish_transcript is None  # 未传 → 用全局默认
    assert parse_args(["https://x", "--polish-transcript"]).polish_transcript is True
    assert parse_args(["https://x", "--no-polish-transcript"]).polish_transcript is False

    monkeypatch.setenv("POLISH_TRANSCRIPT", "1")
    s = Settings.from_mapping(vars(parse_args(["https://x"])))
    assert s.polish_transcript is True


def test_job_defaults_defaults() -> None:
    d = settings_store.job_defaults()
    assert d["summary_template"] == "通用"
    assert d["audio_format"] == "mp3"
    assert d["polish_preset"] == "default"
    assert d["polish_transcript"] is False
    assert d["subtitle_preference"] == "auto"
    assert d["subtitle_language"] == "auto"
    assert d["cookies_browser"] == ""
    assert d["proxy"] == ""
    # 本地 Whisper / 中转通道的兼容键已随历史版本移除（不再有「转写模式」概念）
    for removed in ("asr_mode", "local_engine", "model"):
        assert removed not in d


def test_set_job_defaults_persists() -> None:
    settings_store.set_job_defaults(
        {
            "summary_template": "学术笔记",
            "polish_transcript": True,
            "subtitle_preference": "manual_only",
            "subtitle_language": "en",
        }
    )
    d = settings_store.job_defaults()
    assert d["summary_template"] == "学术笔记"
    assert d["polish_transcript"] is True
    assert d["subtitle_preference"] == "manual_only"
    assert d["subtitle_language"] == "en"


def test_set_job_defaults_ignores_unknown_keys() -> None:
    """未知键（含已移除的历史兼容键）静默忽略：不落库、也不报错。"""
    settings_store.set_job_defaults({"asr_mode": "llm", "model": "small", "subtitle_language": "ja"})
    d = settings_store.job_defaults()
    assert "asr_mode" not in d and "model" not in d
    assert d["subtitle_language"] == "ja"


def test_job_defaults_payload_conversion() -> None:
    settings_store.set_job_defaults({"polish_transcript": True, "subtitle_language": "en"})
    p = settings_store.job_defaults_payload()
    # 开源版转写引擎唯一：字幕优先 + OpenAI 兼容 Whisper API
    assert p["whisper_api"] is True
    assert p["polish_transcript"] is True
    assert p["subtitle_language"] == "en"
    assert p["subtitle_preference"] == "auto"
    assert p["audio_format"] == "mp3"
    for removed in ("llm_transcribe", "model", "local_engine", "asr_mode"):
        assert removed not in p


def test_set_job_defaults_atomic_on_invalid_value() -> None:
    """校验失败时不产生部分写入：合法键先到也不能落库。"""
    settings_store.set_job_defaults({"cookies_browser": "chrome", "proxy": ""})
    assert settings_store.job_defaults()["cookies_browser"] == "chrome"

    with pytest.raises(ValueError, match="invalid proxy url"):
        settings_store.set_job_defaults({"cookies_browser": "firefox", "proxy": "not-a-proxy"})
    # firefox 不得被写入（先到的合法键同批回滚语义）
    assert settings_store.job_defaults()["cookies_browser"] == "chrome"


# ---------------------------------------------------------------- VTS_USER_AGENT 环境变量通道


def test_settings_user_agent_from_env(monkeypatch) -> None:
    from video_to_summary.config import Settings

    monkeypatch.setenv("VTS_USER_AGENT", "  Wget/1.21.3  ")
    assert Settings(url="https://x").user_agent == "Wget/1.21.3"


def test_settings_user_agent_empty_env_keeps_default(monkeypatch) -> None:
    """空/未设置 → None（= 用 yt-dlp 默认 UA，绝不改动默认行为）。"""
    from video_to_summary.config import Settings

    monkeypatch.delenv("VTS_USER_AGENT", raising=False)
    assert Settings(url="https://x").user_agent is None
    monkeypatch.setenv("VTS_USER_AGENT", "   ")
    assert Settings(url="https://x").user_agent is None


def test_settings_user_agent_explicit_wins_over_env(monkeypatch) -> None:
    from video_to_summary.config import Settings

    monkeypatch.setenv("VTS_USER_AGENT", "Env/1.0")
    assert Settings(url="https://x", user_agent="Explicit/2.0").user_agent == "Explicit/2.0"


def test_settings_user_agent_cleans_control_chars_with_warning(monkeypatch, caplog) -> None:
    """含换行/控制字符的 UA 被清理并 WARN（不静默把坏值传给 yt-dlp）。"""
    from video_to_summary.config import Settings

    monkeypatch.setenv("VTS_USER_AGENT", "Wget/1.21.3\nX-Injected: 1")
    with caplog.at_level("WARNING", logger="video_to_summary.config"):
        s = Settings(url="https://x")
    assert s.user_agent == "Wget/1.21.3X-Injected: 1"
    assert any("VTS_USER_AGENT" in r.message and "控制字符" in r.message for r in caplog.records)


def test_settings_user_agent_truncates_overlong_with_warning(monkeypatch, caplog) -> None:
    from video_to_summary.config import Settings, _USER_AGENT_MAX_CHARS

    monkeypatch.setenv("VTS_USER_AGENT", "A" * 500)
    with caplog.at_level("WARNING", logger="video_to_summary.config"):
        s = Settings(url="https://x")
    assert s.user_agent == "A" * _USER_AGENT_MAX_CHARS
    assert len(s.user_agent) == _USER_AGENT_MAX_CHARS
    assert any("超长" in r.message for r in caplog.records)


# ---------------------------------------------------------------- VTS_COOKIES_FILE 环境变量通道


def test_settings_cookies_file_env_used_when_exists(monkeypatch, tmp_path) -> None:
    from video_to_summary.config import Settings

    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.setenv("VTS_COOKIES_FILE", str(cookie_file))
    assert Settings(url="https://x").cookies == cookie_file


def test_settings_cookies_file_env_missing_warns_and_ignored(monkeypatch, tmp_path, caplog) -> None:
    """文件不存在 → WARN 并说明路径，配置被忽略（不静默，也不把死路径交给 yt-dlp）。"""
    from video_to_summary.config import Settings

    missing = tmp_path / "no_such_cookies.txt"
    monkeypatch.setenv("VTS_COOKIES_FILE", str(missing))
    with caplog.at_level("WARNING", logger="video_to_summary.config"):
        s = Settings(url="https://x")
    assert s.cookies is None
    assert any("VTS_COOKIES_FILE" in r.message and str(missing) in r.message for r in caplog.records)


def test_settings_cookies_explicit_wins_over_env(monkeypatch, tmp_path) -> None:
    """CLI --cookies（显式字段）优先于 VTS_COOKIES_FILE 环境变量。"""
    from video_to_summary.config import Settings

    env_file = tmp_path / "env.txt"
    env_file.write_text("# Netscape\n", encoding="utf-8")
    explicit = tmp_path / "explicit.txt"
    explicit.write_text("# Netscape\n", encoding="utf-8")
    monkeypatch.setenv("VTS_COOKIES_FILE", str(env_file))
    assert Settings(url="https://x", cookies=explicit).cookies == explicit


def test_settings_cookies_env_file_beats_browser_cookies(monkeypatch, tmp_path) -> None:
    """VTS_COOKIES_FILE 与 cookies_browser 互斥：显式文件优先（复用 _resolve_cookie_opts 语义）。

    文件缺失（告警忽略）时回落到浏览器 cookies，绝不并存。
    """
    from video_to_summary.config import Settings
    from video_to_summary.sources.url import _resolve_cookie_opts

    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.setenv("VTS_COOKIES_FILE", str(cookie_file))
    s = Settings(url="https://www.bilibili.com/video/BV1xx", cookies_browser="chrome")
    assert _resolve_cookie_opts(s.url, s.cookies, s.cookies_browser) == {"cookiefile": str(cookie_file)}

    monkeypatch.setenv("VTS_COOKIES_FILE", str(tmp_path / "missing.txt"))
    s2 = Settings(url="https://www.bilibili.com/video/BV1xx", cookies_browser="firefox")
    assert _resolve_cookie_opts(s2.url, s2.cookies, s2.cookies_browser) == {"cookiesfrombrowser": ("firefox",)}
