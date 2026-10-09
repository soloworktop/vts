"""Native bridge: trusted main page only, narrow export and filesystem capabilities."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, urljoin
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

from ..utils import subprocess_no_window_kwargs


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("导出接口不允许重定向")


class Bridge:
    def __init__(self, backend, on_exit):
        self._backend = backend
        self._on_exit = on_exit
        self._window = None
        self._saved = set()

    def _check(self):
        if self._window is None:
            raise PermissionError("desktop bridge is not ready")
        url = urlsplit(self._window.get_current_url() or "")
        if f"{url.scheme}://{url.netloc}" != self._backend.origin or url.path != "/":
            raise PermissionError("untrusted desktop page")

    def get_session(self):
        self._check()
        return {"origin": self._backend.origin, "token": self._backend.token, "version": self._backend.version}

    def pick_file(self, kind="media"):
        self._check()
        if kind not in ("media", "cookies"):
            raise ValueError("unsupported file kind")
        import webview
        selected = self._window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False)
        self._check()
        if not selected:
            return None
        path = Path(selected[0]).resolve(strict=True)
        if not path.is_file():
            raise ValueError("请选择一个可读取的文件")
        return str(path)

    def _export_url(self, value):
        if not isinstance(value, str) or len(value) > 8192:
            raise ValueError("invalid export URL")
        absolute = urljoin(self._backend.origin + "/", value)
        parsed = urlsplit(absolute)
        if f"{parsed.scheme}://{parsed.netloc}" != self._backend.origin or parsed.fragment or parsed.username:
            raise ValueError("只允许导出当前本地服务的产物")
        query = parse_qs(parsed.query, keep_blank_values=True)
        if any(len(v) != 1 for v in query.values()):
            raise ValueError("duplicate export parameters")
        if parsed.path in ("/api/v1/logs/export", "/api/v1/jobs/export"):
            if query:
                raise ValueError("unexpected export parameters")
        elif re.fullmatch(r"/api/v1/jobs/[0-9a-f]{32}/export", parsed.path):
            if set(query) != {"path", "format"} or query["format"][0] not in ("md", "txt", "srt", "json"):
                raise ValueError("invalid product export parameters")
        else:
            raise ValueError("unsupported export endpoint")
        return absolute

    def export_product(self, url, name):
        self._check()
        absolute = self._export_url(url)
        # Filename is only a suggestion; never allow it to change dialog directories.
        name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name))[:180].strip(". ") or "export.txt"
        import webview
        selected = self._window.create_file_dialog(webview.SAVE_DIALOG, save_filename=name)
        self._check()
        if not selected:
            return {"saved": False, "cancelled": True}
        destination = Path(selected if isinstance(selected, str) else selected[0]).expanduser().resolve()
        if destination.exists() and not self._window.create_confirmation_dialog("覆盖文件", f"覆盖 {destination.name}？"):
            return {"saved": False, "cancelled": True}
        fd, temporary = tempfile.mkstemp(prefix=".vts-export-", dir=destination.parent)
        try:
            request = Request(absolute, headers={"Authorization": "Bearer " + self._backend.token})
            # Ignore environment proxies for loopback; never send this session to a proxy.
            with os.fdopen(fd, "wb") as target:
                with build_opener(ProxyHandler({}), NoRedirects()).open(request, timeout=30) as response:
                    shutil.copyfileobj(response, target)
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
        self._saved.add(destination)
        return {"saved": True, "cancelled": False, "path": str(destination)}

    def _job_path(self, job_id, product_key=None):
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise ValueError("invalid job ID")
        job = self._backend.request("/api/v1/jobs/" + job_id)
        directory = (self._backend.paths.output / job_id).resolve()
        if not directory.is_relative_to(self._backend.paths.output.resolve()):
            raise PermissionError("output directory escaped")
        if product_key is None:
            if not directory.is_dir():
                raise FileNotFoundError("任务输出目录尚未生成")
            return directory
        path = (job.get("result_paths") or {}).get(product_key)
        if not path:
            raise FileNotFoundError("该产物尚未生成")
        # Backend CWD is the data directory, not the GUI launch directory.
        target = Path(path)
        if not target.is_absolute():
            target = self._backend.paths.database.parent / target
        target = target.resolve(strict=True)
        if not target.is_relative_to(directory) or not target.is_file() or target.suffix.lower() not in (".md", ".txt", ".srt", ".json"):
            raise PermissionError("不是可打开的任务产物")
        return target

    def open_job_output(self, job_id, product_key=None):
        self._check()
        self._open(self._job_path(job_id, product_key))
        return True

    def reveal_in_finder(self, path):
        self._check()
        target = Path(path).resolve(strict=True)
        if target not in self._saved:
            raise PermissionError("只允许显示本次保存的文件")
        self._open(target.parent)
        return True

    def _open(self, target):
        if sys.platform == "win32":
            os.startfile(str(target))
        elif sys.platform == "darwin":
            subprocess.run(["open", str(target)], check=True, **subprocess_no_window_kwargs())
        else:
            raise RuntimeError("不支持的桌面平台")

    def open_external(self, url):
        self._check()
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("只允许打开 HTTP(S) 外链")
        webbrowser.open(url)
        return True

    def copy_text(self, text):
        self._check()
        if not isinstance(text, str) or len(text) > 10_000_000:
            raise ValueError("clipboard text too large")
        if sys.platform == "darwin":
            from AppKit import NSPasteboard, NSPasteboardTypeString
            board = NSPasteboard.generalPasteboard()
            board.clearContents()
            return bool(board.setString_forType_(text, NSPasteboardTypeString))
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            user = ctypes.WinDLL("user32", use_last_error=True)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GlobalAlloc.restype = wintypes.HGLOBAL
            kernel.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
            kernel.GlobalLock.restype = ctypes.c_void_p
            kernel.GlobalLock.argtypes = [wintypes.HGLOBAL]
            kernel.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
            kernel.GlobalFree.argtypes = [wintypes.HGLOBAL]
            user.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
            user.SetClipboardData.restype = wintypes.HANDLE
            raw = (text + "\0").encode("utf-16-le")
            if not user.OpenClipboard(None):
                raise ctypes.WinError(ctypes.get_last_error())
            handle = None
            try:
                user.EmptyClipboard()
                handle = kernel.GlobalAlloc(0x0002, len(raw))
                pointer = kernel.GlobalLock(handle)
                if not pointer:
                    raise ctypes.WinError(ctypes.get_last_error())
                ctypes.memmove(pointer, raw, len(raw))
                kernel.GlobalUnlock(handle)
                if not user.SetClipboardData(13, handle):
                    raise ctypes.WinError(ctypes.get_last_error())
                handle = None  # Windows now owns the allocation.
                return True
            finally:
                if handle:
                    kernel.GlobalFree(handle)
                user.CloseClipboard()
        return False

    def request_exit(self):
        self._check()
        self._on_exit()
        return True
