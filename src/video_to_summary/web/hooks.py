"""插件挂载点：经 entry_points 声明的插件的公开契约（能力声明 / 路由扩展 / 任务完成副作用 / 设置注入）。

核心只定义挂载点，不引用、不探测任何具体插件包名（硬耦合写法会被 CI
禁词扫描拦下，也会让插件加载失败被静默吞掉）。

加载机制
--------
``importlib.metadata.entry_points(group="vts.plugins")``：每个条目指向一个
模块/可调用对象，其 ``register(hooks)`` 被调用一次，用于注册额外路由、
任务完成副作用、额外设置项与能力声明。

判据（fail-closed）
------------------
- 查到 **0 条** = 未安装插件的预期状态，核心即功能完整的 BYOK 产品；
- 查到 **≥1 条**但 ``ep.load()``（或随后的 ``register()``）抛异常 = 插件
  状态可能残缺 → **抛致命错误、拒绝启动**，绝不静默降级成半加载状态。

因此 :func:`load_plugins` 在「有条目但加载失败」时抛 :class:`PluginLoadError`，
由 ``web/app.py`` 在导入期调用——异常会使 uvicorn 直接启动失败，而不是带着
残缺的挂载点继续服务。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, Callable, Iterable, Protocol, runtime_checkable

logger = logging.getLogger("video_to_summary.web.hooks")

#: entry_points 组名：插件发现的唯一入口（D12）
PLUGIN_GROUP = "vts.plugins"


@runtime_checkable
class RouteProvider(Protocol):
    """额外 HTTP 路由：插件把自己的路由挂到同一个 FastAPI app 上。"""

    def register_routes(self, app: Any) -> None:  # pragma: no cover - 协议
        ...


@runtime_checkable
class JobSideEffect(Protocol):
    """任务完成后的副作用（插件自定义的任务终态后处理）。"""

    def on_job_completed(self, job_id: str, metrics: dict) -> None:  # pragma: no cover - 协议
        ...


@runtime_checkable
class SettingsProvider(Protocol):
    """额外设置项：插件声明自己的设置分区，由核心并入设置接口响应。"""

    def settings_items(self) -> list[dict]:  # pragma: no cover - 协议
        ...


@dataclass
class Hooks:
    """已注册的挂载点集合（进程内单例，见模块级 ``HOOKS``）。

    未安装插件时所有列表为空、``capabilities`` 为空字典：
    :func:`video_to_summary.web.capabilities.current_capabilities` 返回空对象。
    """

    route_providers: list[RouteProvider] = field(default_factory=list)
    side_effects: list[JobSideEffect] = field(default_factory=list)
    settings_providers: list[SettingsProvider] = field(default_factory=list)
    #: 插件声明的能力覆盖：{能力名: bool}，核心不预置任何能力位
    capabilities: dict[str, bool] = field(default_factory=dict)

    def register_routes(self, provider: RouteProvider) -> None:
        self.route_providers.append(provider)

    def register_side_effect(self, effect: JobSideEffect) -> None:
        self.side_effects.append(effect)

    def register_settings(self, provider: SettingsProvider) -> None:
        self.settings_providers.append(provider)

    def declare_capabilities(self, mapping: dict | None) -> None:
        """声明/覆盖能力位（只接受布尔值，避免前端拿到不确定的真值判断）。"""
        for name, enabled in (mapping or {}).items():
            self.capabilities[str(name)] = bool(enabled)

    def reset(self) -> None:
        """测试钩子：清空全部注册（重新加载插件前调用）。"""
        self.route_providers.clear()
        self.side_effects.clear()
        self.settings_providers.clear()
        self.capabilities.clear()


#: 进程内挂载点单例。未安装插件时始终为空。
HOOKS = Hooks()


class PluginLoadError(RuntimeError):
    """插件条目存在但加载/注册失败：插件状态可能残缺，必须拒绝启动（fail-closed）。"""


def _iter_entry_points() -> Iterable[Any]:
    """查询 ``vts.plugins`` 组的入口点（无该组时返回空序列）。"""
    try:
        return entry_points(group=PLUGIN_GROUP)
    except TypeError:  # pragma: no cover - 兼容 3.9 旧 selectable API 形态
        return entry_points().get(PLUGIN_GROUP, [])  # type: ignore[union-attr]


def load_plugins(
    *,
    hooks: Hooks | None = None,
    entries: Iterable[Any] | None = None,
    entry_points_fn: Callable[[], Iterable[Any]] | None = None,
) -> Hooks:
    """发现并加载 ``vts.plugins`` 插件，返回填充后的挂载点集合。

    参数中的 ``entries`` / ``entry_points_fn`` 仅为测试注入接缝（默认走真实
    ``importlib.metadata``）。

    - 0 条 → 直接返回（未安装插件，核心即完整产品）
    - ≥1 条且任一 ``load()``/``register()`` 失败 → :class:`PluginLoadError`
    """
    target = hooks if hooks is not None else HOOKS
    if entries is None:
        entries = (entry_points_fn or _iter_entry_points)()
    discovered = list(entries)
    if not discovered:
        # 未安装插件的预期状态：所有挂载点为空、能力聚合为空对象
        logger.debug("no %s plugin found", PLUGIN_GROUP)
        return target

    failures: list[str] = []
    for ep in discovered:
        name = getattr(ep, "name", repr(ep))
        try:
            plugin = ep.load()
            _register_plugin(plugin, target)
        except Exception as exc:  # noqa: BLE001 - 逐条收集，最后统一 fail-closed
            logger.exception("plugin %r failed to load/register", name)
            failures.append(f"{name}: {exc!r}")
        else:
            logger.info("plugin %r loaded", name)

    if failures:
        # fail-closed：条目存在却加载不了，说明插件状态残缺——
        # 静默跳过会让服务带着半注册的挂载点继续运行。
        raise PluginLoadError(
            "插件加载失败，拒绝启动（fail-closed）："
            + "; ".join(failures)
            + f"；请修复或卸载 {PLUGIN_GROUP} 条目后重试。"
        )
    return target


def _register_plugin(plugin: Any, hooks: Hooks) -> None:
    """调用插件的 ``register(hooks)``；同时接受插件模块级的 ``CAPABILITIES`` 声明。"""
    declare = getattr(plugin, "CAPABILITIES", None)
    if isinstance(declare, dict):
        hooks.declare_capabilities(declare)
    register = getattr(plugin, "register", None)
    if register is None:
        raise AttributeError("plugin has no register(hooks) entry point")
    register(hooks)


def run_job_completed_side_effects(job_id: str, metrics: dict) -> None:
    """广播任务完成副作用。

    副作用失败**绝不**影响任务终态（与既有审计语义一致）：逐条 catch 并记日志。
    """
    for effect in HOOKS.side_effects:
        try:
            effect.on_job_completed(job_id, metrics or {})
        except Exception:  # noqa: BLE001 - 副作用绝不冒泡到任务链路
            logger.warning("job side effect failed for %s", job_id, exc_info=True)


def settings_items() -> list[dict]:
    """汇总插件声明的额外设置分区（无插件时为空列表）。"""
    items: list[dict] = []
    for provider in HOOKS.settings_providers:
        try:
            items.extend(provider.settings_items() or [])
        except Exception:  # noqa: BLE001 - 单个插件设置项失败不影响设置接口
            logger.warning("settings provider failed", exc_info=True)
    return items


def register_plugin_routes(app: Any) -> None:
    """把插件的额外路由挂到 app 上（无插件时不做任何事）。

    路由注册失败同样按 fail-closed 处理：半注册的路由表比没有更危险。
    """
    for provider in HOOKS.route_providers:
        provider.register_routes(app)


__all__ = [
    "PLUGIN_GROUP",
    "Hooks",
    "HOOKS",
    "JobSideEffect",
    "PluginLoadError",
    "RouteProvider",
    "SettingsProvider",
    "load_plugins",
    "register_plugin_routes",
    "run_job_completed_side_effects",
    "settings_items",
]
