"""tests/ 共享 fixture。

`fresh_ring`：隔离 `log_export` 的模块级 ring buffer 单例——清理所有 logger 上
已挂的 InMemoryLogHandler、重置 `_ring_handler`；teardown 摘除本用例产生的
handler 并把 `_ring_handler` 置回 None（下次 attach 会重建并重新挂载）。

供 `test_log_export.py` 与 `test_web.py` 的日志导出用例使用：全量跑套件时
e2e 用例会先产生海量日志填满共享 buffer，导出类断言必须先 fresh 再植入样本。

`_no_plugins`：session 级 autouse——整套测试假定"0 插件"（干净构建），
屏蔽运行环境里任何已安装的 `vts.plugins` 插件包（例如装有第三方插件的 venv）。
否则 `web/app.py` 导入期的 `load_plugins()` 会真实加载它们，污染路由/
能力声明/DB 迁移等全部测试结论。插件相关行为一律走 `load_plugins(entries=...)`
显式注入测试（见 test_hooks.py）。
"""

import logging
from unittest import mock

import pytest

from video_to_summary import db, log_export
from video_to_summary.log_export import InMemoryLogHandler
from video_to_summary.web import hooks

_RING_LOGGERS = ("", "uvicorn", "uvicorn.error")

# 屏蔽环境插件：补丁必须在 conftest **导入期**（早于任何测试模块收集）就生效。
# test_web.py 在模块顶层 `from video_to_summary.web.app import app`，导入即触发
# `load_plugins()`；若只靠 session fixture，收集阶段就会把运行环境里已安装的
# vts.plugins 插件真实加载进进程（连带设置 VIDEO_TO_SUMMARY_VERSION 等环境
# 副作用），污染路由/能力声明/DB 迁移/版本解析等全部测试结论。
_no_plugins_patcher = mock.patch.object(hooks, "_iter_entry_points", return_value=[])
_no_plugins_patcher.start()


@pytest.fixture(scope="session", autouse=True)
def _no_plugins():
    """session 级 autouse：整套测试假定"0 插件"（干净构建）。

    补丁在 conftest 导入期已 start（见模块注释——必须早于收集）；本 fixture
    兜底校验约定仍生效，并保证只替换发现入口 `hooks._iter_entry_points`——
    `load_plugins(entries=...)` 显式注入接缝不受影响，test_hooks.py 的
    fail-closed 用例仍会按注入的条目真实加载/验证。
    """
    assert hooks._iter_entry_points() == []  # 约定必须成立：发现恒为空
    yield
    assert hooks._iter_entry_points() == []


def _strip_ring_handlers() -> None:
    for name in _RING_LOGGERS:
        target = logging.getLogger(name)
        for handler in list(target.handlers):
            if isinstance(handler, InMemoryLogHandler):
                target.removeHandler(handler)


@pytest.fixture()
def fresh_ring():
    _strip_ring_handlers()
    log_export._ring_handler = None
    yield
    _strip_ring_handlers()
    log_export._ring_handler = None


@pytest.fixture()
def web_client(monkeypatch, tmp_path):
    """独立 Web 客户端：tmp DB + 双处 patch enqueue_job（不真实调度）。"""
    from fastapi.testclient import TestClient

    from video_to_summary.web import app as web_app
    from video_to_summary.web import tasks as web_tasks

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "app.db")
    monkeypatch.setattr(db, "LEGACY_LLM_FILE", tmp_path / "missing_llm.json")
    monkeypatch.setattr(db, "LEGACY_TEMPLATE_FILE", tmp_path / "missing_tpl.json")
    db.reset()
    web_tasks._jobs.clear()
    web_tasks._cancel_flags.clear()
    monkeypatch.setattr(web_app, "enqueue_job", lambda job: None)
    monkeypatch.setattr(web_tasks, "enqueue_job", lambda job: None)
    with TestClient(web_app.app) as client:
        yield client
    db.reset()
