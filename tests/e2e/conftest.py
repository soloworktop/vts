"""E2E 测试层共享 fixture：真实 uvicorn 服务器 + 真实任务调度。

与 `tests/test_web.py::isolate_db`（单元层）的关键差异：
- **不 patch `enqueue_job`**——E2E 的目的就是验证真实调度链路
  （API handler → asyncio.create_task → asyncio.to_thread(run_job) → pipeline.run）。
  uvicorn 在 pytest 进程内的 daemon 线程里跑自己的事件循环，天然满足
  enqueue_job 对运行中 loop 的要求。
- 两个子层：
  - **离线层**（`e2e_server`）：三个网络边界（下载/转写/LLM）通过 monkeypatch
    `web_tasks._build_*` 接缝注入 `tests/e2e/fakes.py` 的进程内假体：零网络、零模型、
    确定性产物。默认 `python -m pytest` 即跑，必须全绿。
  - **live 层**（`live_server` 等）：不 patch 任何 `_build_*`，真实下载/转写/LLM，
    配置读 `.env`。默认整层 skip（`VTS_LIVE_E2E=1` 才运行），CI 不跑。
- DB / legacy JSON / 输出目录全部锚定 `tmp_path`，用例间完全隔离。

运行：`python -m pytest -m e2e`；浏览器用例需先 `playwright install chromium`；
live 层见 `VTS_LIVE_E2E=1 bash scripts/e2e.sh live`。
"""

import os
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests


# ---------------------------------------------------------------- server bootstrap

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_health(base_url: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    last_exc: Exception | None = None
    while time.time() < deadline:
        try:
            res = requests.get(f"{base_url}/api/v1/health", timeout=2)
            if res.status_code == 200:
                return
        except requests.RequestException as exc:  # noqa: PERF203 - 轮询重试属预期
            last_exc = exc
        time.sleep(0.05)
    raise RuntimeError(f"e2e server health check failed: {last_exc!r}")


def _start_isolated_server(tmp_path, monkeypatch, *, thread_name: str):
    """DB/legacy JSON/产物目录锚定 `tmp_path`，daemon 线程起真实 uvicorn 并等健康检查。

    离线层（`e2e_server`，fakes 注入）与 live 层（`live_server`，真实边界）共用的
    隔离底座；是否 patch `_build_*` 工厂由调用方决定。**绝不 patch `enqueue_job`**——
    真实调度是本层被测行为的一部分。
    """
    import uvicorn

    from video_to_summary import db as store_db
    from video_to_summary.web import app as web_app
    from video_to_summary.web import tasks as web_tasks

    monkeypatch.setattr(store_db, "DATABASE_PATH", tmp_path / "app.db")
    monkeypatch.setattr(store_db, "LEGACY_LLM_FILE", tmp_path / "missing_llm_profiles.json")
    monkeypatch.setattr(store_db, "LEGACY_TEMPLATE_FILE", tmp_path / "missing_summary_templates.json")
    store_db.reset()
    web_tasks._jobs.clear()
    web_tasks._cancel_flags.clear()
    out_dir = tmp_path / "output"
    # app.py 通过 `from .tasks import output_base` 持有独立绑定，
    # /api/v1/jobs/{id}/file 端点用的是 app 模块名 → 两个模块都要 patch
    monkeypatch.setattr(web_tasks, "output_base", lambda: str(out_dir))
    monkeypatch.setattr(web_app, "output_base", lambda: str(out_dir))

    port = _free_port()
    config = uvicorn.Config(web_app.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name=thread_name)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    _wait_health(base_url)
    return base_url, out_dir, server, thread


# ---------------------------------------------------------------- offline layer（fakes）

@pytest.fixture()
def e2e_server(tmp_path, monkeypatch):
    """函数级隔离：tmp DB + tmp 产物目录 + fakes + 真实 uvicorn（daemon 线程）。"""
    from fakes import FakeSummarizer, FakeTranscriber  # noqa: PLC0415 - pytest 将本目录插入 sys.path

    from video_to_summary.web import tasks as web_tasks

    base_url, out_dir, server, thread = _start_isolated_server(
        tmp_path, monkeypatch, thread_name="e2e-uvicorn"
    )

    # 进程内 fakes 注入（job 工作线程与请求线程共享模块全局，patch 对其可见）。
    # 服务器已起但尚无任何请求，job 只会由各测试触发，patch 一定先于第一次 _build_*。
    transcriber = FakeTranscriber()
    summarizer = FakeSummarizer()
    monkeypatch.setattr(web_tasks, "_build_transcriber", lambda settings, cfg: transcriber)
    monkeypatch.setattr(web_tasks, "_build_summarizer", lambda settings, cfg: summarizer)
    monkeypatch.setattr(web_tasks, "_build_polisher", lambda settings, cfg: None)

    try:
        yield SimpleNamespace(
            base_url=base_url,
            output_dir=out_dir,
            transcriber=transcriber,
            summarizer=summarizer,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ---------------------------------------------------------------- helpers as fixtures

@pytest.fixture()
def make_media_file(tmp_path):
    """生成假媒体文件（转写被 fake，内容无关紧要）；stem 即任务 source_id。"""

    def _make(name: str = "sample.mp3") -> Path:
        media = tmp_path / "media" / name
        media.parent.mkdir(parents=True, exist_ok=True)
        # 2 字节 mp3 帧头 + 填充，只求文件存在（LocalAudioSource 仅检查存在性）
        media.write_bytes(b"\xff\xfb" + b"0" * 256)
        return media

    return _make


@pytest.fixture()
def wait_job(e2e_server):
    """轮询直到任务进入期望状态集合；超时给出最后快照便于诊断。"""
    base_url = e2e_server.base_url

    def _wait(job_id: str, statuses: set[str], timeout: float = 15.0) -> dict:
        deadline = time.time() + timeout
        last: dict | None = None
        while time.time() < deadline:
            res = requests.get(f"{base_url}/api/v1/jobs/{job_id}", timeout=5)
            res.raise_for_status()
            last = res.json()
            if last["status"] in statuses:
                return last
            time.sleep(0.1)
        raise AssertionError(
            f"job {job_id} 未在 {timeout}s 内进入 {statuses}（最后状态快照: {last}）"
        )

    return _wait


# ---------------------------------------------------------------- browser fixtures

@pytest.fixture(scope="session")
def _pw_browser():
    """会话级 chromium；未安装时整组 UI 用例 skip（不污染默认套件结果）。"""
    sync_api = pytest.importorskip("playwright.sync_api")
    try:
        pw = sync_api.sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
    except Exception as exc:  # noqa: BLE001 - 环境差异统一降级为 skip
        pytest.skip(f"playwright chromium 不可用（先运行 `playwright install chromium`）: {exc}")
    yield browser
    browser.close()
    pw.stop()


@pytest.fixture()
def ui_page(_pw_browser, e2e_server):
    """每用例独立 context（localStorage/cookie 天然隔离）+ 页面。"""
    context = _pw_browser.new_context()
    page = context.new_page()
    page.set_default_timeout(10_000)
    yield page
    context.close()


@pytest.fixture()
def goto_console(ui_page, e2e_server):
    """打开控制台首页并等待应用初始化完成。"""

    def _goto() -> None:
        ui_page.goto(f"{e2e_server.base_url}/")
        ui_page.wait_for_selector("#submitBtn")
        # 等 app.js 顶层执行完毕（全部事件绑定挂好）：#submitBtn 在 HTML 解析期
        # 就存在，负载下「先点击后绑定」的竞态曾让提交点击被静默吞掉
        ui_page.wait_for_function("window.__APP_READY__ === true")

    return _goto


@pytest.fixture()
def submit_local_job(ui_page, goto_console, make_media_file, e2e_server):
    """走真实表单链路提交一个本地文件任务（含预检软拦截的「仍要创建」分支）。

    每次调用都先打开控制台首页（提交后前端停留在任务视图，再次调用可复位于新建视图）。
    创建证明用 API 轮询：假体任务瞬时完成即被移出，「状态卡片出现」可能从不发生。
    """

    import time

    import requests

    def _submit(title: str, *, media_path: Path | None = None, wait_terminal: bool = True) -> Path:
        goto_console()
        media = media_path or make_media_file()
        ui_page.click('.tab[data-panel="tabLocal"]')
        ui_page.fill("#audioPath", str(media))
        ui_page.fill("#title", title)
        ui_page.click("#submitBtn")
        # 两种合法结果：软拦截 warn box（点「仍要创建任务」），或直接创建。
        # 假体任务瞬时完成即被移出——「状态卡片出现」可能从不发生，禁止等卡片；
        # 提交后等待该任务（按 title 匹配）到达终态再返回，使后续 DOM 断言确定。
        deadline = time.time() + 30
        warned = False
        while time.time() < deadline:
            if not warned:
                proceed = ui_page.locator(".warn-proceed")
                if proceed.count():
                    proceed.click()
                    warned = True
                    time.sleep(0.2)
                    continue
            jobs = requests.get(f"{e2e_server.base_url}/api/v1/jobs", timeout=5).json()["jobs"]
            mine = [j for j in jobs if j.get("title") == title]
            if mine and mine[0].get("status") in ("completed", "failed", "cancelled"):
                return media
            if not wait_terminal and mine:
                return media  # 需要在途任务（取消/进度用例）：创建即返回
            time.sleep(0.3)
        raise TimeoutError(f"任务「{title}」未在时限内到达终态")

    return _submit


@pytest.fixture()
def wait_ui_status(e2e_server):
    """等待任务到达期望状态（React 视图懒挂载，改走 API 轮询，杜绝 DOM 时序依赖）。"""

    import time

    base_url = e2e_server.base_url

    def _wait(status: str, timeout: float = 20_000) -> None:
        deadline = time.time() + timeout / 1000
        last: list = []
        while time.time() < deadline:
            res = requests.get(f"{base_url}/api/v1/jobs", timeout=5)
            res.raise_for_status()
            last = res.json().get("jobs", [])
            if any(j["status"] == status for j in last):
                return
            time.sleep(0.2)
        raise AssertionError(
            f"任务未在 {timeout}ms 内到达 {status}（最后快照状态: {[j['status'] for j in last]}）"
        )

    return _wait


# ---------------------------------------------------------------- live layer（真实边界）
#
# 与离线层的唯一差异：不 patch 任何 `_build_*`，下载/转写/LLM 全部走真实实现，
# 配置来自 `.env`（SUMMARY_* / ASR_* / VTS_*，经 config.Settings 生产级 env 兜底链）。
# 默认整层 skip（VTS_LIVE_E2E=1 才启用）——CI 无 secrets，离线全绿基线不受影响。
# 运行：`VTS_LIVE_E2E=1 python -m pytest -m live -v` 或 `bash scripts/e2e.sh live`。

LIVE_GATE_ENV = "VTS_LIVE_E2E"
LIVE_URL_ENV = "VTS_LIVE_TEST_URL"
LIVE_TIMEOUT_ENV = "VTS_LIVE_TIMEOUT"
LIVE_DEFAULT_TIMEOUT = 600.0


@pytest.fixture(scope="session")
def live_env():
    """live 层总开关：`VTS_LIVE_E2E=1` 才继续，并按 CLI 同款语义（usecwd 向上）加载 .env。

    惰性加载——只有真实边界用例请求本 fixture 时才读 .env，离线套件零感知。
    """
    if not os.environ.get(LIVE_GATE_ENV):
        pytest.skip(f"真实边界 E2E 默认跳过；设置 {LIVE_GATE_ENV}=1 运行（配置读 .env）")
    from dotenv import find_dotenv, load_dotenv  # noqa: PLC0415

    load_dotenv(find_dotenv(usecwd=True))


@pytest.fixture(scope="session")
def live_settings(live_env):
    """经生产 `Settings` env 兜底链解析出的两槽位配置（Key / base_url / model）。"""
    from video_to_summary.config import Settings  # noqa: PLC0415

    return Settings.from_mapping(
        {"url": (os.environ.get(LIVE_URL_ENV) or "").strip() or "https://live.invalid/unused"}
    )


@pytest.fixture(scope="session")
def live_url(live_env):
    """真实测试视频 URL；未配置时 skip 并给出可行动提示（不硬编码易失效的链接）。"""
    url = (os.environ.get(LIVE_URL_ENV) or "").strip()
    if not url:
        pytest.skip(f"未配置 {LIVE_URL_ENV}（在 .env 填一个 yt-dlp 支持的短视频 URL 后重跑）")
    return url


@pytest.fixture(scope="session")
def live_timeout(live_env):
    """live 任务等待上限（秒）：真实下载/转写以分钟计，默认 600。"""
    try:
        return float(os.environ.get(LIVE_TIMEOUT_ENV) or LIVE_DEFAULT_TIMEOUT)
    except ValueError:
        return LIVE_DEFAULT_TIMEOUT


@pytest.fixture()
def live_server(live_env, tmp_path, monkeypatch):
    """真实边界 uvicorn：不 patch `_build_*`，组件由 .env 配置构造；隔离同 e2e_server。

    为确定性禁用与被测场景无关的环境干扰：POLISH_TRANSCRIPT（额外 LLM 阶段）、
    VIDEO_TO_SUMMARY_TOKEN（会把测试请求 401 掉）、JOB_TIMEOUT / MAX_CONCURRENT
    （本机环境变量不应改变测试等待语义）。
    """
    for var in (
        "POLISH_TRANSCRIPT",
        "VIDEO_TO_SUMMARY_TOKEN",
        "VIDEO_TO_SUMMARY_JOB_TIMEOUT",
        "VIDEO_TO_SUMMARY_MAX_CONCURRENT",
    ):
        monkeypatch.delenv(var, raising=False)

    base_url, out_dir, server, thread = _start_isolated_server(
        tmp_path, monkeypatch, thread_name="live-uvicorn"
    )
    try:
        yield SimpleNamespace(base_url=base_url, output_dir=out_dir)
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def live_media(request, live_url):
    """真实下载一次音频（走 Settings env 链里的 UA/proxy/cookies），供本地文件类用例复用。

    module 级 fixture 不能依赖 function 级 tmp_path → tempfile 自管生命周期。
    真实下载本身就覆盖了 yt-dlp + ffmpeg 音频提取边界。
    """
    import shutil
    import tempfile

    from video_to_summary.config import Settings  # noqa: PLC0415
    from video_to_summary.sources.url import URLAudioSource  # noqa: PLC0415

    settings = Settings.from_mapping({"url": live_url})
    workdir = Path(tempfile.mkdtemp(prefix="vts-live-media-"))
    request.addfinalizer(lambda: shutil.rmtree(workdir, ignore_errors=True))

    source = URLAudioSource(
        live_url,
        output_dir=workdir,
        cookies=settings.cookies,
        user_agent=settings.user_agent,
        proxy=settings.proxy,
        audio_format="mp3",
    )
    audio_path, meta = source.resolve()
    assert audio_path.exists() and audio_path.stat().st_size > 0, "真实下载失败：音频文件缺失或为空"
    return SimpleNamespace(audio_path=audio_path, meta=meta, workdir=workdir)


@pytest.fixture()
def wait_live_job(live_server, live_timeout):
    """轮询任务详情至期望状态集合；真实任务耗时以分钟计，轮询间隔与超时都放宽。"""
    base_url = live_server.base_url

    def _wait(job_id: str, statuses: set[str], timeout: float | None = None) -> dict:
        limit = timeout or live_timeout
        deadline = time.time() + limit
        last: dict | None = None
        while time.time() < deadline:
            res = requests.get(f"{base_url}/api/v1/jobs/{job_id}", timeout=10)
            res.raise_for_status()
            last = res.json()
            if last["status"] in statuses:
                return last
            time.sleep(0.5)
        raise AssertionError(f"job {job_id} 未在 {limit}s 内进入 {statuses}（最后状态快照: {last}）")

    return _wait
