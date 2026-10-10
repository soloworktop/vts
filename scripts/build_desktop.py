#!/usr/bin/env python3
"""Native desktop build. Hash verified sources, explicit resources, no user data."""
import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen
if __package__:
    from .prepare_version import prepare
else:
    from prepare_version import prepare

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging/desktop"
BUILD = ROOT / "build/desktop"
DIST = ROOT / "desktop-dist"


def digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def target_platform():
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return "macos-arm64"
    if sys.platform == "win32" and platform.machine().lower() in ("amd64", "x86_64"):
        return "windows-x64"
    raise RuntimeError("Desktop builds require macOS arm64 or Windows x64; cross freezing is unsupported")


def run(command, **kwargs):
    subprocess.run(list(map(str, command)), check=True, cwd=ROOT, **kwargs)


def fetch_verified(source, cache):
    path = cache / source["url"].rsplit("/", 1)[1]
    if path.is_file() and digest(path) == source["sha256"]:
        return path
    cache.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    try:
        request = Request(source["url"], headers={"User-Agent": "VTS-desktop-build"})
        with urlopen(request, timeout=30) as response, temporary.open("wb") as handle:
            shutil.copyfileobj(response, handle)
        if digest(temporary) != source["sha256"]:
            raise RuntimeError("Source SHA256 mismatch: " + path.name)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def msys2_root():
    return Path(os.environ.get("VTS_MSYS2_ROOT") or "C:/msys64")


def verify_binaries(directory, target):
    suffix = ".exe" if target == "windows-x64" else ""
    details = {}
    for name in ("ffmpeg", "ffprobe"):
        binary = directory / (name + suffix)
        version = subprocess.check_output([str(binary), "-version"], text=True, stderr=subprocess.STDOUT)
        if "version 9.0.2" not in version or "--enable-gpl" in version or "--enable-nonfree" in version:
            raise RuntimeError("Unexpected FFmpeg version/license configuration")
        tls = "--enable-securetransport" if target == "macos-arm64" else "--enable-schannel"
        if tls not in version:
            raise RuntimeError("Native TLS is required for existing HTTPS/HLS functionality")
        if target == "macos-arm64":
            if "arm64" not in subprocess.check_output(["file", str(binary)], text=True):
                raise RuntimeError("FFmpeg is not arm64")
            dependencies = subprocess.check_output(["otool", "-L", str(binary)], text=True)
            for line in dependencies.splitlines()[1:]:
                library = line.strip().split(" (", 1)[0]
                if not library.startswith(("/usr/lib/", "/System/Library/")):
                    raise RuntimeError("Non-system FFmpeg dependency: " + library)
        else:
            dependencies = subprocess.check_output([str(msys2_root() / "mingw64/bin/objdump.exe"), "-p", str(binary)], text=True)
            allowed = {"kernel32.dll", "msvcrt.dll", "advapi32.dll", "ws2_32.dll", "secur32.dll", "bcrypt.dll", "user32.dll", "ole32.dll", "shell32.dll", "psapi.dll", "ntdll.dll", "crypt32.dll", "gdi32.dll", "winmm.dll", "shlwapi.dll"}
            if "pei-x86-64" not in dependencies:
                raise RuntimeError("FFmpeg is not Windows x64")
            for library in re.findall(r"DLL Name:\s*(\S+)", dependencies):
                if library.lower() not in allowed and not library.lower().startswith(("api-ms-win-", "ext-ms-win-")):
                    raise RuntimeError("Non-system FFmpeg DLL: " + library)
        details[name] = {"sha256": digest(binary), "version": version.splitlines()[0]}
    protocols = subprocess.check_output([str(directory / ("ffmpeg" + suffix)), "-protocols"], text=True, stderr=subprocess.DEVNULL)
    if not re.search(r"^\s+https$", protocols, re.M):
        raise RuntimeError("FFmpeg HTTPS protocol is unavailable")
    return details


def build_ffmpeg(target):
    manifest = json.loads((PACKAGING / "binaries.json").read_text())
    recipe = PACKAGING / manifest["recipe"].rsplit("/", 1)[1]
    fingerprint = hashlib.sha256((digest(recipe) + json.dumps(manifest, sort_keys=True)).encode()).hexdigest()
    cache = ROOT / ".desktop-cache"
    sources = cache / "sources"
    for source in manifest["sources"]:
        fetch_verified(source, sources)
    destination = cache / "ffmpeg" / target / fingerprint[:16]
    evidence = destination / "build-manifest.json"
    if evidence.is_file():
        previous = json.loads(evidence.read_text())
        if previous.get("fingerprint") == fingerprint and previous.get("binaries") == verify_binaries(destination, target):
            return destination
        raise RuntimeError("Cached binaries failed provenance/hash validation; remove this build cache")
    destination.mkdir(parents=True, exist_ok=True)
    command = [str(recipe), str(sources), str(destination), target]
    if target == "windows-x64":
        command = [str(msys2_root() / "usr/bin/bash.exe"), "-l", *[v.replace("\\", "/") for v in command]]
    else:
        command = ["bash", *command]
    env = dict(os.environ)
    if target == "windows-x64":
        env.update(MSYSTEM="MINGW64", CHERE_INVOKING="1")
    with (destination / "build.log").open("w", encoding="utf-8") as log:
        run(command, env=env, stdout=log, stderr=subprocess.STDOUT)
    details = verify_binaries(destination, target)
    shutil.rmtree(destination / "work", ignore_errors=True)
    evidence.write_text(json.dumps({"fingerprint": fingerprint, "binaries": details, "sources": manifest["sources"], "target": target}, indent=2) + "\n")
    return destination


def notices(binary_directory):
    output = BUILD / "notices"
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    for name in ("THIRD_PARTY_NOTICES.txt", "binaries.json", "build_ffmpeg.sh", "requirements-macos.lock", "requirements-windows.lock"):
        shutil.copy2(PACKAGING / name, output / name)
    shutil.copytree(PACKAGING / "licenses", output / "licenses")
    python_licenses = [Path(sys.base_prefix) / "LICENSE.txt", Path(sys.base_prefix) / "Lib/LICENSE.txt",
                       Path(sys.base_prefix) / "lib/python3.12/LICENSE.txt"]
    python_license = next((p for p in python_licenses if p.is_file()), None)
    if python_license is None:
        raise RuntimeError("Python runtime license is missing; refusing redistribution")
    shutil.copy2(python_license, output / "licenses/Python-LICENSE.txt")
    for name in ("buildinfo.txt", "build-manifest.json"):
        shutil.copy2(binary_directory / name, output / name)
    distributions = []
    allowed = set()
    for lock in PACKAGING.glob("requirements-*.lock"):
        allowed.update(re.findall(r"^([A-Za-z0-9_-]+)==", lock.read_text(), re.M))
    normalize = lambda s: re.sub(r"[-_.]+", "-", s.lower())
    allowed = {normalize(s) for s in allowed} | {"vts", "pip"}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata["Name"]
        vendored = "setuptools/_vendor" in str(getattr(distribution, "_path", "")).replace("\\", "/")
        if normalize(name) not in allowed and not vendored:
            raise RuntimeError(f"Uncontrolled build dependency {name}; use a clean environment installed from the lock")
        distributions.append({"name": name, "version": distribution.version, "license": distribution.metadata.get("License-Expression") or distribution.metadata.get("License")})
        for file in distribution.files or ():
            if any(part.lower().startswith(("license", "copying", "notice")) for part in file.parts) and (".dist-info/" in str(file) or ".egg-info/" in str(file)):
                source = distribution.locate_file(file)
                if source.is_file():
                    dest = output / "licenses" / name / Path(*file.parts)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, dest)
    (output / "python-distributions.json").write_text(json.dumps(distributions, indent=2) + "\n")


def windows_version(info):
    numbers = tuple(map(int, info["windows"].split(".")))
    (BUILD / "windows-version.txt").write_text(f'''VSVersionInfo(ffi=FixedFileInfo(filevers={numbers!r}, prodvers={numbers!r}, mask=0x3f, flags=0, OS=0x40004, fileType=0x1, subtype=0, date=(0,0)), kids=[StringFileInfo([StringTable('040904B0', [StringStruct('FileDescription', 'VTS'), StringStruct('ProductName', 'VTS'), StringStruct('FileVersion', '{info["display"]}'), StringStruct('ProductVersion', '{info["display"]}')])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])])''', encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag")
    parser.add_argument("--installer", action="store_true")
    parser.add_argument("--binaries-only", action="store_true")
    args = parser.parse_args()
    started = time.monotonic()
    target = target_platform()
    if args.tag and subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip():
        raise RuntimeError("Release requires a clean source checkout")
    info = prepare(args.tag)
    BUILD.mkdir(parents=True, exist_ok=True)
    (BUILD / "version.json").write_text(json.dumps(info, indent=2) + "\n")
    source = build_ffmpeg(target)
    destination = BUILD / "bin" / target
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    for item in source.iterdir():
        if item.is_file() and item.name != "build.log":
            shutil.copy2(item, destination / item.name)
    if args.binaries_only:
        return
    notices(source)
    windows_version(info)
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", DIST, "--workpath", BUILD / "freeze", PACKAGING / "vts.spec"])
    if args.installer:
        if target == "windows-x64":
            compiler = shutil.which("ISCC") or "C:/Program Files (x86)/Inno Setup 6/ISCC.exe"
            run([compiler, f'/DDisplayVersion={info["display"]}', f'/DNumericVersion={info["windows"]}', f'/DBuildRoot={DIST}', f'/DOutputDir={DIST}', PACKAGING / "windows/installer.iss"])
        else:
            stage = BUILD / "dmg"
            if stage.exists():
                shutil.rmtree(stage)
            stage.mkdir()
            run(["ditto", DIST / "VTS.app", stage / "VTS.app"])
            (stage / "Applications").symlink_to("/Applications")
            run(["hdiutil", "create", "-volname", "VTS", "-srcfolder", stage, "-ov", "-format", "UDZO", DIST / f'vts-{info["display"]}-macos-arm64.dmg'])
    artifacts = {}
    for item in DIST.iterdir():
        artifacts[item.name] = item.stat().st_size if item.is_file() else sum(
            p.stat().st_size for p in item.rglob("*") if p.is_file() and not p.is_symlink())
    metrics = {"target": target, "seconds": round(time.monotonic() - started, 1), "artifacts_bytes": artifacts}
    (BUILD / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    print(json.dumps(metrics))


if __name__ == "__main__":
    main()
