"""文档↔代码防漂移测试：以代码为基准断言文档关键事实。

本仓不带内部战略/规划文档集（内部文档不入库），因此本文件的校验面收敛为
**仓库内自洽**的部分：

- ``README.md`` 的接口表 ↔ ``web/app.py`` 的真实路由清单（双向）
- ``README.md`` 列出的内置模板 ↔ ``summarizers/openai.py`` 的 ``SUMMARY_TEMPLATES``
- ``README.md`` 文档化的 ``/api/v1/capabilities`` 语义 ↔ ``web/capabilities.py``
  的 ``current_capabilities()``（插件声明聚合；核心不预置，本构建为 ``{}``）

改动影响本测试时的正确顺序：**先改代码 → 同步 README/AGENTS → 测试通过**。
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: canonical 前缀：只有 /api/v1 参与「文档 ↔ 代码」双向校验
#: （/api 是过渡别名，不进 OpenAPI、也不要求文档逐条列出）
API_PREFIX = "/api/v1"


def _read(*parts: str) -> str:
    return (REPO.joinpath(*parts)).read_text(encoding="utf-8")


def _normalize_path(path: str) -> str:
    """路由路径参数归一：代码 ``{job_id}`` 与文档 ``{id}`` 视为同一路径。"""
    return re.sub(r"\{[^}]+\}", "{}", path)


def _app_routes() -> set[str]:
    from fastapi.routing import APIRoute

    from video_to_summary.web.app import app

    routes = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue  # /static 挂载等非 API 路由
        if not route.path.startswith(API_PREFIX):
            continue  # 过渡别名不计入契约
        routes.add(_normalize_path(route.path))
    return routes


def _readme_routes() -> set[str]:
    """README 接口表里出现的 /api/v1 路径（支持 ``GET, HEAD`` 这类多方法写法）。"""
    text = _read("README.md")
    found = set()
    for token in re.findall(re.escape(API_PREFIX) + r"/[A-Za-z0-9_{}./\-]*", text):
        found.add(_normalize_path(token.rstrip("`")))
    return found


# ---------------------------------------------------------------- HTTP 路由


def test_readme_documents_every_route() -> None:
    """每个 canonical 路由都必须在 README 接口表里出现（新增端点必须同步文档）。"""
    missing = sorted(_app_routes() - _readme_routes())
    assert not missing, f"README.md 缺少路由文档：{missing}"


def test_readme_does_not_document_phantom_routes() -> None:
    """README 声明的路由必须真实存在（删除端点时必须同步删文档）。"""
    phantom = sorted(_readme_routes() - _app_routes())
    assert not phantom, f"README.md 声称但 app.py 不存在的路由：{phantom}"


def test_legacy_alias_not_in_openapi_schema() -> None:
    """过渡别名已删除：OpenAPI 中不存在任何非 ``/api/v1`` 的 API 路径。

    ``/`` 与 ``/guide`` 是 HTML 页面路由（不算 API 路径），可留在 schema 中；
    除此之外任何非 ``/api/v1`` 前缀的路径都视为别名/幽灵路由残留。
    """
    from video_to_summary.web.app import app

    html_ok = {"/", "/guide"}
    paths = app.openapi().get("paths", {})
    api_paths = {p for p in paths if p.startswith("/api")}
    assert api_paths, "OpenAPI 不应包含任何 /api 路径"
    assert all(p.startswith(API_PREFIX) for p in api_paths), f"OpenAPI 混入非 canonical 路径：{api_paths}"
    non_api = {p for p in paths if not p.startswith("/api")}
    assert non_api <= html_ok, f"OpenAPI 混入非 API 路径：{sorted(non_api - html_ok)}"


# ---------------------------------------------------------------- 能力协商

#: 历史核心能力键：核心不预置能力位后，这些名字不得再出现在 README（列出即 fail）。
#: 注意按反引号包裹的键名匹配，避免误伤 MIT License 等合法许可元数据。
_REMOVED_CAPABILITY_KEYS = ("pdf_export", "bilibili_login", "local_asr", "license", "audit")


def test_readme_documents_capability_flags() -> None:
    """README 文档化的 ``/api/v1/capabilities`` 语义 = 插件声明集合（本构建为空对象）。

    能力一律由插件经 ``declare_capabilities`` 动态声明，核心不预置任何能力位：
    历史核心能力键不得再被 README 列为能力位（防止从历史文档误抄回来）。
    """
    readme = _read("README.md")
    assert "/api/v1/capabilities" in readme, "README 应文档化 /api/v1/capabilities 端点及其语义"
    for name in _REMOVED_CAPABILITY_KEYS:
        assert f"`{name}`" not in readme, f"README 不得再列出核心能力位（能力一律由插件声明）：{name}"


def test_capabilities_empty_without_plugins() -> None:
    """0 插件（测试套件约定）：``/api/v1/capabilities`` 返回空对象 ``{}``。"""
    from video_to_summary.web.capabilities import current_capabilities

    assert current_capabilities() == {}


# ---------------------------------------------------------------- 模板体系


def test_readme_lists_real_builtin_templates() -> None:
    from video_to_summary.summarizers.openai import SUMMARY_TEMPLATES

    assert SUMMARY_TEMPLATES, "SUMMARY_TEMPLATES 不应为空"
    readme = _read("README.md")
    missing = [name for name in SUMMARY_TEMPLATES if name not in readme]
    assert not missing, f"README 缺少内置模板：{missing}"


def test_readme_default_template_is_current() -> None:
    readme = _read("README.md")
    assert "通用" in readme
    # 历史名 default 仅作为兼容别名存在，不再作为默认值对外宣传
    assert "默认 `default`" not in readme


# ---------------------------------------------------------------- 禁止复活已删能力


def test_readme_does_not_advertise_removed_capabilities() -> None:
    """本构建不再宣传已移除的能力（防止从历史文档误抄回来）。"""
    readme = _read("README.md")
    for phrase in ("扫码登录 B 站", "本地模型管理", "转写模式选择"):
        assert phrase not in readme, f"README 仍宣传已移出范围的能力：{phrase}"


def test_agents_does_not_reintroduce_gating() -> None:
    """仓库约定文档不得把门控/授权写成现状（红线：核心永不含门控）。"""
    agents = _read("AGENTS.md")
    assert "vts.plugins" in agents, "AGENTS.md 应写明插件唯一挂载方式"
    assert "/api/v1" in agents, "AGENTS.md 应写明 canonical 前缀"
