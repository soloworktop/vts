#!/usr/bin/env python3
"""Resolve one version for packages, desktop metadata and Release assets."""
import argparse
import json
import re
import subprocess
from pathlib import Path
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
TAG = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-rc\.([1-9]\d*))?$")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def release_version(tag, sha):
    match = TAG.fullmatch(tag)
    if not match:
        raise ValueError("版本须为 vMAJOR.MINOR.PATCH 或 vMAJOR.MINOR.PATCH-rc.N")
    major, minor, patch, rc = match.groups()
    numbers = [int(major), int(minor), int(patch), int(rc or 0)]
    if any(n > 65535 for n in numbers):
        raise ValueError("版本号超出 Windows 数字版本范围")
    return {"display": tag, "package": str(Version(tag[1:])), "windows": ".".join(map(str, numbers)),
            "macos": f"{major}.{minor}.{patch}", "sha": sha, "prerelease": bool(rc)}


def prepare(tag=None):
    try:
        sha = git("rev-parse", "HEAD")
    except (subprocess.CalledProcessError, FileNotFoundError):
        sha = "unknown"
    if tag:
        if git("rev-parse", tag + "^{commit}") != sha:
            raise ValueError("指定 Tag 与当前源码不一致")
        info = release_version(tag, sha)
    else:
        from setuptools_scm import get_version
        package = get_version(root=str(ROOT), fallback_version="0.0.0.dev0")
        try:
            display = git("describe", "--tags", "--always", "--dirty")
        except (subprocess.CalledProcessError, FileNotFoundError):
            display = "0.0.0.dev0"
        info = {"display": display, "package": package, "windows": "0.0.0.0", "macos": "0.0.0", "sha": sha, "prerelease": True}
    (ROOT / "src/video_to_summary/_version.py").write_text(f'VERSION = {info["display"]!r}\n', encoding="utf-8")
    return info


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag")
    parser.add_argument("--output", type=Path, default=ROOT / "build/desktop/version.json")
    args = parser.parse_args()
    info = prepare(args.tag)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(info))


if __name__ == "__main__":
    main()
