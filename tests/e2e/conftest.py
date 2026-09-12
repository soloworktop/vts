"""E2E 测试层共享 fixture：真实 uvicorn 服务器 + 真实任务调度 + 进程内 fakes。

与 `tests/test_web.py::isolate_db`（单元层）的关键差异：
- **不 patch `enqueue_job`**——E2E 的目的就是验证真实调度链路
  （API handler → asyncio.create_task → asyncio.to_thread(run_job) → pipeline.run）。
  uvicorn 在 pytest 进程内的 daemon 线程里跑自己的事件循环，天然满足
  enqueue_job 对运行中 loop 的要求。
- 三个网络边界（下载/转写/LLM）通过 monkeypatch `web_tasks._build_*` 接缝注入
  `tests/e2e/fakes.py` 的进程内假体：零网络、零模型、确定性产物。
- DB / legacy JSON / 输出目录全部锚定 `tmp_path`，用例间完全隔离。

运行：`python -m pytest -m e2e`；浏览器用例需先 `playwright install chromium`。
"""

import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests


# ---------------------------------------------------------------- server fixture

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


@pytest.fixture()
def e2e_server(tmp_path, monkeypatch):
    """函数级隔离：tmp DB + tmp 产物目录 + fakes + 真实 uvicorn（daemon 线程）。"""
    import uvicorn

    from video_to_summary import db as store_db
    from video_to_summary.web import app as web_app
    from video_to_summary.web import tasks as web_tasks

    from fakes import FakeSummarizer, FakeTranscriber  # noqa: PLC0415 - pytest 将本目录插入 sys.path

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

    # 进程内 fakes 注入（job 工作线程与请求线程共享模块全局，patch 对其可见）。
    # 注意：绝不 patch enqueue_job——真实调度是本层被测行为的一部分。
    transcriber = FakeTranscriber()
    summarizer = FakeSummarizer()
    monkeypatch.setattr(web_tasks, "_build_transcriber", lambda settings, cfg: transcriber)
    monkeypatch.setattr(web_tasks, "_build_summarizer", lambda settings, cfg: summarizer)
    monkeypatch.setattr(web_tasks, "_build_polisher", lambda settings, cfg: None)

    port = _free_port()
    config = uvicorn.Config(web_app.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="e2e-uvicorn")
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_health(base_url)
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
