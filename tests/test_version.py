"""version 模块测试：版本解析优先级与回落链（全程离线、无真实 git 依赖）。

接缝：_build_version/_git_version/_metadata_version 三个 layer 函数逐层
monkeypatch；注意重置模块级缓存 `_cached`。
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from video_to_summary import version as version_mod  # noqa: E402


@pytest.fixture()
def fresh_cache(monkeypatch):
    """每用例清缓存 + 清 env：get_version 进程内只解析一次，测试需显式重置。

    同时删除 VIDEO_TO_SUMMARY_VERSION，保证回退链类用例不依赖环境里是否
    残留该变量（例如其它插件包在导入期设置过它）。
    """
    monkeypatch.setattr(version_mod, "_cached", None)
    monkeypatch.delenv("VIDEO_TO_SUMMARY_VERSION", raising=False)


def test_build_version_has_highest_priority(fresh_cache, monkeypatch):
    """构建包：_version.py 存在 → 直接采用，不再探 git/元数据。"""
    monkeypatch.setattr(version_mod, "_build_version", lambda: "v0.3.0")
    monkeypatch.setattr(version_mod, "_git_version", lambda: pytest.fail("不应探 git"))
    monkeypatch.setattr(version_mod, "_metadata_version", lambda: pytest.fail("不应读元数据"))
    assert version_mod.get_version() == "v0.3.0"
    assert version_mod.get_version() == "v0.3.0"  # 缓存：二次调用同一结果


def test_falls_back_to_git_then_metadata_then_dev(fresh_cache, monkeypatch):
    monkeypatch.setattr(version_mod, "_build_version", lambda: "")
    monkeypatch.setattr(version_mod, "_git_version", lambda: "v0.2.0-3-g1a2b3c4")
    assert version_mod.get_version() == "v0.2.0-3-g1a2b3c4"


def test_falls_back_to_metadata(fresh_cache, monkeypatch):
    monkeypatch.setattr(version_mod, "_build_version", lambda: "")
    monkeypatch.setattr(version_mod, "_git_version", lambda: "")
    monkeypatch.setattr(version_mod, "_metadata_version", lambda: "0.1.0")
    assert version_mod.get_version() == "0.1.0"


def test_all_layers_empty_falls_back_to_dev(fresh_cache, monkeypatch):
    monkeypatch.setattr(version_mod, "_build_version", lambda: "")
    monkeypatch.setattr(version_mod, "_git_version", lambda: "")
    monkeypatch.setattr(version_mod, "_metadata_version", lambda: "")
    assert version_mod.get_version() == "dev"


def test_env_version_overrides_all_layers(fresh_cache, monkeypatch):
    """VIDEO_TO_SUMMARY_VERSION 优先：非空即返回，不再探构建/git/元数据。"""
    monkeypatch.setenv("VIDEO_TO_SUMMARY_VERSION", "9.9.9-ext")
    monkeypatch.setattr(version_mod, "_build_version", lambda: pytest.fail("不应探构建版本"))
    monkeypatch.setattr(version_mod, "_git_version", lambda: pytest.fail("不应探 git"))
    monkeypatch.setattr(version_mod, "_metadata_version", lambda: pytest.fail("不应读元数据"))
    assert version_mod.get_version() == "9.9.9-ext"
    assert version_mod.get_version() == "9.9.9-ext"  # 缓存：二次调用同一结果


def test_env_version_empty_falls_back(fresh_cache, monkeypatch):
    """环境变量为空字符串：视为未设置，回落链不受影响。"""
    monkeypatch.setenv("VIDEO_TO_SUMMARY_VERSION", "  ")
    monkeypatch.setattr(version_mod, "_build_version", lambda: "v0.3.0")
    monkeypatch.setattr(version_mod, "_git_version", lambda: pytest.fail("空 env 不应探 git"))
    monkeypatch.setattr(version_mod, "_metadata_version", lambda: pytest.fail("不应读元数据"))
    assert version_mod.get_version() == "v0.3.0"


def test_env_version_unset_falls_back_unchanged(fresh_cache, monkeypatch):
    """未设置环境变量：原回落链（git → 元数据 → dev）不受影响。"""
    monkeypatch.delenv("VIDEO_TO_SUMMARY_VERSION", raising=False)
    monkeypatch.setattr(version_mod, "_build_version", lambda: "")
    monkeypatch.setattr(version_mod, "_git_version", lambda: "v0.2.0-3-g1a2b3c4")
    monkeypatch.setattr(version_mod, "_metadata_version", lambda: pytest.fail("不应读元数据"))
    assert version_mod.get_version() == "v0.2.0-3-g1a2b3c4"


def test_env_override_not_cached_after_removal(fresh_cache, monkeypatch):
    """env 覆盖不缓存：设置 → 调用 → 移除 → 再调用 应回落回退链。

    修复前 get_version 整体缓存，移除 env 后仍返回旧 env 值（本用例在
    修复前失败、修复后通过）。
    """
    monkeypatch.delenv("VIDEO_TO_SUMMARY_VERSION", raising=False)
    monkeypatch.setattr(version_mod, "_build_version", lambda: "")
    monkeypatch.setattr(version_mod, "_git_version", lambda: "v0.2.0-3-g1a2b3c4")
    monkeypatch.setattr(version_mod, "_metadata_version", lambda: pytest.fail("移除 env 后不应读元数据"))
    monkeypatch.setenv("VIDEO_TO_SUMMARY_VERSION", "v9.9.9")
    assert version_mod.get_version() == "v9.9.9"
    monkeypatch.delenv("VIDEO_TO_SUMMARY_VERSION")
    assert version_mod.get_version() == "v0.2.0-3-g1a2b3c4"


@pytest.mark.parametrize("blank", ["", "  ", "\t ", "\n"])
def test_env_blank_treated_as_unset(fresh_cache, monkeypatch, blank):
    """env 为空串 / 仅空白：视为未设置，回落链照常且结果稳定。"""
    monkeypatch.setenv("VIDEO_TO_SUMMARY_VERSION", blank)
    monkeypatch.setattr(version_mod, "_build_version", lambda: "")
    monkeypatch.setattr(version_mod, "_git_version", lambda: "v0.2.0-3-g1a2b3c4")
    monkeypatch.setattr(version_mod, "_metadata_version", lambda: "")
    assert version_mod.get_version() == "v0.2.0-3-g1a2b3c4"
    assert version_mod.get_version() == "v0.2.0-3-g1a2b3c4"  # 回退链缓存：二次一致


def test_fallback_chain_still_cached_when_env_unset(fresh_cache, monkeypatch):
    """未设 env：昂贵回退链仍缓存——连续调用返回同一值且仅解析一次。"""
    calls = {"n": 0}

    def fake_git():
        calls["n"] += 1
        return "v0.2.0-3-g1a2b3c4"

    monkeypatch.delenv("VIDEO_TO_SUMMARY_VERSION", raising=False)
    monkeypatch.setattr(version_mod, "_build_version", lambda: "")
    monkeypatch.setattr(version_mod, "_git_version", fake_git)
    monkeypatch.setattr(version_mod, "_metadata_version", lambda: pytest.fail("不应读元数据"))
    assert version_mod.get_version() == "v0.2.0-3-g1a2b3c4"
    assert version_mod.get_version() == "v0.2.0-3-g1a2b3c4"
    assert calls["n"] == 1  # 第二次调用命中缓存，未重复 spawn git


def test_real_resolution_returns_nonempty(fresh_cache):
    """真实环境（git 树/包元数据至少其一可用）解析结果非空且稳定。"""
    v = version_mod.get_version()
    assert isinstance(v, str) and v
    assert v == version_mod.get_version()
