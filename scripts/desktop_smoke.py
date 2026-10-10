#!/usr/bin/env python3
"""Smoke from frozen and installed artifacts; native GUI validation is mandatory."""
import argparse
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def scan_sensitive(directory):
    for path in directory.rglob("*"):
        name = path.name.lower()
        if name == ".env" or name.startswith(".env.") or name in ("enc_key", "cookies.txt") or name.endswith((".db", ".db-wal", ".db-shm", ".log")):
            raise RuntimeError("Sensitive build input in final artifact: " + str(path.relative_to(directory)))
        if any(part in ("output", "data", ".venv", "node_modules") for part in path.relative_to(directory).parts):
            raise RuntimeError("Unexpected data/build directory in artifact: " + str(path))


def alive(pid):
    if sys.platform == "win32":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 0x102
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def smoke(executable, gui, expected=None):
    if __package__:
        from .build_desktop import verify_binaries
    else:
        from build_desktop import verify_binaries
    directory = executable.parents[1] / "Frameworks/bin" if sys.platform == "darwin" else executable.parent / "_internal/bin"
    verify_binaries(directory, "macos-arm64" if sys.platform == "darwin" else "windows-x64")
    with tempfile.TemporaryDirectory(prefix="vts-install-smoke-") as temporary:
        data = Path(temporary)
        report = data / "report.json"
        suffix = ".exe" if sys.platform == "win32" else ""
        ffmpeg = str(directory / ("ffmpeg" + suffix))
        subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-f", "lavfi", "-i", "sine=duration=0.1", str(data / "input.wav")], check=True)
        subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-i", str(data / "input.wav"), "-vn", "-c:a", "libmp3lame", str(data / "prepared.mp3")], check=True)
        probe = subprocess.check_output([str(directory / ("ffprobe" + suffix)), "-v", "error", "-show_streams", "-of", "json", str(data / "prepared.mp3")], text=True)
        if not any(stream["codec_type"] == "audio" for stream in json.loads(probe)["streams"]):
            raise RuntimeError("Bundled FFmpeg audio conversion failed")
        env = dict(os.environ)
        for name in list(env):
            if name.startswith(("VTS_", "VIDEO_TO_SUMMARY_", "SUMMARY_", "ASR_", "LLM_", "OPENAI_", "POLISH_")):
                env.pop(name)
        env.update(VIDEO_TO_SUMMARY_DB=str(data / "app.db"), VIDEO_TO_SUMMARY_OUTPUT_DIR=str(data / "output"))
        process = subprocess.Popen([str(executable), "--desktop-gui-smoke" if gui else "--desktop-smoke", str(report)], env=env, cwd=data)
        try:
            process.wait(timeout=75)
            if process.returncode or not report.is_file():
                raise RuntimeError("Frozen app failed readiness/native smoke")
            result = json.loads(report.read_text())
            if not result.get("ok") or (gui and not result.get("bridge")):
                raise RuntimeError("Native smoke failed: " + str(result))
            if expected and result["version"] != expected:
                raise RuntimeError("Frozen version differs from release Tag")
            if alive(result["backend_pid"]):
                raise RuntimeError("Backend survived GUI exit")
            print(json.dumps(result))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--gui", action="store_true")
    parser.add_argument("--version")
    args = parser.parse_args()
    artifact = args.artifact.resolve()
    if artifact.suffix == ".dmg":
        with tempfile.TemporaryDirectory(prefix="vts-dmg-smoke-") as tmp:
            mount = Path(tmp) / "mount"
            subprocess.run(["hdiutil", "attach", str(artifact), "-mountpoint", str(mount), "-nobrowse"], check=True)
            try:
                installed = Path(tmp) / "VTS.app"
                subprocess.run(["ditto", str(mount / "VTS.app"), str(installed)], check=True)
                scan_sensitive(installed)
                smoke(installed / "Contents/MacOS/VTS", args.gui, args.version)
            finally:
                subprocess.run(["hdiutil", "detach", str(mount)], check=True)
    elif artifact.name.endswith("-setup.exe"):
        with tempfile.TemporaryDirectory(prefix="vts-exe-smoke-") as tmp:
            installed = Path(tmp) / "app"
            subprocess.run([str(artifact), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/DIR={installed}"], check=True)
            try:
                scan_sensitive(installed)
                smoke(installed / "VTS.exe", args.gui, args.version)
            finally:
                subprocess.run([str(installed / "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], check=True)
            if (installed / "VTS.exe").exists():
                raise RuntimeError("Installer uninstall failed")
    else:
        directory = artifact if artifact.is_dir() else artifact.parent
        scan_sensitive(directory)
        executable = directory / "Contents/MacOS/VTS" if directory.suffix == ".app" else directory / "VTS.exe"
        smoke(executable, args.gui, args.version)


if __name__ == "__main__":
    main()
