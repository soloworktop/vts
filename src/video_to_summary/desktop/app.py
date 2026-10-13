"""Native window and bounded backend lifetime. CLI imports remain independent."""
import argparse
import multiprocessing
import sys
import threading
import time
import json
from pathlib import Path


class _CloseCoordinator:
    """Return from the native close callback before calling WebView APIs."""

    def __init__(self, window, approved_exit, exit_window):
        self.window = window
        self.approved_exit = approved_exit
        self.exit_window = exit_window
        self._checking = threading.Event()

    def closing(self):
        if self.approved_exit.is_set():
            return True
        if not self._checking.is_set():
            self._checking.set()
            threading.Thread(target=self._check, daemon=True).start()
        return False

    def _check(self):
        try:
            if self.window.events.loaded.is_set():
                try:
                    if self.window.evaluate_js("Boolean(window.__APP_READY__)"):
                        self.window.evaluate_js('window.dispatchEvent(new CustomEvent("vts:desktop-close"))')
                        return
                except Exception:
                    pass
            if self.window.create_confirmation_dialog("退出 VTS", "退出后未完成任务将在下次启动恢复。确认退出？"):
                self.exit_window()
        finally:
            self._checking.clear()


def main():
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(prog="vts-desktop")
    parser.add_argument("--desktop-smoke", type=Path)
    parser.add_argument("--desktop-gui-smoke", type=Path)
    args = parser.parse_args()
    from .paths import DesktopPaths
    from .backend import Backend
    paths = DesktopPaths.resolve()
    backend = Backend(paths)
    try:
        if not args.desktop_smoke:
            _require_webview2()
        backend.start(cleanup_probe=bool(args.desktop_smoke or args.desktop_gui_smoke))
        if args.desktop_smoke:
            args.desktop_smoke.write_text(json.dumps({"ok": True, "version": backend.version, "checks": backend.checks, "backend_pid": backend.process.pid, "media_probe_pid": backend.media_probe_pid}), encoding="utf-8")
            return
        import webview
        from .bridge import Bridge
        approved_exit = threading.Event()
        def exit_window():
            approved_exit.set()
            window.destroy()
        bridge = Bridge(backend, exit_window)
        window = webview.create_window("VTS", backend.origin, js_api=bridge, width=1200, height=800, min_size=(850, 600))
        bridge._window = window
        close_coordinator = _CloseCoordinator(window, approved_exit, exit_window)
        window.events.closing += close_coordinator.closing
        def monitor():
            backend.process.join()
            if not approved_exit.is_set() and backend.process.exitcode and window.create_confirmation_dialog("本地服务已退出", "请重新启动 VTS；诊断日志位于 " + str(paths.logs)):
                approved_exit.set()
                window.destroy()
        threading.Thread(target=monitor, daemon=True).start()
        if args.desktop_gui_smoke:
            def gui_probe():
                verified = False
                try:
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        if window.evaluate_js("Boolean(window.__APP_READY__)"):
                            break
                        time.sleep(0.1)
                    else:
                        raise TimeoutError("React initialization failed in native WebView")
                    window.evaluate_js("document.getElementById('guideBtn').click()")
                    while time.monotonic() < deadline:
                        if window.evaluate_js("Boolean(document.getElementById('guideFrame')?.contentDocument?.querySelector('section'))"):
                            break
                        time.sleep(0.1)
                    else:
                        raise TimeoutError("User guide failed to load inside native window")
                    window.evaluate_js("window.pywebview.api.copy_text('VTS desktop smoke', " + json.dumps(backend.token) + ").then(ok => { window.__DESKTOP_SMOKE_OK__ = Boolean(ok); })")
                    while time.monotonic() < deadline:
                        if window.evaluate_js("Boolean(window.__DESKTOP_SMOKE_OK__)"):
                            args.desktop_gui_smoke.write_text(json.dumps({"ok": True, "gui": True, "bridge": True, "version": backend.version, "checks": backend.checks, "backend_pid": backend.process.pid, "media_probe_pid": backend.media_probe_pid}), encoding="utf-8")
                            verified = True
                            break
                        time.sleep(0.1)
                    else:
                        raise TimeoutError("Native bridge round trip failed")
                except Exception as exc:
                    args.desktop_gui_smoke.write_text(json.dumps({"ok": False, "error": str(exc)}), encoding="utf-8")
                finally:
                    if not verified:
                        approved_exit.set()
                        window.destroy()
                    else:
                        close_coordinator.closing()
            window.events.loaded += lambda: threading.Thread(target=gui_probe, daemon=True).start()
        webview.start(gui="edgechromium" if sys.platform == "win32" else "cocoa", storage_path=str(paths.webview), private_mode=False)
    except Exception as exc:
        if args.desktop_smoke or args.desktop_gui_smoke:
            raise
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(exc) + "\n诊断日志：" + str(paths.logs), "VTS 无法启动", 0x10)
            return
        import html
        import webview
        webview.create_window("VTS 启动失败", html="<h2>VTS 无法启动</h2><p>" + html.escape(str(exc)) + "</p><p>诊断日志：" + html.escape(str(paths.logs)) + "</p>")
        webview.start(gui="edgechromium" if sys.platform == "win32" else "cocoa")
    finally:
        backend.stop()


def _require_webview2():
    if sys.platform != "win32":
        return
    import winreg
    runtime = r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            try:
                with winreg.OpenKey(hive, runtime, 0, winreg.KEY_READ | view) as key:
                    value, _ = winreg.QueryValueEx(key, "pv")
                    if int(str(value).split(".")[0]) >= 86:
                        return
            except (OSError, ValueError):
                pass
    raise RuntimeError("缺少 Microsoft WebView2 Runtime。请安装 Evergreen Runtime 后重启 VTS： https://developer.microsoft.com/microsoft-edge/webview2/")
