#!/usr/bin/env python3
"""Assemble, verify, upload to draft, verify again, then publish immutable assets."""
import argparse
import hashlib
import json
import subprocess
import tempfile
import zipfile
from pathlib import Path
if __package__:
    from .prepare_version import release_version
else:
    from prepare_version import release_version


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def expected_assets(info):
    return {f'vts-{info["package"]}-py3-none-any.whl', f'vts-{info["package"]}.tar.gz',
            f'vts-{info["display"]}-windows-x64-setup.exe', f'vts-{info["display"]}-macos-arm64.dmg'}


def assemble(directory, info):
    expected = expected_assets(info)
    actual = {p.name for p in directory.iterdir() if p.is_file() and p.name != "SHA256SUMS"}
    if actual != expected:
        raise RuntimeError(f"Incomplete/unexpected Release assets: missing={expected - actual}, extra={actual - expected}")
    wheel = next(directory.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        metadata = archive.read(next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))).decode()
        if f'\nVersion: {info["package"]}\n' not in metadata:
            raise RuntimeError("Python package version differs from release Tag")
    hashes = {name: checksum(directory / name) for name in sorted(expected)}
    (directory / "SHA256SUMS").write_text("".join(f"{digest}  {name}\n" for name, digest in hashes.items()))
    hashes["SHA256SUMS"] = checksum(directory / "SHA256SUMS")
    return hashes


def gh(*args):
    return subprocess.check_output(["gh", *args], text=True).strip()


def verify_remote(tag, hashes):
    with tempfile.TemporaryDirectory(prefix="vts-release-check-") as tmp:
        gh("release", "download", tag, "--dir", tmp)
        for name, expected in hashes.items():
            path = Path(tmp) / name
            if not path.is_file() or checksum(path) != expected:
                raise RuntimeError("Uploaded Release attachment failed hash verification: " + name)


def publish(tag, directory, info, mode, hashes):
    if mode == "build":
        return
    remote_sha = gh("api", "repos/{owner}/{repo}/commits/" + tag, "--jq", ".sha")
    if remote_sha != info["sha"]:
        raise RuntimeError("Remote Tag moved after version validation; refusing release")
    result = subprocess.run(["gh", "release", "view", tag, "--json", "isDraft,assets"], capture_output=True, text=True)
    if result.returncode:
        # Only a genuine 404 permits creation; transport/auth failures remain failures.
        if "404" not in result.stderr and "release not found" not in result.stderr.lower():
            raise RuntimeError("Cannot inspect existing Release: " + result.stderr)
        args = ["release", "create", tag, "--draft", "--verify-tag", "--title", tag, "--generate-notes"]
        if info["prerelease"]:
            args.append("--prerelease")
        gh(*args)
    else:
        existing = json.loads(result.stdout)
        if not existing["isDraft"]:
            if {a["name"] for a in existing["assets"]} != set(hashes):
                raise RuntimeError("Published Release assets differ; refusing mutation")
            verify_remote(tag, hashes)
            return
    gh("release", "upload", tag, "--clobber", *[str(directory / name) for name in hashes])
    uploaded = json.loads(gh("release", "view", tag, "--json", "isDraft,assets"))
    if {asset["name"] for asset in uploaded["assets"]} != set(hashes):
        raise RuntimeError("Draft contains unexpected attachments; remove them before publication")
    verify_remote(tag, hashes)
    if mode == "publish":
        gh("release", "edit", tag, "--draft=false", "--prerelease=" + str(info["prerelease"]).lower())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--directory", type=Path, default=Path("dist-release"))
    parser.add_argument("--mode", choices=("build", "draft", "publish"), default="build")
    args = parser.parse_args()
    info = release_version(args.tag, args.sha)
    hashes = assemble(args.directory, info)
    publish(args.tag, args.directory.resolve(), info, args.mode, hashes)
    print(json.dumps({"tag": args.tag, "mode": args.mode, "assets": hashes}, indent=2))


if __name__ == "__main__":
    main()
