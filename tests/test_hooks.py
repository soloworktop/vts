"""插件挂载点（`web/hooks.py`）与能力声明（`web/capabilities.py`）的契约测试。

本文件锁死 **fail-closed 判据** 与 **能力协商契约**：

- 查到 **0 个**插件 = 未安装插件的预期状态 → 核心即功能完整的 BYOK 产品，
  ``current_capabilities()`` 返回空对象 ``{}``
- 查到 **≥1 个**但 `ep.load()`（或随后的 `register()`）失败 → **拒绝启动**
  （抛 `PluginLoadError`，绝不静默降级成半注册状态）
- 能力位一律由插件经 ``declare_capabilities`` 动态声明（任意名字、布尔值）；
  核心不预置任何能力位（``BASE_CAPABILITIES == {}``）
"""

import pytest
from pathlib import Path

from video_to_summary.web import hooks
from video_to_summary.web.capabilities import BASE_CAPABILITIES, current_capabilities


@pytest.fixture(autouse=True)
def _clean_hooks():
    """每个用例前后都清空全局挂载点，避免相互污染。"""
    hooks.HOOKS.reset()
    yield
    hooks.HOOKS.reset()


class _FakeEntryPoint:
    """importlib.metadata.EntryPoint 的最小替身（load() 行为可注入）。"""

    def __init__(self, name, plugin=None, *, raises=None):
        self.name = name
        self._plugin = plugin
        self._raises = raises

    def load(self):
        if self._raises is not None:
            raise self._raises
        return self._plugin


class _FullPlugin:
    """四类挂载点 + 能力声明都注册的插件（能力名为插件自定义的任意名字）。"""

    CAPABILITIES = {"custom_export": True}

    def __init__(self):
        self.seen = None

    def register(self, h):
        self.seen = h
        h.register_routes(_Routes())
        h.register_side_effect(_Effect())
        h.register_settings(_Settings())
        h.declare_capabilities({"custom_panel": True})


class _Routes:
    def __init__(self):
        self.registered = []

    def register_routes(self, app):
        self.registered.append(app)


class _Effect:
    def __init__(self):
        self.calls = []

    def on_job_completed(self, job_id, metrics):
        self.calls.append((job_id, metrics))


class _Settings:
    def settings_items(self):
        return [{"id": "plugin-section", "title": "插件设置"}]


# ---------------------------------------------------------------- 加载判据


def test_zero_plugins_is_clean_build_and_empty_capabilities(monkeypatch) -> None:
    """0 条 = 未安装插件的预期状态：核心即完整 BYOK 产品，能力聚合为空对象。"""
    monkeypatch.setattr(hooks, "_iter_entry_points", lambda: [])
    result = hooks.load_plugins()
    assert result is hooks.HOOKS
    # 能力协商：核心不预置 + 无插件声明 → 恒为空对象（前端据此渲染完整基础 UI）
    assert current_capabilities() == {}
    assert result.capabilities == {}
    assert result.route_providers == []
    assert result.side_effects == []
    assert result.settings_providers == []


def test_plugin_load_failure_is_fail_closed(monkeypatch) -> None:
    """有条目但 load() 抛异常 → 拒绝启动（绝不静默跳过）。"""
    entries = [
        _FakeEntryPoint("good", _FullPlugin()),
        _FakeEntryPoint("broken", raises=ImportError("no module named plugin_backend")),
    ]
    with pytest.raises(hooks.PluginLoadError) as ei:
        hooks.load_plugins(entries=entries)
    message = str(ei.value)
    assert "broken" in message and "fail-closed" in message
    # 关键：异常必须抛出，而不是"跳过坏插件、照常服务"
    assert "拒绝启动" in message


def test_plugin_register_failure_is_fail_closed() -> None:
    """load() 成功但 register() 抛异常 → 同样拒绝启动（半注册比没有更危险）。"""

    class _BrokenRegister:
        def register(self, h):
            raise RuntimeError("register 里炸了")

    with pytest.raises(hooks.PluginLoadError):
        hooks.load_plugins(entries=[_FakeEntryPoint("broken-register", _BrokenRegister())])


def test_plugin_without_register_entry_is_fail_closed() -> None:
    """插件对象没有 register(hooks) → 视为加载失败（fail-closed）。"""

    class _NoRegister:
        pass

    with pytest.raises(hooks.PluginLoadError) as ei:
        hooks.load_plugins(entries=[_FakeEntryPoint("no-register", _NoRegister())])
    assert "register" in str(ei.value)


def test_loaded_plugin_populates_every_hook_point(monkeypatch) -> None:
    """正常插件：四类挂载点与能力声明全部生效。"""
    plugin = _FullPlugin()
    hooks.load_plugins(entries=[_FakeEntryPoint("full", plugin)])

    assert plugin.seen is hooks.HOOKS
    assert len(hooks.HOOKS.route_providers) == 1
    assert len(hooks.HOOKS.side_effects) == 1
    assert len(hooks.HOOKS.settings_providers) == 1
    # 能力位：模块级 CAPABILITIES 与 declare_capabilities 的声明聚合（名字由插件自定义）
    caps = current_capabilities()
    assert caps["custom_export"] is True
    assert caps["custom_panel"] is True
    assert set(caps) == {"custom_export", "custom_panel"}  # 无任何核心预置能力位


def test_base_capabilities_is_empty() -> None:
    """核心不预置任何能力位：BASE_CAPABILITIES 恒为空对象。"""
    assert BASE_CAPABILITIES == {}


def test_current_capabilities_empty_without_plugins() -> None:
    """0 插件（_clean_hooks 已清空挂载点）：current_capabilities() 返回空对象。"""
    assert current_capabilities() == {}


def test_declare_capabilities_coerces_to_bool() -> None:
    h = hooks.Hooks()
    h.declare_capabilities({"custom_export": 1, "custom_panel": "", "extra": None})
    assert h.capabilities == {"custom_export": True, "custom_panel": False, "extra": False}


# ---------------------------------------------------------------- 副作用 / 设置 / 路由


def test_side_effects_are_broadcast(monkeypatch) -> None:
    effects = [_Effect(), _Effect()]
    monkeypatch.setattr(hooks.HOOKS, "side_effects", effects)
    hooks.run_job_completed_side_effects("job-1", {"summary_chars": 42})
    for e in effects:
        assert e.calls == [("job-1", {"summary_chars": 42})]


def test_side_effect_failure_never_bubbles(monkeypatch) -> None:
    """副作用抛异常绝不影响任务终态（逐条 catch + 记日志）。"""

    class _Broken:
        def on_job_completed(self, job_id, metrics):
            raise RuntimeError("上报失败")

    ok = _Effect()
    monkeypatch.setattr(hooks.HOOKS, "side_effects", [_Broken(), ok])
    hooks.run_job_completed_side_effects("job-2", {})  # 不抛
    assert ok.calls == [("job-2", {})]  # 后续副作用照常执行


def test_settings_items_aggregate_and_swallow_failure(monkeypatch) -> None:
    class _Broken:
        def settings_items(self):
            raise RuntimeError("nope")

    monkeypatch.setattr(hooks.HOOKS, "settings_providers", [_Broken(), _Settings()])
    assert hooks.settings_items() == [{"id": "plugin-section", "title": "插件设置"}]


def test_settings_items_empty_without_plugins() -> None:
    assert hooks.settings_items() == []


def test_register_plugin_routes_forwards_app(monkeypatch) -> None:
    provider = _Routes()
    monkeypatch.setattr(hooks.HOOKS, "route_providers", [provider])
    sentinel = object()
    hooks.register_plugin_routes(sentinel)
    assert provider.registered == [sentinel]


def test_hooks_reset_clears_everything() -> None:
    h = hooks.Hooks()
    h.register_routes(_Routes())
    h.register_side_effect(_Effect())
    h.register_settings(_Settings())
    h.declare_capabilities({"custom_export": True})
    h.reset()
    assert (h.route_providers, h.side_effects, h.settings_providers, h.capabilities) == (
        [],
        [],
        [],
        {},
    )


# ---------------------------------------------------------------- 真实入口点查询


def test_repo_declares_no_vts_plugins_entry_point() -> None:
    """本仓 pyproject.toml 未声明 vts.plugins 入口点（环境无关的静态断言）。

    不查询运行时 metadata：只要 venv 里装了任何声明 vts.plugins 的插件包
    （例如第三方插件），真实 importlib.metadata 查询就会非 0，结论被环境干扰。
    插件相关行为一律用 ``load_plugins(entries=...)`` 显式注入测试；
    "0 条 = 完整产品 + 空能力对象"语义由
    ``test_zero_plugins_is_clean_build_and_empty_capabilities`` 覆盖
    （conftest 的 ``_no_plugins`` session fixture 同时保证整套测试默认 0 插件）。
    """
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    assert "vts.plugins" not in pyproject.read_text(encoding="utf-8")


def test_app_import_loads_plugins_without_error() -> None:
    """导入期调用 load_plugins 不得抛异常（0 条 = 正常路径）。"""
    from video_to_summary.web import app as web_app  # noqa: F401 - 导入即触发

    assert hooks.HOOKS.capabilities == {}
