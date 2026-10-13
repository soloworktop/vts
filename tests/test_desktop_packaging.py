import hashlib
import json
import re
import struct
from pathlib import Path
import pytest
from scripts.build_desktop import fetch_verified
from scripts.desktop_smoke import scan_sensitive

ROOT = Path(__file__).resolve().parents[1]


def test_source_manifest_license_and_tls():
    manifest = json.loads((ROOT / "packaging/desktop/binaries.json").read_text())
    assert manifest["mode"] == "source"
    assert set(manifest["targets"]) == {"windows-x64", "macos-arm64"}
    assert manifest["license"] == "LGPL-2.1-or-later"
    for source in manifest["sources"]:
        assert source["url"].startswith("https://") and "/latest/" not in source["url"]
        assert re.fullmatch("[0-9a-f]{64}", source["sha256"])
    recipe = (ROOT / manifest["recipe"]).read_text()
    assert "--enable-securetransport" in recipe and "--enable-schannel" in recipe
    assert "--disable-autodetect" in recipe and "--enable-nonfree" not in recipe


def test_source_integrity_checked_before_use(tmp_path, monkeypatch):
    import scripts.build_desktop as build
    source = {"url": "https://example.test/source.tar.gz", "sha256": hashlib.sha256(b"good").hexdigest()}
    (tmp_path / "source.tar.gz").write_bytes(b"bad")
    import io
    monkeypatch.setattr(build, "urlopen", lambda *a, **kw: io.BytesIO(b"bad"))
    with pytest.raises(RuntimeError, match="SHA256"):
        fetch_verified(source, tmp_path)
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("name", [".env", "app.db", "enc_key", "cookies.txt", "output/user.txt", ".venv/python"])
def test_artifact_rejects_sensitive_inputs(tmp_path, name):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("test")
    with pytest.raises(RuntimeError):
        scan_sensitive(tmp_path)


def test_freezing_has_explicit_resources_and_hashed_locks():
    spec = (ROOT / "packaging/desktop/vts.spec").read_text()
    assert "console=False" in spec and "upx=False" in spec
    assert "web/static" in spec and "copy_metadata" in spec
    for name in ("macos", "windows"):
        lock = (ROOT / f"packaging/desktop/requirements-{name}.lock").read_text()
        assert "--hash=sha256:" in lock
        assert "pyinstaller==6.22.3" in lock and "pywebview==6.2.1" in lock
        assert "cryptography==50.0.2" in lock
        assert not re.search(r"^\w+[<>=]([^=]|$)", lock, re.M)


def test_desktop_icon_assets_are_wired_into_both_packages():
    asset_dir = ROOT / "packaging/desktop"
    assert "<svg" in (asset_dir / "icon.svg").read_text()
    png = (asset_dir / "icon.png").read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", png[16:24]) == (1024, 1024)
    ico = (asset_dir / "icon.ico").read_bytes()
    assert struct.unpack("<HHH", ico[:6]) == (0, 1, 6)
    assert (asset_dir / "icon.icns").read_bytes()[:4] == b"icns"
    spec = (asset_dir / "vts.spec").read_text()
    assert "icon.ico" in spec and "icon.icns" in spec
    assert "SetupIconFile" in (asset_dir / "windows/installer.iss").read_text()


def test_compiler_location_uses_actual_environment(monkeypatch, tmp_path):
    from scripts.build_desktop import msys2_root
    custom = tmp_path / "Action compiler location"
    monkeypatch.setenv("VTS_MSYS2_ROOT", str(custom))
    assert msys2_root() == custom
    monkeypatch.delenv("VTS_MSYS2_ROOT")
    assert msys2_root().as_posix() == "C:/msys64"


@pytest.mark.parametrize("extra_dll", ["ncrypt.dll", "OLEAUT32.dll", "vfw32.dll", "AVICAP32.dll", "libwinpthread-1.dll"])
def test_windows_system_crypto_dependency_only(tmp_path, monkeypatch, extra_dll):
    import scripts.build_desktop as build
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        (tmp_path / name).write_bytes(b"fixture")
    def output(command, **kwargs):
        if "-version" in command:
            return "ffmpeg version 9.0.2\nconfiguration: --enable-schannel\n"
        if "-protocols" in command:
            return "Input:\n  https\n"
        return "file format pei-x86-64\nDLL Name: KERNEL32.dll\nDLL Name: " + extra_dll
    monkeypatch.setattr(build.subprocess, "check_output", output)
    if extra_dll != "libwinpthread-1.dll":
        assert set(build.verify_binaries(tmp_path, "windows-x64")) == {"ffmpeg", "ffprobe"}
    else:
        with pytest.raises(RuntimeError, match="Non-system FFmpeg DLL"):
            build.verify_binaries(tmp_path, "windows-x64")


def test_freeze_refreshes_stale_metadata_without_new_dependencies(monkeypatch):
    import scripts.build_desktop as build
    versions = iter(["0.1.0", "0.4.0rc2"])
    monkeypatch.setattr(build.importlib.metadata, "version", lambda _: next(versions))
    calls = []
    monkeypatch.setattr(build, "run", lambda command, **kwargs: calls.append((command, kwargs)))
    build.ensure_package_version({"package": "0.4.0rc2"})
    command, kwargs = calls[0]
    assert "--no-deps" in command and "--no-build-isolation" in command
    assert kwargs["env"]["SETUPTOOLS_SCM_PRETEND_VERSION_FOR_VTS"] == "0.4.0rc2"
    assert command[-1] == build.ROOT


def test_freeze_rejects_metadata_that_cannot_be_refreshed(monkeypatch):
    import scripts.build_desktop as build
    monkeypatch.setattr(build.importlib.metadata, "version", lambda _: "0.1.0")
    monkeypatch.setattr(build, "run", lambda *a, **kw: None)
    with pytest.raises(RuntimeError, match="metadata differs"):
        build.ensure_package_version({"package": "0.4.0"})
