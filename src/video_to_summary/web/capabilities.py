"""能力协商：``GET /api/v1/capabilities``。

核心不预置任何能力位；插件通过 :func:`video_to_summary.web.hooks.Hooks.declare_capabilities`
动态声明可选能力名（任意名字，原样带出）。接口返回核心预置（恒为空）与插件
声明的聚合结果，前端据此显示/隐藏对应的能力相关 UI。
"""

from __future__ import annotations

from .hooks import HOOKS

#: 核心预置能力位：恒为空——核心不含任何能力开关，能力一律由插件声明。
BASE_CAPABILITIES: dict[str, bool] = {}


def current_capabilities() -> dict[str, bool]:
    """核心预置（恒空）+ 插件声明的能力聚合（能力名由插件任意声明）。"""
    caps = dict(BASE_CAPABILITIES)
    for name, enabled in HOOKS.capabilities.items():
        caps[name] = bool(enabled)
    return caps


__all__ = ["BASE_CAPABILITIES", "current_capabilities"]
