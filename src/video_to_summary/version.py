"""应用版本解析：发布构建可生成 `_version.py`（git tag），开发态回落。

优先级（env 每次调用实时读取；昂贵回退链命中即缓存，进程内只解析一次）：
1. ``VIDEO_TO_SUMMARY_VERSION`` 环境变量 —— 外部构建可注入**自己的**版本号
   （非空即返回，**不缓存**，同进程内可动态覆盖/撤销，供运行期注入版本；
   包内 ``_version.py`` 位于 site-packages，不应被外部构建写入，
   此变量即为此设计的通用扩展点）；
2. `_version.py` 的 ``VERSION`` —— 发布构建在构建期用
   ``git describe --tags --always --dirty`` 生成（gitignore 不入库），
   发布包走此分支，显示的即 tag 版本号；
3. 开发态 git describe（源码树内直接运行，无 tag 时 --always 回落短 hash）；
4. 包安装元数据（importlib.metadata，= pyproject 静态版本，通常滞后，仅兜底）；
5. 全部失败回落 ``"dev"``。

2~5 为昂贵回退链（含 git 子进程/包元数据查询），结果缓存于 ``_cached``。
"""

import logging
import os

logger = logging.getLogger("video_to_summary.version")

_cached: str | None = None


def _build_version() -> str:
    """构建期生成的版本（_version.py 缺失 = 开发态，静默跳过）。"""
    try:
        from ._version import VERSION  # noqa: PLC0415 - 惰性导入，缺失属正常
    except Exception:  # noqa: BLE001
        return ""
    return (VERSION or "").strip()


def _git_version() -> str:
    """开发态 git describe（非 git 目录 / 无 git 命令时静默跳过）。"""
    try:
        import subprocess
        from pathlib import Path

        from .utils import subprocess_no_window_kwargs

        out = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True,
            text=True,
            timeout=5,
            **subprocess_no_window_kwargs(),
        )
    except Exception:  # noqa: BLE001
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _metadata_version() -> str:
    try:
        from importlib.metadata import version

        return version("vts")
    except Exception:  # noqa: BLE001
        return ""


def get_version() -> str:
    """当前应用版本号：构建包 = git tag（如 ``v0.3.0``），开发态 = git describe。

    外部构建可注入自己的版本号：设置环境变量 ``VIDEO_TO_SUMMARY_VERSION``（非空即返回），
    避免外部构建写入 site-packages 中的 ``_version.py``。env 每次调用都实时读取、
    **不缓存**，同进程内动态覆盖/撤销均立即生效；昂贵回退链见 ``_cached_version()``。
    """
    env_version = os.environ.get("VIDEO_TO_SUMMARY_VERSION", "").strip()
    if env_version:
        return env_version
    return _cached_version()


def _cached_version() -> str:
    """昂贵回退链：`_version.py` → git describe → 包元数据 → ``"dev"``，结果进程内缓存。

    只缓存回退链结果，绝不缓存 env 覆盖（env 在 ``get_version`` 入口实时读取）。
    """
    global _cached
    if _cached is not None:
        return _cached
    for layer in (_build_version, _git_version, _metadata_version):
        v = layer()
        if v:
            _cached = v
            return v
    _cached = "dev"
    return _cached
