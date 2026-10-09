from pathlib import Path
from types import SimpleNamespace
import sys
import pytest
from video_to_summary.desktop.bridge import Bridge, NoRedirects


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    source = tmp_path / "output" / ("a" * 32)
    source.mkdir(parents=True)
    product = source / "中文 摘要.md"
    product.write_text("# test", encoding="utf-8")
    backend = SimpleNamespace(origin="http://127.0.0.1:1234", token="example-session", version="test",
        paths=SimpleNamespace(output=tmp_path / "output", database=tmp_path / "app.db"),
        request=lambda _: {"result_paths": {"summary": str(product)}})
    instance = Bridge(backend, lambda: None)
    instance._window = SimpleNamespace(get_current_url=lambda: backend.origin + "/", create_file_dialog=lambda *a, **kw: None)
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(OPEN_DIALOG=1, SAVE_DIALOG=2))
    return instance


def test_untrusted_pages_rejected(bridge):
    bridge._window.get_current_url = lambda: "https://example.test/"
    for call in (bridge.get_session, lambda: bridge.pick_file(), lambda: bridge.copy_text("test"), bridge.request_exit):
        with pytest.raises(PermissionError):
            call()


@pytest.mark.parametrize("url", ["https://example.test/api/v1/jobs/export", "//example.test/api/v1/logs/export",
    "/api/v1/health", "/api/v1/logs/export?token=x", "/api/v1/jobs/export#fragment",
    "/api/v1/jobs/" + "a" * 32 + "/export?path=x&format=md&format=md"])
def test_export_url_boundary(bridge, url):
    with pytest.raises(ValueError):
        bridge._export_url(url)


def test_export_cancel_and_redirect(bridge):
    assert bridge.export_product("/api/v1/logs/export", "logs.txt") == {"saved": False, "cancelled": True}
    with pytest.raises(ValueError, match="重定向"):
        NoRedirects().redirect_request(None, None, 302, "found", {}, "https://example.test/")


def test_only_actual_products_open(bridge, monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(bridge, "_open", opened.append)
    assert bridge.open_job_output("a" * 32, "summary")
    assert opened[0].name == "中文 摘要.md"
    outside = tmp_path / "program.exe"
    outside.write_bytes(b"program")
    bridge._backend.request = lambda _: {"result_paths": {"summary": str(outside)}}
    with pytest.raises(PermissionError):
        bridge.open_job_output("a" * 32, "summary")
    with pytest.raises(PermissionError):
        bridge.reveal_in_finder(str(outside))


def test_no_public_backend_capabilities(bridge):
    assert not any(name in vars(bridge) for name in ("backend", "window", "on_exit", "saved"))
