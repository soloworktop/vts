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
    backend = SimpleNamespace(origin="http://127.0.0.1:1234", token="example-session", bootstrap_nonce="example-bootstrap", version="test",
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


def test_exit_waits_for_bridge_response(bridge, monkeypatch):
    scheduled = []
    closed = []

    class Timer:
        def __init__(self, delay, callback):
            scheduled.append((delay, callback))

        def start(self):
            pass

    monkeypatch.setattr("video_to_summary.desktop.bridge.threading.Timer", Timer)
    bridge._on_exit = lambda: closed.append(True)
    assert bridge.request_exit("example-session") is True
    assert bridge.request_exit("example-session") is True
    assert len(scheduled) == 1
    assert closed == []
    scheduled[0][1]()
    assert closed == [True]


@pytest.mark.parametrize("url", ["https://example.test/api/v1/jobs/export", "//example.test/api/v1/logs/export",
    "/api/v1/health", "/api/v1/logs/export?token=x", "/api/v1/jobs/export#fragment",
    "/api/v1/jobs/" + "a" * 32 + "/export?path=x&format=md&format=md"])
def test_export_url_boundary(bridge, url):
    with pytest.raises(ValueError):
        bridge._export_url(url)


def test_export_cancel_and_redirect(bridge):
    assert bridge.export_product("/api/v1/logs/export", "logs.txt", "example-session") == {"saved": False, "cancelled": True}
    with pytest.raises(ValueError, match="重定向"):
        NoRedirects().redirect_request(None, None, 302, "found", {}, "https://example.test/")


def test_only_actual_products_open(bridge, monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(bridge, "_open", opened.append)
    assert bridge.open_job_output("a" * 32, "summary", "example-session")
    assert opened[0].name == "中文 摘要.md"
    outside = tmp_path / "program.exe"
    outside.write_bytes(b"program")
    bridge._backend.request = lambda _: {"result_paths": {"summary": str(outside)}}
    with pytest.raises(PermissionError):
        bridge.open_job_output("a" * 32, "summary", "example-session")
    with pytest.raises(PermissionError):
        bridge.reveal_in_finder(str(outside), "example-session")


def test_no_public_backend_capabilities(bridge):
    assert not any(name in vars(bridge) for name in ("backend", "window", "on_exit", "saved"))


def test_remote_frame_cannot_use_main_page_bridge(bridge):
    with pytest.raises(PermissionError, match="caller"):
        bridge.get_session()
    with pytest.raises(PermissionError, match="caller"):
        bridge.copy_text("attack")
    assert bridge.get_session("example-bootstrap")["token"] == "example-session"


@pytest.mark.parametrize("write_succeeds", [True, False])
def test_windows_clipboard_owner_and_allocation_lifetime(bridge, monkeypatch, write_succeeds):
    import ctypes
    from video_to_summary.desktop import bridge as module
    class Function:
        def __init__(self, callback):
            self.callback = callback
        def __call__(self, *args):
            return self.callback(*args)
    owner = 0x123456789
    opened, freed, closed = [], [], []
    buffer = ctypes.create_string_buffer(128)
    bridge._window.native = SimpleNamespace(Handle=SimpleNamespace(ToInt64=lambda: owner))
    kernel = SimpleNamespace(
        GlobalAlloc=Function(lambda *a: 1),
        GlobalLock=Function(lambda *a: ctypes.addressof(buffer)),
        GlobalUnlock=Function(lambda *a: 1),
        GlobalFree=Function(lambda handle: freed.append(handle)))
    user = SimpleNamespace(
        OpenClipboard=Function(lambda handle: opened.append(handle) or int(handle == owner)),
        EmptyClipboard=Function(lambda: 1),
        SetClipboardData=Function(lambda *a: int(write_succeeds)),
        CloseClipboard=Function(lambda: closed.append(True)))
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kw: kernel if name == "kernel32" else user, raising=False)
    monkeypatch.setattr(ctypes, "WinError", lambda *a: OSError("clipboard write failed"), raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    monkeypatch.setattr(module.sys, "platform", "win32")
    if write_succeeds:
        assert bridge.copy_text("中文 text", "example-session")
    else:
        with pytest.raises(OSError, match="clipboard write"):
            bridge.copy_text("中文 text", "example-session")
    assert opened == [owner] and closed == [True]
    assert freed == ([] if write_succeeds else [1])
    assert buffer.raw.startswith(("中文 text" + "\0").encode("utf-16-le"))
