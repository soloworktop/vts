"""打包静态资源一致性门禁（防「只在打包路径暴露」的缺陷回归）。

背景：setuptools 的 ``package-data`` glob **不递归** —— ``web/static/*`` 只匹配
``web/static/`` 顶层文件，``web/static/assets/``（前端构建产物的 JS/CSS）不会进包。
若 package-data 漏掉 ``assets/``，**非 editable 安装**（如 Docker stage2 的
``pip install .``，装入 site-packages）后运行时 ``/static/assets/*`` 404 →
首页白屏；而 editable 安装（README 主推的 ``pip install -e .``）与全部现有测试都
从源码树读文件，完全看不出来——这是只在打包路径暴露的缺陷（Docker 实测白屏根因）。

选型取舍：这里**不做「在测试里构建 wheel」**的门禁，改用静态一致性断言，因为：

1. 本套件约定全离线（见根 AGENTS.md「默认全离线」）；``pip wheel .`` 默认 build
   isolation 需联网拉 setuptools/wheel，``--no-build-isolation`` 又要求测试环境
   预装 setuptools/wheel（当前 dev 环境两者都缺）。
2. ``pip wheel`` 会在工作树生成 ``build/`` 与 ``*.egg-info``，污染被测试覆盖的目录。
3. 打包正确性本身由发布流程的 wheel 实测覆盖（构建 wheel + zipfile 校验 + 非
   editable 全新 venv 端到端跑服务）；本测试负责把「package-data 必须覆盖
   ``web/static/`` 全树（含 ``assets/``）」固化成回归门禁。

断言语义严格对齐 setuptools：``*`` 不跨 ``/``、``**`` 可跨任意层。能拦住本次缺陷
——把 ``web/static/assets/*`` 从 pyproject 的 package-data 删除时本测试必须失败
（已实测验证，见修复报告）。
"""

import fnmatch
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PYPROJECT = REPO / "pyproject.toml"
STATIC_DIR = REPO / "src" / "video_to_summary" / "web" / "static"

#: 必须被打包的模式层入口（git 跟踪、CI 恒存在）：static 顶层入口页
REQ_TOP_LEVEL = "web/static/index.html"
#: 必须被打包的模式层入口：assets/ 子目录（构建产物，gitignore，CI 检出时可能不在磁盘；
#: 用规范路径做结构性断言，不依赖磁盘上是否有构建产物）
REQ_ASSETS_PATH = "web/static/assets/index-hash.js"


def _load_package_data_patterns() -> list[str]:
    """读取 pyproject.toml ``[tool.setuptools.package-data] video_to_summary`` 模式列表。"""
    text = PYPROJECT.read_text(encoding="utf-8")
    try:  # Python 3.11+ 标准库
        import tomllib

        data = tomllib.loads(text)
        patterns = data["tool"]["setuptools"]["package-data"]["video_to_summary"]
        return [str(p) for p in patterns]
    except ModuleNotFoundError:  # Python 3.10：本 section 受控，极简回退解析
        pass
    section_lines: list[str] = []
    in_section = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[tool.setuptools.package-data]"):
            in_section = True
            continue
        if in_section and line.startswith("["):
            break
        if in_section and line:
            section_lines.append(line)
    for line in section_lines:
        key, _, rest = line.partition("=")
        if key.strip() == "video_to_summary":
            body = rest.strip()
            body = body[body.find("[") + 1 : body.rfind("]")] if "[" in body else body
            return [v.strip().strip('"').strip("'") for v in body.split(",") if v.strip()]
    raise AssertionError("pyproject.toml 未找到 [tool.setuptools.package-data] video_to_summary 模式")


def _matches(pattern: str, relpath: str) -> bool:
    """按 setuptools package-data 的 glob 语义匹配：``*`` 不跨 ``/``，``**`` 跨任意层。"""
    pat_parts = pattern.split("/")
    path_parts = relpath.split("/")

    def rec(pp: list[str], pa: list[str]) -> bool:
        if not pp:
            return not pa
        head = pp[0]
        if head == "**":
            return rec(pp[1:], pa) or (bool(pa) and rec(pp, pa[1:]))
        if not pa:
            return False
        return fnmatch.fnmatchcase(pa[0], head) and rec(pp[1:], pa[1:])

    return rec(pat_parts, path_parts)


def test_package_data_covers_static_assets_subdir() -> None:
    """package-data 必须同时覆盖顶层入口页与 assets/ 子目录（结构性门禁，不依赖磁盘构建产物）。

    setuptools 的 glob 不递归：只写 ``web/static/*`` 时 ``assets/`` 的 JS/CSS 不进包，
    非 editable 安装（Docker ``pip install .``）首页白屏。此断言在 CI（assets/ 因
    gitignore 不在磁盘）同样生效。
    """
    patterns = _load_package_data_patterns()
    assert patterns, "package-data 必须声明至少一个 web/static 模式"

    assert any(_matches(p, REQ_TOP_LEVEL) for p in patterns), (
        f"package-data 模式 {patterns!r} 未覆盖顶层入口 {REQ_TOP_LEVEL}"
    )

    assert any(_matches(p, REQ_ASSETS_PATH) for p in patterns), (
        f"package-data 模式 {patterns!r} 未覆盖 assets/ 子目录（如 {REQ_ASSETS_PATH}）。\n"
        "setuptools 的 package-data glob 不递归：`web/static/*` 不会把 assets/ 的 JS/CSS "
        "打进包——非 editable 安装（Docker `pip install .`）时 /static/assets/* 404，首页白屏。"
    )


def test_package_data_covers_every_static_file_on_disk() -> None:
    """补充门禁：磁盘上实际存在的每个 static 文件都必须被某个模式覆盖。

    捕获「新增顶层文件/子目录却忘了同步 package-data」的回归；CI 检出无构建产物
    （仅 README.md/user-guide.html）时自动放行，结构性断言由上一个用例兜底。
    """
    if not STATIC_DIR.exists():
        pytest.skip("static 目录不存在（源码检出未构建前端）")
    patterns = _load_package_data_patterns()
    rel_root = STATIC_DIR.parents[1]  # src/video_to_summary（web/static/... 相对 package 根）
    missing: list[str] = []
    for f in sorted(STATIC_DIR.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(rel_root).as_posix()
        if not any(_matches(p, rel) for p in patterns):
            missing.append(rel)
    assert not missing, (
        "以下 static 文件未被 package-data 覆盖（setuptools glob 不递归，需显式列出子目录）：\n  "
        + "\n  ".join(missing)
    )
