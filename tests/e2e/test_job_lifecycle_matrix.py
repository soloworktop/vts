"""L1 E2E：Job 生命周期矩阵补强（P1-2）。

与 `test_api_job_lifecycle.py` 互补：这里覆盖生命周期矩阵中缺失的组合——
- completed → retry → completed
- cancelled → retry → completed
- running → cancel 后 retry 的时序约束（运行中不可 retry）
- 进程重启（running 状态崩溃）→ resume → completed
- max_concurrent=1 时任务严格串行
- 两个 Job 并行执行互不影响

真实边界：SQLite + 文件系统 + uvicorn + 真实任务调度（asyncio → executor → pipeline）；
fake 边界：下载 / 转写 / LLM（tests/e2e/fakes.py）。
"""

import json
import sqlite3
import threading
import time

import pytest
import requests

from fakes import FakeTranscriber, STYLE_RICH_MARKERS

pytestmark = pytest.mark.e2e


def _wait_db_status(db_path, job_id: str, statuses: set[str], timeout: float = 10.0) -> str:
    """直接轮询 DB 状态（服务器停机期间 API 不可用时的观测手段）。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        finally:
            conn.close()
        last = row[0] if row else None
        if last in statuses:
            return last
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} 未在 {timeout}s 内进入 {statuses}（DB 状态: {last}）")


# ---------------------------------------------------------------- retry 矩阵

def test_retry_completed_job(e2e_server, make_media_file, wait_job) -> None:
    """completed → retry → completed：产物目录被清空重建，转写不命中旧缓存。"""
    media = make_media_file("done-retry.mp3")
    res = requests.post(
        f"{e2e_server.base_url}/api/v1/jobs",
        json={"source_type": "local", "audio_path": str(media), "title": "完成后重试"},
        timeout=5,
    )
    job_id = res.json()["job_id"]
    wait_job(job_id, {"completed"})
    assert e2e_server.transcriber.calls == 1

    job_dir = e2e_server.output_dir / job_id
    stale = job_dir / "stale.txt"
    stale.write_text("should be wiped", encoding="utf-8")

    retry_res = requests.post(f"{e2e_server.base_url}/api/v1/jobs/{job_id}/retry", timeout=5)
    assert retry_res.status_code == 200
    assert retry_res.json()["status"] == "pending"

    detail = wait_job(job_id, {"completed"})
    assert detail["retry_count"] == 1
    assert detail["retried_at"]
    assert not stale.exists(), "retry 必须清空旧产物目录"
    assert (job_dir / "done-retry.summary.md").exists()
    assert e2e_server.transcriber.calls == 2, "目录已清空，转写必须真实重跑（不得命中旧缓存）"
    for marker in STYLE_RICH_MARKERS:
        assert marker in (job_dir / "done-retry.summary.md").read_text(encoding="utf-8")


def test_retry_cancelled_job(e2e_server, make_media_file, wait_job) -> None:
    """cancelled → retry → completed。"""
    e2e_server.transcriber.delay = 1.5
    media = make_media_file("cancel-retry.mp3")
    job_id = requests.post(
        f"{e2e_server.base_url}/api/v1/jobs",
        json={"source_type": "local", "audio_path": str(media), "title": "取消后重试"},
        timeout=5,
    ).json()["job_id"]
    wait_job(job_id, {"running"})
    assert requests.post(f"{e2e_server.base_url}/api/v1/jobs/{job_id}/cancel", timeout=5).status_code == 200
    wait_job(job_id, {"cancelled"})

    retry_res = requests.post(f"{e2e_server.base_url}/api/v1/jobs/{job_id}/retry", timeout=5)
    assert retry_res.status_code == 200
    detail = wait_job(job_id, {"completed"})
    assert detail["retry_count"] == 1
    assert detail["error"] in (None, ""), "retry 后旧错误必须清空"


def test_retry_rejected_while_cancel_pending(e2e_server, make_media_file, wait_job) -> None:
    """running → cancel + retry 竞争（时序约束面）：运行中任务不可 retry（400），
    取消落定后方可 retry；cancel 与 retry 不得互相覆盖状态。"""
    e2e_server.transcriber.delay = 1.5
    media = make_media_file("cancel-vs-retry.mp3")
    job_id = requests.post(
        f"{e2e_server.base_url}/api/v1/jobs",
        json={"source_type": "local", "audio_path": str(media), "title": "取消与重试竞争"},
        timeout=5,
    ).json()["job_id"]
    wait_job(job_id, {"running"})

    cancel_res = requests.post(f"{e2e_server.base_url}/api/v1/jobs/{job_id}/cancel", timeout=5)
    assert cancel_res.status_code == 200
    # 取消请求已受理但任务尚未到 cancelled 终态：retry 必须被拒（400，非终态不可重试）
    retry_res = requests.post(f"{e2e_server.base_url}/api/v1/jobs/{job_id}/retry", timeout=5)
    assert retry_res.status_code == 400

    detail = wait_job(job_id, {"cancelled"})
    assert detail["retry_count"] == 0, "被拒的 retry 不得改变任务状态"

    retry_res = requests.post(f"{e2e_server.base_url}/api/v1/jobs/{job_id}/retry", timeout=5)
    assert retry_res.status_code == 200
    wait_job(job_id, {"completed"})


# ---------------------------------------------------------------- 重启恢复

def test_running_job_resumes_after_restart(tmp_path, monkeypatch, make_media_file) -> None:
    """running → 进程重启 → resume → completed（生命周期 4）。

    真实 uvicorn + 真实调度两次起停，同一 SQLite 与产物目录：
    - 阶段 A：转写阻塞在 gate 上，任务到 running 后停服；放行 gate 让 worker 收尾，
      随后把 DB 改写回崩溃形态（status=running、progress 清空）——精确模拟
      「进程在运行中被 kill」留下的现场（真实场景由单实例锁保证旧进程已消亡）；
    - 阶段 B：重启服务，resume_pending_jobs 把 running 任务重新入队，
      转写缓存命中（.txt 已在盘）、总结真实重跑，任务到 completed。
    """
    from conftest import _start_isolated_server

    from fakes import FakeSummarizer

    from video_to_summary.web import tasks as web_tasks

    gate = threading.Event()

    class _GateTranscriber(FakeTranscriber):
        def transcribe(self, audio_path, cancel_check=None):
            gate.wait(timeout=10)
            return super().transcribe(audio_path, cancel_check)

    blocking = _GateTranscriber()
    summarizer_fake = FakeSummarizer()
    monkeypatch.setattr(web_tasks, "_build_transcriber", lambda settings, cfg: blocking)
    monkeypatch.setattr(web_tasks, "_build_summarizer", lambda settings, cfg: summarizer_fake)
    monkeypatch.setattr(web_tasks, "_build_polisher", lambda settings, cfg: None)

    base_url, out_dir, server, thread = _start_isolated_server(
        tmp_path, monkeypatch, thread_name="restart-a"
    )
    db_path = tmp_path / "app.db"
    media = make_media_file("resume.mp3")
    job_id = requests.post(
        f"{base_url}/api/v1/jobs",
        json={"source_type": "local", "audio_path": str(media), "title": "重启恢复"},
        timeout=5,
    ).json()["job_id"]

    deadline = time.time() + 10
    while time.time() < deadline:
        detail = requests.get(f"{base_url}/api/v1/jobs/{job_id}", timeout=5).json()
        if detail["status"] == "running":
            break
        time.sleep(0.05)
    else:
        raise AssertionError(f"任务未进入 running: {detail['status']}")

    # 停服（等价进程退出：lifespan 收尾会释放单实例锁）
    server.should_exit = True
    thread.join(timeout=5)
    gate.set()
    _wait_db_status(db_path, job_id, {"completed"})

    # 把 DB 改写为「运行中崩溃」现场：状态 running、事件链清空、无 result_paths
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE jobs SET status = 'running', progress = '[]', result_paths = NULL WHERE job_id = ?",
            (job_id,),
        )
        conn.commit()
    finally:
        conn.close()

    # 阶段 B：重启服务（同一 DB/产物目录），换上真实节奏的 fake 转写器
    fast = FakeTranscriber()
    monkeypatch.setattr(web_tasks, "_build_transcriber", lambda settings, cfg: fast)
    base_url2, out_dir2, server2, thread2 = _start_isolated_server(
        tmp_path, monkeypatch, thread_name="restart-b"
    )
    try:
        assert base_url2 != base_url  # 新端口新实例
        deadline = time.time() + 15
        last = None
        while time.time() < deadline:
            res = requests.get(f"{base_url2}/api/v1/jobs/{job_id}", timeout=5)
            assert res.status_code == 200, "resume 后任务必须仍然可见"
            last = res.json()
            if last["status"] == "completed":
                break
            time.sleep(0.1)
        else:
            raise AssertionError(f"resume 后任务未完成: {last and last['status']}")

        # resume 语义：转写缓存命中（磁盘 .txt 复用，不重复调用转写器）、总结重跑
        assert fast.calls == 0
        events = [e["event"] for e in last["progress"]]
        assert "transcribe_done" in events
        assert (out_dir2 / job_id / "resume.txt").exists()
        assert (out_dir2 / job_id / "resume.summary.md").exists()
        assert summarizer_fake.calls >= 2, "summary 无缓存语义：resume 必须重新生成总结"
    finally:
        server2.should_exit = True
        thread2.join(timeout=5)


# ---------------------------------------------------------------- 并发控制

class _ConcurrencyTracker(FakeTranscriber):
    """记录同时处于 transcribe 内的最大并发数的转写器。"""

    def __init__(self, *args, delay: float = 0.0, **kwargs) -> None:
        super().__init__(*args, delay=delay, **kwargs)
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0

    def transcribe(self, audio_path, cancel_check=None):
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            return super().transcribe(audio_path, cancel_check)
        finally:
            with self._lock:
                self._active -= 1


def test_max_concurrent_one_executes_strictly_serially(
    e2e_server, make_media_file, wait_job, monkeypatch
) -> None:
    """max_concurrent=1：多个任务严格按并发限制串行执行（生命周期 9）。"""
    from video_to_summary.web import tasks as web_tasks

    monkeypatch.setenv("VIDEO_TO_SUMMARY_MAX_CONCURRENT", "1")
    # 信号量/线程池惰性创建且为模块级单例：先归零，让本用例以 limit=1 重建
    monkeypatch.setattr(web_tasks, "_job_semaphore", None)
    monkeypatch.setattr(web_tasks, "_job_executor", None)

    tracker = _ConcurrencyTracker(delay=0.15)
    monkeypatch.setattr(web_tasks, "_build_transcriber", lambda settings, cfg: tracker)

    job_ids = []
    for i in range(3):
        media = make_media_file(f"serial-{i}.mp3")
        job_ids.append(
            requests.post(
                f"{e2e_server.base_url}/api/v1/jobs",
                json={"source_type": "local", "audio_path": str(media), "title": f"串行{i}"},
                timeout=5,
            ).json()["job_id"]
        )
    for job_id in job_ids:
        detail = wait_job(job_id, {"completed"}, timeout=30)
        assert detail["error"] in (None, "")
    assert tracker.max_active == 1, f"limit=1 下出现并发转写: max_active={tracker.max_active}"


def test_two_jobs_run_in_parallel_without_interference(
    e2e_server, make_media_file, wait_job, monkeypatch
) -> None:
    """两个不同 Job 并行执行互不影响（生命周期 10）：并发度达到 2，产物各归其主。"""
    from video_to_summary.web import tasks as web_tasks

    monkeypatch.delenv("VIDEO_TO_SUMMARY_MAX_CONCURRENT", raising=False)
    monkeypatch.setattr(web_tasks, "_job_semaphore", None)
    monkeypatch.setattr(web_tasks, "_job_executor", None)

    tracker = _ConcurrencyTracker(delay=0.4)
    monkeypatch.setattr(web_tasks, "_build_transcriber", lambda settings, cfg: tracker)

    created = []
    for i in range(2):
        media = make_media_file(f"parallel-{i}.mp3")
        job_id = requests.post(
            f"{e2e_server.base_url}/api/v1/jobs",
            json={"source_type": "local", "audio_path": str(media), "title": f"并行{i}"},
            timeout=5,
        ).json()["job_id"]
        created.append(job_id)

    for job_id in created:
        detail = wait_job(job_id, {"completed"}, timeout=30)
        assert detail["error"] in (None, "")
    assert tracker.max_active == 2, f"两个任务应真实并行: max_active={tracker.max_active}"

    # 产物互不串扰：各自目录、各自 source_id
    for i, job_id in enumerate(created):
        job_dir = e2e_server.output_dir / job_id
        assert (job_dir / f"parallel-{i}.txt").exists()
        assert (job_dir / f"parallel-{i}.summary.md").exists()
