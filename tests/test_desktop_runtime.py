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
            assert "frame-ancestors 'none'" in response.headers['Content-Security-Policy']
        with urlopen(backend.origin + '/guide') as response:
            assert "frame-ancestors 'self'" in response.headers['Content-Security-Policy']
            assert b'<section' in response.read()
    finally:
        backend.stop()
    assert not backend.process.is_alive()


def test_shutdown_preserves_work_and_cancel_intent(tmp_path, monkeypatch):
    import time
    import pytest
    from video_to_summary import db
    from video_to_summary.web import tasks
    from video_to_summary.constants import JobStatus
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "jobs.db")
    db.reset()
    tasks.configure_runtime()
    try:
        job = tasks.create_job({"url": "https://example.test/v"})
        job.mark_running()
        tasks.begin_shutdown()
        with pytest.raises(tasks.JobSuspendedError):
            tasks._make_cancel_check(job.job_id, time.monotonic(), 0)()
        tasks._suspend_job(job)
        assert tasks.get_job(job.job_id, refresh=True).status == JobStatus.PENDING
        job.mark_running()
        assert tasks.cancel_job(job.job_id)
        tasks._suspend_job(job)
        assert tasks.get_job(job.job_id, refresh=True).status == JobStatus.CANCELLED
        tasks._suspend_job(job)
        assert tasks.get_job(job.job_id, refresh=True).status == JobStatus.CANCELLED
        with pytest.raises(RuntimeError, match="退出"):
            tasks.create_job({"url": "https://example.test/v"})
    finally:
        tasks.configure_runtime()
        tasks._jobs.clear()
        tasks._cancel_flags.clear()
        db.reset()


def test_desktop_does_not_inherit_model_secrets(monkeypatch):
    from video_to_summary.desktop.backend import _clear_inherited_model_config
    for key in ("SUMMARY_API_KEY", "ASR_API_KEY", "OPENAI_API_KEY", "LLM_BASE_URL", "POLISH_TRANSCRIPT"):
        monkeypatch.setenv(key, "fixture-value")
    monkeypatch.setenv("VIDEO_TO_SUMMARY_DB", "explicit.db")
    monkeypatch.setenv("VTS_USER_AGENT", "explicit-agent")
    _clear_inherited_model_config()
    import os
    assert not any(key.startswith(("SUMMARY_", "ASR_", "OPENAI_", "LLM_", "POLISH_")) for key in os.environ)
    assert os.environ["VIDEO_TO_SUMMARY_DB"] == "explicit.db"
    assert os.environ["VTS_USER_AGENT"] == "explicit-agent"
