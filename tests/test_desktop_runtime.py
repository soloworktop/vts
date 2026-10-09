from pathlib import Path

from video_to_summary.db import resolve_database_path
from video_to_summary.desktop.paths import DesktopPaths
from video_to_summary.desktop.backend import Backend
from video_to_summary.desktop.logging import RedactedFormatter
import logging


def test_user_data_bypasses_checkout(tmp_path):
    assert resolve_database_path(use_user_data=True, platform="darwin", home=tmp_path).parent == tmp_path / "Library/Application Support/VTS"
    assert resolve_database_path(env="explicit.db", use_user_data=True) == Path("explicit.db")


def test_desktop_paths_keep_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_TO_SUMMARY_DB", str(tmp_path / "custom/app.db"))
    monkeypatch.setenv("VIDEO_TO_SUMMARY_OUTPUT_DIR", str(tmp_path / "products"))
    paths = DesktopPaths.resolve()
    paths.prepare()
    assert paths.database == tmp_path / "custom/app.db"
    assert paths.output == tmp_path / "products"
    assert paths.webview.is_dir()


def test_session_redacted():
    formatter = RedactedFormatter("sample-session-value")
    record = logging.LogRecord("test", logging.ERROR, __file__, 1, "Bearer sample-session-value", (), None)
    assert "sample-session-value" not in formatter.format(record)


def test_spawned_backend_ready_and_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_TO_SUMMARY_DB", str(tmp_path / "app.db"))
    monkeypatch.setenv("VIDEO_TO_SUMMARY_OUTPUT_DIR", str(tmp_path / "output"))
    backend = Backend(DesktopPaths.resolve())
    try:
        backend.start()
        assert backend.origin.startswith("http://127.0.0.1:")
        assert backend.request("/api/v1/health")["status"] == "ok"
        from urllib.request import urlopen
        from urllib.error import HTTPError
        import pytest
        with pytest.raises(HTTPError) as rejected:
            urlopen(backend.origin + "/api/v1/health")
        assert rejected.value.code == 401
        with urlopen(backend.origin) as response:
            assert b'name="vts-desktop"' in response.read()
    finally:
        backend.stop()
    assert not backend.process.is_alive()
