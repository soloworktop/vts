"""CLI 入口单测：转写器/摘要器的 BYOK 配置解析。

本构建语义（相对历史版本的差异）：
- 转写引擎唯一 = OpenAI 兼容 Whisper API；字幕优先在上游兜住常见情况
- LLM 接入信息全部来自用户自备（``--summary-key`` / ``--summary-base-url`` /
  ``--summary-model`` 或 ``SUMMARY_*`` / ``ASR_*`` 环境变量）；未配置时不构造摘要器
  （降级出转写）
- 变量/旗标已按槽位更名（SUMMARY_* / ASR_*）：旧名 LLM_* / OPENAI_API_KEY /
  --llm-* / --openai-key 作为兼容别名保留，本文件同时锚定新旧两条路径

不触网：URLAudioSource.resolve / pipeline.run / summarizer 全部打桩。
"""

import logging
from pathlib import Path

import pytest

from video_to_summary import config
from video_to_summary import main as cli
from video_to_summary.config import DEFAULT_ASR_API_MODEL
from video_to_summary.transcribers.openai_whisper_api import MissingASRCredentialsError

#: 可能喂给两个槽位的全部环境变量（新名 + 兼容别名），隔离用
_ALL_KEY_VARS = (
    "SUMMARY_API_KEY", "SUMMARY_BASE_URL", "SUMMARY_MODEL",
    "ASR_API_KEY", "ASR_MODEL", "ASR_BASE_URL",
    "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
    "OPENAI_API_KEY",
)


class _FakeTranscriber:
    last_kwargs: dict | None = None

    def __init__(self, api_key: str = "", model: str = "", base_url=None, **extra):
        type(self).last_kwargs = {"api_key": api_key, "model": model, "base_url": base_url}


class _FakeSummarizer:
    instances: list = []

    def __init__(self, *args, **kwargs):
        type(self).instances.append(kwargs)


@pytest.fixture()
def isolated_cli(monkeypatch):
    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(cli, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(cli, "OpenAIWhisperAPITranscriber", _FakeTranscriber)
    monkeypatch.setattr(cli, "OpenAISummarizer", _FakeSummarizer)
    monkeypatch.setattr(
        cli.URLAudioSource,
        "resolve",
        lambda self: (Path("a.wav"), type("M", (), {"title": "t", "source_id": "sid"})()),
    )
    monkeypatch.setattr(cli, "run", lambda *a, **k: Path("out.summary.md"))
    _FakeTranscriber.last_kwargs = None
    _FakeSummarizer.instances = []
    return _FakeTranscriber


@pytest.fixture()
def clean_key_env(monkeypatch):
    """清空两槽位全部 Key/端点环境变量 + 重置旧名提示去重，保证用例隔离。"""
    for var in _ALL_KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    config._legacy_env_hinted.clear()


# ---------------------------------------------------------------- ASR 配置回落链


def test_asr_flags_override_env(isolated_cli, monkeypatch):
    monkeypatch.setenv("ASR_MODEL", "env-model")
    monkeypatch.setenv("ASR_BASE_URL", "https://env.example/v1")
    rc = cli.main([
        "https://example.com/v/1",
        "--asr-model", "flag-model",
        "--asr-base-url", "https://flag.example/v1",
    ])
    assert rc == 0
    assert isolated_cli.last_kwargs["model"] == "flag-model"
    assert isolated_cli.last_kwargs["base_url"] == "https://flag.example/v1"


def test_asr_env_used_when_no_flags(isolated_cli, monkeypatch):
    monkeypatch.setenv("ASR_MODEL", "asr-x")
    monkeypatch.setenv("ASR_BASE_URL", "https://asr.example/v1")
    rc = cli.main(["https://example.com/v/1", "--summary-key", "sk-test"])
    assert rc == 0
    assert isolated_cli.last_kwargs["model"] == "asr-x"
    assert isolated_cli.last_kwargs["base_url"] == "https://asr.example/v1"


def test_asr_base_url_falls_back_to_summary_base_url(isolated_cli, monkeypatch):
    rc = cli.main([
        "https://example.com/v/1",
        "--summary-key", "sk-test",
        "--summary-base-url", "https://summary.example/v1",
    ])
    assert rc == 0
    assert isolated_cli.last_kwargs["model"] == DEFAULT_ASR_API_MODEL
    assert isolated_cli.last_kwargs["base_url"] == "https://summary.example/v1"


def test_asr_final_fallback_is_default_model(isolated_cli, clean_key_env):
    rc = cli.main(["https://example.com/v/1"])
    assert rc == 0
    assert isolated_cli.last_kwargs["model"] == DEFAULT_ASR_API_MODEL
    assert isolated_cli.last_kwargs["base_url"] is None
    # BYOK：没有任何 LLM 配置时不构造摘要器（pipeline 推 summarize_skipped 后仍出转写）
    assert _FakeSummarizer.instances == []


# ---------------------------------------------------------------- 槽位环境变量（新名 + 兼容别名）


def test_summary_slot_new_env_names(isolated_cli, clean_key_env, monkeypatch):
    """SUMMARY_* 三元组直接生效（权威名）。"""
    monkeypatch.setenv("SUMMARY_API_KEY", "sk-new")
    monkeypatch.setenv("SUMMARY_BASE_URL", "https://new.example/v1")
    monkeypatch.setenv("SUMMARY_MODEL", "new-model")
    rc = cli.main(["https://example.com/v/1"])
    assert rc == 0
    assert len(_FakeSummarizer.instances) == 1
    assert _FakeSummarizer.instances[0]["api_key"] == "sk-new"
    assert _FakeSummarizer.instances[0]["model"] == "new-model"
    assert _FakeSummarizer.instances[0]["base_url"] == "https://new.example/v1"


def test_legacy_llm_env_names_still_work(isolated_cli, clean_key_env, monkeypatch, caplog):
    """旧名 LLM_* 经别名链仍生效，且命中时打一次更名 INFO（不告警不阻断）。"""
    monkeypatch.setenv("LLM_API_KEY", "sk-legacy")
    monkeypatch.setenv("LLM_BASE_URL", "https://legacy.example/v1")
    monkeypatch.setenv("LLM_MODEL", "legacy-model")
    with caplog.at_level(logging.INFO, logger="video_to_summary.config"):
        rc = cli.main(["https://example.com/v/1"])
    assert rc == 0
    assert len(_FakeSummarizer.instances) == 1
    assert _FakeSummarizer.instances[0]["api_key"] == "sk-legacy"
    assert _FakeSummarizer.instances[0]["model"] == "legacy-model"
    hints = [r for r in caplog.records if "LLM_API_KEY" in r.getMessage()]
    assert hints and "SUMMARY_API_KEY" in hints[0].getMessage()
    # 进程内同一旧名只提示一次（第二次构造不再打）
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="video_to_summary.config"):
        cli.main(["https://example.com/v/1"])
    hints_again = [r for r in caplog.records if "LLM_API_KEY" in r.getMessage()]
    assert hints_again == []


def test_new_name_wins_over_legacy(isolated_cli, clean_key_env, monkeypatch):
    """兜底链「新名 → 旧专名」：SUMMARY_API_KEY 与 LLM_API_KEY 同时设置时新名优先。"""
    monkeypatch.setenv("SUMMARY_API_KEY", "sk-new")
    monkeypatch.setenv("LLM_API_KEY", "sk-legacy")
    rc = cli.main(["https://example.com/v/1"])
    assert rc == 0
    assert _FakeSummarizer.instances[0]["api_key"] == "sk-new"


def test_asr_key_new_env_name(isolated_cli, clean_key_env, monkeypatch):
    """ASR_API_KEY 等价旧名 OPENAI_API_KEY：--whisper-api 检查通过并传给转写器。"""
    monkeypatch.setenv("ASR_API_KEY", "sk-asr-new")
    rc = cli.main(["https://example.com/v/1", "--whisper-api"])
    assert rc == 0
    assert isolated_cli.last_kwargs["api_key"] == "sk-asr-new"


# ---------------------------------------------------------------- CLI 四件套


def test_prog_is_vts(capsys):
    """usage 首行显示命令身份而非 main.py。"""
    with pytest.raises(SystemExit) as ei:
        cli.parse_args(["--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: vts")


def test_help_shows_defaults_and_examples(capsys):
    """+ArgumentDefaultsHelpFormatter：默认值可见；epilog 含常用示例（只展示新名旗标）。"""
    with pytest.raises(SystemExit):
        cli.parse_args(["--help"])
    out = capsys.readouterr().out
    assert "default:" in out          # 默认值展示（如 --output-dir default: output）
    assert "示例:" in out             # epilog 示例段
    assert "--summary-template" in out
    assert "--summary-key" in out
    assert "--asr-key" in out
    # 旧旗标是隐藏别名：不进 --help，但解析仍接受（见下）

def test_legacy_cli_flags_are_hidden_aliases(isolated_cli):
    """旧旗标 --llm-* / --openai-key 以同 dest 隐藏别名保留，老命令行不受影响。"""
    rc = cli.main([
        "https://example.com/v/1",
        "--llm-key", "sk-legacy-flag",
        "--llm-model", "legacy-flag-model",
    ])
    assert rc == 0
    assert _FakeSummarizer.instances[0]["api_key"] == "sk-legacy-flag"
    assert _FakeSummarizer.instances[0]["model"] == "legacy-flag-model"


def test_help_has_no_local_engine_flags(capsys):
    """本构建不再有本地 Whisper / 中转通道类参数。"""
    with pytest.raises(SystemExit):
        cli.parse_args(["--help"])
    out = capsys.readouterr().out
    for removed in ("--local-engine", "--llm-transcribe"):
        assert removed not in out


def test_version_flag_prints_and_exits(capsys):
    """--version 打印版本号后以 0 退出。"""
    with pytest.raises(SystemExit) as ei:
        cli.parse_args(["--version"])
    assert ei.value.code == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("vts ")
    assert out.split(" ", 1)[1]       # 版本串非空（开发态 git describe / dev）


def test_whisper_api_missing_key_error_goes_to_stderr(isolated_cli, capsys, clean_key_env):
    """业务错误必须走 stderr（管道/脚本友好），stdout 保持干净。"""
    rc = cli.main([
        "https://example.com/v/1",
        "--whisper-api",
    ])
    assert rc == 2
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert captured.out == ""


def test_whisper_api_env_key_is_accepted(isolated_cli, monkeypatch):
    """OPENAI_API_KEY（兼容别名）环境变量等价显式 --asr-key：检查必须在 Settings 回落之后。

    回归锚点：前置检查曾放在 Settings 构造之前只看显式参数，导致按报错文案
    设置环境变量的用户仍被误拒（文案与行为矛盾）。
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-test")
    rc = cli.main([
        "https://example.com/v/1",
        "--whisper-api",
    ])
    assert rc == 0
    # Settings.__post_init__ 回落的环境变量 Key 传给了转写器
    assert isolated_cli.last_kwargs["api_key"] == "sk-env-test"


def test_no_subtitle_without_key_friendly_error(isolated_cli, capsys, clean_key_env, monkeypatch):
    """无字幕视频 + 无任何 Key：stderr 给可行动引导 + exit 2，不裸抛 SDK traceback。

    有字幕时 transcribe 不会被调用（无 Key 降级仍出 .txt/.srt），本错误只在
    转写真正发生时抛出（见 MissingASRCredentialsError 文档字符串）。
    """

    def _run(*a, **k):
        raise MissingASRCredentialsError("该视频没有可用的自带字幕，转写需要 Whisper API Key")

    monkeypatch.setattr(cli, "run", _run)
    rc = cli.main(["https://example.com/v/1"])
    assert rc == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error:" in captured.err
    assert "Key" in captured.err


def test_dotenv_discovered_from_cwd(tmp_path, monkeypatch):
    """.env 从**命令执行的当前目录**向上查找，pip 安装用户在自己目录放 .env 必须生效。

    回归锚点：load_dotenv() 无参从本模块文件位置向上找 .env——editable/PyPI 安装后
    在任意目录使用时，用户自己的 .env 静默失效（README 指引过"写进 .env"）。
    本测试不打桩 load_dotenv，走真实的 find_dotenv(usecwd=True) 路径。
    """
    for var in _ALL_KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".env").write_text("ASR_API_KEY=sk-cwd-env\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(cli, "configure_logging", lambda: None)
    monkeypatch.setattr(cli, "OpenAIWhisperAPITranscriber", _FakeTranscriber)
    monkeypatch.setattr(cli, "OpenAISummarizer", _FakeSummarizer)
    monkeypatch.setattr(
        cli.URLAudioSource,
        "resolve",
        lambda self: (Path("a.wav"), type("M", (), {"title": "t", "source_id": "sid"})()),
    )
    monkeypatch.setattr(cli, "run", lambda *a, **k: Path("out.summary.md"))

    rc = cli.main(["https://example.com/v/1", "--whisper-api"])
    assert rc == 0
    # Key 来自 CWD 的 .env，而非进程环境
    assert _FakeTranscriber.last_kwargs["api_key"] == "sk-cwd-env"


def test_removed_flags_are_rejected(isolated_cli):
    """已移除的历史参数必须被 argparse 拒绝（而非静默忽略）。"""
    for flag in ("--local-engine", "--llm-transcribe", "--model"):
        with pytest.raises(SystemExit) as ei:
            cli.parse_args(["https://example.com/v/1", flag, "x"])
        assert ei.value.code == 2
