import hashlib
import json
import re
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


def test_compiler_location_uses_actual_environment(monkeypatch, tmp_path):
    from scripts.build_desktop import msys2_root
    custom = tmp_path / "Action compiler location"
    monkeypatch.setenv("VTS_MSYS2_ROOT", str(custom))
    assert msys2_root() == custom
    monkeypatch.delenv("VTS_MSYS2_ROOT")
    assert msys2_root().as_posix() == "C:/msys64"
