"""OpenAPI 契约快照测试（防接口漂移）。

- 断言 ``app.openapi()`` 的路径集合与仓库内快照 ``tests/openapi_paths.json`` 完全一致
  （路径参数统一归一为 ``{}``，重命名路径参数不产生无意义快照漂移）。
- 断言不存在任何非 ``/api/v1`` 前缀的 **API** 路径（``/`` 与 ``/guide`` 两个 HTML
  路由除外）——过渡别名 / 幽灵路由残留会在这里被拦下。

接口有意的增删改时，更新快照二选一（改完人工 review diff 后重跑本测试确认）：

    VTS_UPDATE_OPENAPI_SNAPSHOT=1 python -m pytest tests/test_openapi_contract.py
    python scripts/snapshot_openapi.py
"""

import json
import os
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SNAPSHOT = REPO / "tests" / "openapi_paths.json"
API_PREFIX = "/api/v1"
# 允许出现在 OpenAPI 中的非 API 路径（HTML 页面路由）
HTML_OK = {"/", "/guide"}


def _normalize(path: str) -> str:
    """路径参数归一：``{job_id}`` 与 ``{label_id}`` 等视为同一路径 ``{}``。"""
    return re.sub(r"\{[^}]+\}", "{}", path)


def _openapi_paths() -> list[str]:
    from video_to_summary.web.app import app

    return sorted(_normalize(p) for p in app.openapi().get("paths", {}))


def _load_snapshot() -> list[str]:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def test_openapi_matches_snapshot() -> None:
    current = _openapi_paths()
    if os.environ.get("VTS_UPDATE_OPENAPI_SNAPSHOT"):
        SNAPSHOT.write_text(
            json.dumps(current, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        pytest.skip("已按请求更新 OpenAPI 快照；请人工 review diff 后重跑以确认")
    assert current == _load_snapshot(), (
        "OpenAPI 路径与快照不一致（接口漂移）。若为有意的接口变更，请更新快照：\n"
        "  VTS_UPDATE_OPENAPI_SNAPSHOT=1 python -m pytest tests/test_openapi_contract.py\n"
        "  或 python scripts/snapshot_openapi.py"
    )


def test_no_non_canonical_api_paths() -> None:
    """OpenAPI 中不存在任何非 ``/api/v1`` 前缀的 API 路径（``/`` 与 ``/guide`` 除外）。"""
    from video_to_summary.web.app import app

    paths = app.openapi().get("paths", {})
    api_paths = {p for p in paths if p.startswith("/api")}
    assert api_paths, "OpenAPI 不应为空（至少要有 /api/v1/* 路由）"
    non_canonical = sorted(p for p in api_paths if not p.startswith(API_PREFIX))
    assert not non_canonical, f"OpenAPI 混入非 canonical 路径：{non_canonical}"
    non_api = {p for p in paths if not p.startswith("/api")}
    assert non_api <= HTML_OK, f"OpenAPI 混入非 HTML 的非 API 路径：{sorted(non_api - HTML_OK)}"
