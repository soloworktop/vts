# Explicit resource list; no CWD data, environment files or desktop binaries in wheels.
import json
import os
import plistlib
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, copy_metadata

root = Path(SPECPATH).parents[1]
metadata = json.loads((root / "build/desktop/version.json").read_text())
platform_name = "windows-x64" if sys.platform == "win32" else "macos-arm64"
bin_dir = root / "build/desktop/bin" / platform_name
suffix = ".exe" if sys.platform == "win32" else ""
binaries = [(str(bin_dir / (name + suffix)), "bin") for name in ("ffmpeg", "ffprobe")]
datas = [(str(root / "src/video_to_summary/web/static"), "video_to_summary/web/static"),
         (str(root / "build/desktop/notices"), "notices"),
         (str(root / "build/desktop/version.json"), "notices")]
datas += copy_metadata("vts")
hidden = collect_submodules("video_to_summary")
hidden += ["webview.platforms.winforms", "webview.platforms.edgechromium", "clr"] if sys.platform == "win32" else ["webview.platforms.cocoa"]
a = Analysis([str(root / "src/video_to_summary/desktop/__main__.py")], pathex=[str(root / "src")],
             binaries=binaries, datas=datas, hiddenimports=hidden,
             excludes=["pytest", "tkinter", "PySide6", "PyQt6", "PyQt5", "IPython", "notebook", "webview.platforms.qt", "webview.platforms.gtk", "webview.platforms.mshtml"],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="VTS", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          icon=str(root / "packaging/desktop/icon.ico") if sys.platform == "win32" else None,
          version=str(root / "build/desktop/windows-version.txt") if sys.platform == "win32" else None,
          target_arch="arm64" if sys.platform == "darwin" else None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="VTS")
if sys.platform == "darwin":
    info = plistlib.loads((root / "packaging/desktop/macos/Info.plist").read_bytes())
    info.update(CFBundleShortVersionString=metadata["macos"], CFBundleVersion=metadata["macos"], VTSDisplayVersion=metadata["display"], VTSGitSHA=metadata["sha"])
    app = BUNDLE(coll, name="VTS.app", bundle_identifier=info["CFBundleIdentifier"],
                 icon=str(root / "packaging/desktop/icon.icns"), info_plist=info)
