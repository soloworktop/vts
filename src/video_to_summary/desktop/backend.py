"""Spawn one backend, bind the actual ephemeral socket, report readiness via IPC."""
import asyncio
import json
import multiprocessing
import os
import secrets
import socket
import time
from urllib.request import Request, build_opener, ProxyHandler

from ..config import DESKTOP_START_TIMEOUT, DESKTOP_STOP_TIMEOUT
from ..constants import DesktopMessage


def _clear_inherited_model_config():
    """Desktop BYOK configuration is explicit; never inherit a developer shell Key."""
    for name in tuple(os.environ):
        if name.startswith(("SUMMARY_", "ASR_", "LLM_", "OPENAI_", "POLISH_")):
            os.environ.pop(name, None)


def backend_main(connection):
    # Native lifetime containment is installed before any business code or subprocess.
    from .process_control import isolate_backend
    isolate_backend()
    config = connection.recv()
    paths = config["paths"]
    _clear_inherited_model_config()
    os.chdir(paths.database.parent)
    os.environ["VIDEO_TO_SUMMARY_DB"] = str(paths.database)
    os.environ["VIDEO_TO_SUMMARY_OUTPUT_DIR"] = str(paths.output)
    os.environ["VIDEO_TO_SUMMARY_TOKEN"] = config["token"]
    from .. import db
    db.DATABASE_PATH = paths.database
    from ..utils import install_windows_subprocess_guard
    install_windows_subprocess_guard()
    from .logging import configure
    configure(paths.logs, config["token"])
    try:
        asyncio.run(_serve(connection, paths, config["bootstrap_nonce"], config.get("cleanup_probe", False)))
    except BaseException:
        import logging
        logging.getLogger("video_to_summary.desktop").exception("backend startup/runtime failed")
        try:
            connection.send({"event": DesktopMessage.ERROR, "message": "本地服务无法启动，请查看桌面诊断日志"})
        except (BrokenPipeError, OSError):
            pass
        raise
    finally:
        connection.close()
        if os.name != "nt":
            import signal
            # Also reap descendants after GUI crashes or the group leader exits.
            os.killpg(os.getpgrp(), signal.SIGKILL)


async def _serve(connection, paths, bootstrap_nonce, cleanup_probe=False):
    import uvicorn
    from starlette.responses import HTMLResponse
    from ..web.app import app, _static_dir
    from ..version import get_version
    from ..web.tasks import configure_runtime, begin_shutdown, wait_for_shutdown
    from .media import local_source_factory
    configure_runtime(local_source_factory=local_source_factory)
    from .paths import binary_path
    import sys
    if getattr(sys, "frozen", False):
        from pathlib import Path
        os.environ["PATH"] = str(Path(binary_path("ffmpeg")).parent) + os.pathsep + os.environ.get("PATH", "")
        binary_path("ffprobe")
    static = _static_dir()
    if not (static / "index.html").is_file() or not list((static / "assets").glob("*.js")):
        raise RuntimeError("桌面前端资源缺失；源码开发请先运行 pnpm build")

    @app.middleware("http")
    async def desktop_page(request, call_next):
        if request.url.path == "/" and request.method == "GET":
            page = (static / "index.html").read_text(encoding="utf-8")
            return HTMLResponse(page.replace("<head>", f'<head><meta name="vts-desktop" content="{bootstrap_nonce}">', 1), headers={"Cache-Control": "no-store", "Content-Security-Policy": "frame-ancestors 'none'; frame-src 'self'; object-src 'none'; base-uri 'self'"})
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        ancestors = "'self'" if request.url.path == "/guide" else "'none'"
        response.headers["Content-Security-Policy"] = f"frame-ancestors {ancestors}; frame-src 'self'; object-src 'none'; base-uri 'self'"
        return response

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, timeout_graceful_shutdown=DESKTOP_STOP_TIMEOUT))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            if serving.done():
                await serving
                raise RuntimeError("backend exited before readiness")
            await asyncio.sleep(0.05)
        from ..crypto import encrypt_secret, decrypt_secret
        encrypted = encrypt_secret("desktop-runtime-probe")
        if decrypt_secret(encrypted) != "desktop-runtime-probe" or not encrypted.startswith("enc:v1:"):
            raise RuntimeError("Desktop encryption probe failed")
        from .. import db
        with db.get_conn() as database:
            database.execute("SELECT count(*) FROM jobs_fts").fetchone()
        probe_pid = None
        if cleanup_probe:
            import subprocess
            from ..utils import subprocess_no_window_kwargs
            # Native artifact smoke leaves a real media child alive until GUI shutdown.
            child = subprocess.Popen([binary_path("ffmpeg"), "-nostdin", "-v", "error", "-re", "-f", "lavfi",
                "-i", "sine=duration=600", "-f", "null", "-"], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, **subprocess_no_window_kwargs())
            await asyncio.sleep(0.1)
            if child.poll() is not None:
                raise RuntimeError("Media child cleanup probe exited before shutdown")
            probe_pid = child.pid
        connection.send({"event": DesktopMessage.READY, "port": sock.getsockname()[1], "version": get_version(),
                         "checks": ["cryptography", "sqlite-fts5"], "media_probe_pid": probe_pid})
        while not serving.done():
            if connection.poll():
                try:
                    message = connection.recv()
                except EOFError:
                    message = DesktopMessage.STOP
                if message == DesktopMessage.STOP:
                    break
            await asyncio.sleep(0.05)
        begin_shutdown(preserve_pending=True)
        await wait_for_shutdown(DESKTOP_STOP_TIMEOUT - 2)
        server.should_exit = True
        await serving
    finally:
        sock.close()


class Backend:
    def __init__(self, paths):
        self.paths = paths
        self.token = secrets.token_urlsafe(32)
        self.bootstrap_nonce = secrets.token_urlsafe(32)
        self.origin = ""
        self.version = ""
        self.checks = []
        self.media_probe_pid = None
        context = multiprocessing.get_context("spawn")
        self.connection, child = context.Pipe()
        self.process = context.Process(target=backend_main, args=(child,), name="vts-backend")
        self.child = child
        self.tree = None

    def start(self, timeout=DESKTOP_START_TIMEOUT, *, cleanup_probe=False):
        from .process_control import ProcessTree
        self.paths.prepare()
        self.process.start()
        self.child.close()
        try:
            self.tree = ProcessTree(self.process)
            self.connection.send({"paths": self.paths, "token": self.token, "bootstrap_nonce": self.bootstrap_nonce, "cleanup_probe": cleanup_probe})
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.connection.poll(0.1):
                    message = self.connection.recv()
                    if message["event"] != DesktopMessage.READY:
                        raise RuntimeError(message.get("message", "backend failed"))
                    if os.name != "nt":
                        self.tree.group = self.process.pid
                    self.origin = f"http://127.0.0.1:{message['port']}"
                    self.version = message["version"]
                    self.checks = message["checks"]
                    self.media_probe_pid = message.get("media_probe_pid")
                    health = self.request("/api/v1/health")
                    if health.get("status") != "ok" or health.get("version") != self.version:
                        raise RuntimeError("本地服务健康/版本检查失败")
                    import re
                    page = self.read_bytes("/").decode("utf-8")
                    assets = re.findall(r'(?:src|href)="(/static/assets/[^"?]+)"', page)
                    if not assets or not any(asset.endswith(".js") for asset in assets):
                        raise RuntimeError("Desktop frontend asset manifest missing")
                    for asset in assets:
                        if not self.read_bytes(asset):
                            raise RuntimeError("Desktop frontend asset missing")
                    if not self.read_bytes("/guide"):
                        raise RuntimeError("Desktop user guide missing")
                    self.checks += ["health-version", "homepage", "static-assets", "user-guide"]
                    return self
                if not self.process.is_alive():
                    raise RuntimeError("本地服务异常退出，请查看诊断日志")
            raise TimeoutError("本地服务启动超时（30 秒），请查看诊断日志")
        except BaseException:
            self.stop(timeout=0)
            raise

    def request(self, path):
        request = Request(self.origin + path, headers={"Authorization": "Bearer " + self.token})
        with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
            return json.load(response)

    def read_bytes(self, path):
        request = Request(self.origin + path, headers={"Authorization": "Bearer " + self.token})
        with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
            return response.read()

    def stop(self, timeout=DESKTOP_STOP_TIMEOUT):
        if self.process.pid:
            if self.process.is_alive():
                try:
                    self.connection.send(DesktopMessage.STOP)
                except (BrokenPipeError, OSError):
                    pass
                self.process.join(timeout=timeout)
            if self.tree:
                self.tree.close()
            elif self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=2)
        self.connection.close()
