#!/usr/bin/env python3
"""禁词扫描门禁（AGENTS.md 铁律 3 的落地实现）。

扫描"仓库内容"中的禁词模式（供应商专有词与内部代号），命中即非 0 退出。
只依赖 Python 标准库（git 仅作外部命令调用），可在 CI 与本地直接运行：

    python scripts/banned_words_lint.py

设计约定
--------
- 扫描范围：**仓库内容**（已跟踪文件 + 未忽略的未跟踪文件），由
  ``git ls-files -z --cached --others --exclude-standard`` 权威列出；
  gitignored 的本地配置（如 ``.env``——BYOK 端点/Key 所在，出现任意
  供应商文案都是正常的）**不参与扫描**；``.env.example`` 已跟踪、是会
  发布的模板，**必须仍被扫描**。git 不可用（非 git 仓库等）时回退全树
  扫描，并保留显式排除（``.git/``、``node_modules/``、构建产物
  （``web-src/dist/``、``src/video_to_summary/web/static/assets/``、
  ``*.egg-info/``、``__pycache__/``、``.pytest_cache/`` 等）、运行时数据
  目录与本地配置 ``.env``）——扫描范围绝不因 git 环境问题收窄。
- 白名单：``scripts/oss_leak_allowlist.txt``（格式 ``路径glob|模式|理由``）。
  说明性合法表述必须**逐条登记**并写明理由，禁止用宽泛目录排除代替登记。
  本脚本与白名单文件自身豁免扫描（它们必然包含模式字面量）。
- 新增扫描模式：同步在 ``ALL_PATTERNS`` 追加，并在白名单中登记受影响的
  说明性表述；白名单中出现的模式必须存在于 ``ALL_PATTERNS``（防笔误）。
- 匹配方式：逐行子串匹配（区分大小写）；二进制文件（UTF-8 解码失败）跳过。
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
import sys

# 英文模式（内部代号 / 端点 / 通道名）
EN_PATTERNS: list[str] = [
    "VTS1",
    "_bootstrap",
    "official_gate",
    "OFFICIAL_PROFILE",
    "audit_url",
    "usage_audit",
    "usage_totals",
    "vts_commercial",
    "license_admin",
    "_e1",
    "_e2",
    "api.stepfun.com",
    "stepaudio",
    "step-router",
    "step-3.7",
    "LEGACY_API_PREFIX",
]

# 中文禁词（说明性合法表述须在白名单逐条登记豁免）
CN_PATTERNS: list[str] = [
    "官方通道",
    "官方中转",
    "激活码",
    "授权码",
    "扫码登录",
    "模型管理",
    "用量审计",
    "试用",
    "闭源商用",
    "License 门控",
]

ALL_PATTERNS: list[str] = EN_PATTERNS + CN_PATTERNS

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
ALLOWLIST_FILE = os.path.join(SCRIPT_DIR, "oss_leak_allowlist.txt")

# 排除的目录名（任意深度命中即跳过）：VCS / 依赖 / 构建产物 / 缓存 / 运行时数据
EXCLUDED_DIR_NAMES = {
    ".git",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "*.egg-info",
    "dist",      # web-src/dist 等前端构建产物
    "build",
    "output",    # 任务产物（gitignore）
    "data",      # SQLite 运行时数据（gitignore）
    ".cache",
    ".venv",
    "venv",
}

# 构建产物目录（相对仓库根的精确路径，兜底上面的目录名排除）
EXCLUDED_REL_DIRS = {
    os.path.join("src", "video_to_summary", "web", "static", "assets"),
    os.path.join("web-src", "dist"),
}

# 本工具自身豁免：脚本与白名单文件必然包含模式字面量
SELF_SKIP = {os.path.abspath(__file__), os.path.abspath(ALLOWLIST_FILE)}

TEXT_EXTENSIONS = {".py", ".md", ".txt", ".ts", ".tsx", ".js", ".mjs", ".css", ".html", ".yml", ".yaml", ".toml", ".json", ".sh", ".cfg", ".ini", ".example", ".lock"}


def _is_excluded(rel_path: str, abs_path: str) -> bool:
    if abs_path in SELF_SKIP:
        return True
    # 显式排除本地配置文件（保底）：.env 是 gitignored 的用户配置（BYOK
    # 端点/Key），任意供应商文案都可能合法出现，绝不参与"仓库内容"扫描。
    # 仅精确匹配 basename .env，不影响 .env.example（已跟踪模板，仍须扫描）。
    if os.path.basename(rel_path) == ".env":
        return True
    parts = rel_path.split(os.sep)
    if any(fnmatch.fnmatch(part, pat) for part in parts for pat in EXCLUDED_DIR_NAMES):
        return True
    for excluded_dir in EXCLUDED_REL_DIRS:
        if rel_path == excluded_dir or rel_path.startswith(excluded_dir + os.sep):
            return True
    return False


def _git_repo_content(root: str) -> list[str] | None:
    """取"仓库内容"相对路径清单：已跟踪 + 未忽略的未跟踪文件。

    实现：``git ls-files -z --cached --others --exclude-standard``
    （--exclude-standard 使未跟踪文件按 .gitignore 过滤，故 .env 等
    gitignored 本地配置不在清单内；已跟踪文件无条件在列，.env.example
    仍会被扫描）。返回 None 表示 git 不可用（非 git 仓库 / git 缺失 /
    调用失败），由调用方回退全树扫描——绝不因 git 环境问题扫得更少。
    """
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    # -z 输出 NUL 分隔；路径相对仓库根、以 / 分隔，统一为 os.sep
    return [
        p.decode("utf-8", "replace").replace("/", os.sep)
        for p in proc.stdout.split(b"\0")
        if p
    ]


def _iter_text_files(root: str):
    """遍历"仓库内容"中的文本文件（相对路径 + 绝对路径）。

    首选 git 判定（见 _git_repo_content）：只扫已跟踪 + 未忽略的未跟踪
    文件，gitignored 的本地配置（如 .env）天然不参与；git 不可用时回退
    全树 os.walk（显式排除仍生效），保证扫描范围绝不因此收窄。
    """
    repo_files = _git_repo_content(root)
    if repo_files is not None:
        for rel in repo_files:
            abs_path = os.path.join(root, rel)
            if _is_excluded(rel, abs_path):
                continue
            ext = os.path.splitext(os.path.basename(rel))[1].lower()
            if ext and ext not in TEXT_EXTENSIONS:
                continue
            yield rel, abs_path
        return

    print(
        "[scan] git 不可用（非 git 仓库或调用失败），回退全树扫描："
        "无法按 .gitignore 过滤，仍显式跳过本地配置 .env",
        file=sys.stderr,
    )
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        # 剪枝：整目录被排除时不进入
        kept: list[str] = []
        for name in dirnames:
            rel = os.path.join(rel_dir, name) if rel_dir != "." else name
            if _is_excluded(rel, os.path.join(dirpath, name)):
                continue
            kept.append(name)
        dirnames[:] = kept
        for fname in filenames:
            rel = os.path.join(rel_dir, fname) if rel_dir != "." else fname
            abs_path = os.path.join(dirpath, fname)
            if _is_excluded(rel, abs_path):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext and ext not in TEXT_EXTENSIONS:
                continue
            yield rel, abs_path


def _read_lines(path: str) -> list[str] | None:
    """读取文本文件；非 UTF-8 文本（二进制）返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().splitlines()
    except (UnicodeDecodeError, OSError):
        return None


def load_allowlist() -> list[tuple[str, str, str]]:
    """解析白名单文件：[(路径glob, 模式, 理由), ...]。"""
    entries: list[tuple[str, str, str]] = []
    if not os.path.exists(ALLOWLIST_FILE):
        return entries
    with open(ALLOWLIST_FILE, "r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) < 3:
                print(f"[allowlist] 第 {lineno} 行格式错误（应为 路径glob|模式|理由）：{line}", file=sys.stderr)
                sys.exit(2)
            path_glob, pattern, reason = parts[0].strip(), parts[1].strip(), "|".join(parts[2:]).strip()
            if pattern not in ALL_PATTERNS:
                print(
                    f"[allowlist] 第 {lineno} 行模式 {pattern!r} 不在 ALL_PATTERNS 中"
                    f"（新增模式必须先在脚本中登记）",
                    file=sys.stderr,
                )
                sys.exit(2)
            entries.append((path_glob, pattern, reason))
    return entries


def main() -> int:
    allowlist = load_allowlist()
    leaks: list[tuple[str, str, list[int]]] = []

    for rel, abs_path in _iter_text_files(REPO_ROOT):
        lines = _read_lines(abs_path)
        if lines is None:
            continue
        for pattern in ALL_PATTERNS:
            hit_lines = [i for i, line in enumerate(lines, 1) if pattern in line]
            if not hit_lines:
                continue
            if any(fnmatch.fnmatch(rel, glob) and pat == pattern for glob, pat, _ in allowlist):
                continue  # 白名单登记：说明性合法表述
            leaks.append((rel, pattern, hit_lines))

    if leaks:
        print("禁词扫描未通过，命中禁词（白名单未登记）：")
        for rel, pattern, lines in sorted(leaks):
            shown = ",".join(str(n) for n in lines[:10])
            extra = "…" if len(lines) > 10 else ""
            print(f"  {rel}:{shown}{extra}  模式={pattern}")
        print(f"共 {len(leaks)} 个（文件, 模式）组合；若属说明性合法表述，请在 "
              f"{os.path.relpath(ALLOWLIST_FILE, REPO_ROOT)} 逐条登记。")
        return 1

    print(f"禁词扫描通过：0 命中（白名单登记 {len(allowlist)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
