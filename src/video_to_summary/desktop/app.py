"""Native window and bounded backend lifetime. CLI imports remain independent."""
import argparse
import multiprocessing
import sys
import threading
from pathlib import Path


def main():
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(prog="vts-desktop")
    parser.add_argument("--desktop-smoke", type=Path)
    args = parser.parse_args()
    from .paths import DesktopPaths
    from .backend import Backend
    paths = DesktopPaths.resolve()
    backend = Backend(paths)
    try:
        backend.start()
        if args.desktop_smoke:
            import json
            args.desktop_smoke.write_text(json.dumps({"ok": True, "version": backend.version}), encoding="utf-8")
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
        def closing():
            if approved_exit.is_set():
                return True
            try:
                if window.evaluate_js("Boolean(window.__APP_READY__)"):
                    window.evaluate_js("window.dispatchEvent(new CustomEvent(\"vts:desktop-close\"))")
                    return False
            except Exception:
                pass
            if window.create_confirmation_dialog("退出 VTS", "退出后未完成任务将在下次启动恢复。确认退出？"):
                return True
            return False
        window.events.closing += closing
        def monitor():
            backend.process.join()
            if backend.process.exitcode and window.create_confirmation_dialog("本地服务已退出", "请重新启动 VTS；诊断日志位于 " + str(paths.logs)):
                window.destroy()
        threading.Thread(target=monitor, daemon=True).start()
        webview.start(gui="edgechromium" if sys.platform == "win32" else "cocoa", storage_path=str(paths.webview), private_mode=False)
    except Exception as exc:
        if args.desktop_smoke:
            raise
        import html
        import webview
        webview.create_window("VTS 启动失败", html="<h2>VTS 无法启动</h2><p>" + html.escape(str(exc)) + "</p><p>诊断日志：" + html.escape(str(paths.logs)) + "</p>")
        webview.start(gui="edgechromium" if sys.platform == "win32" else "cocoa")
    finally:
        backend.stop()
