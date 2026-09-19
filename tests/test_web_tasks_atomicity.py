"""Job 状态原子转换（transition_job）与 Web 单实例锁的单元测试。

背景（P1-1 / P0-1 整改）：
- Job 状态修改历史上是「读 status → Python 判断 → 盲写 save()」，并发下 cancel/retry/
  worker 收尾会互相覆盖（completed 被打回 running、重试任务被写回 cancelled 等）。
  transition_job 把判断与写入合并进一条 UPDATE … WHERE status = expected。
- Job 调度/取消/启动恢复都是进程内状态 → VTS Web 明确 single-process / single-worker；
  数据库目录锁文件在第二个进程启动期给出明确错误。
"""

import json
import threading
from pathlib import Path

import pytest

from video_to_summary import constants, db
from video_to_summary.web import tasks as web_tasks

JobStatus = constants.JobStatus
JobEvent = constants.JobEvent


@pytest.fixture()
def tasks_db(tmp_path, monkeypatch):
    """独立 tmp DB + 清空调度器内存状态；enqueue 为 no-op（无事件循环）。"""
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "app.db")
    db.reset()
    web_tasks._jobs.clear()
    web_tasks._cancel_flags.clear()
    monkeypatch.setattr(web_tasks, "enqueue_job", lambda job: None)
    monkeypatch.setattr(web_tasks, "output_base", lambda: str(tmp_path / "output"))
    yield tmp_path
    db.reset()
    web_tasks._jobs.clear()
    web_tasks._cancel_flags.clear()


def _db_status(job_id: str) -> str:
    """直读 DB 的权威状态（绕过可能滞后的内存缓存对象）。"""
    db.init_db()
    with db.get_conn() as conn:
        row = conn.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    assert row is not None, f"job {job_id} 不存在于 DB"
    return row["status"]


# ---------------------------------------------------------------- transition_job

def test_transition_job_wins_when_status_matches(tasks_db):
    job = web_tasks.create_job({"source_type": "url", "url": "https://example.test/v"})
    assert job.status == JobStatus.PENDING
    assert web_tasks.transition_job(job.job_id, JobStatus.PENDING, JobStatus.RUNNING) is True
    assert _db_status(job.job_id) == JobStatus.RUNNING


def test_transition_job_loses_when_status_changed(tasks_db):
    """DB 状态已推进时转换必须失败（affected rows = 0），绝不盲写覆盖。"""
    job = web_tasks.create_job({"source_type": "url", "url": "https://example.test/v"})
    # 模拟另一方已把任务推进到 running
    assert web_tasks.transition_job(job.job_id, JobStatus.PENDING, JobStatus.RUNNING) is True
    # 输家仍按旧快照 pending → cancelled 发起转换：必须失败，DB 保持 running
    assert web_tasks.transition_job(job.job_id, JobStatus.PENDING, JobStatus.CANCELLED) is False
    assert _db_status(job.job_id) == JobStatus.RUNNING


def test_transition_job_rejects_unknown_field(tasks_db):
    job = web_tasks.create_job({"source_type": "url", "url": "https://example.test/v"})
    with pytest.raises(ValueError, match="unsupported field"):
        web_tasks.transition_job(job.job_id, JobStatus.PENDING, JobStatus.RUNNING, fields={"api_key": "x"})


def test_transition_job_concurrent_exactly_one_winner(tasks_db):
    """并发竞争：N 个线程同时发起不同转换，恰好一个获胜，DB 状态与获胜方一致。"""
    job = web_tasks.create_job({"source_type": "url", "url": "https://example.test/v"})
    db.init_db()
    barrier = threading.Barrier(4)
    outcomes: list[tuple[str, bool]] = []
    lock = threading.Lock()

    def attempt(expected: str, new: str) -> None:
        barrier.wait()
        ok = web_tasks.transition_job(job.job_id, expected, new)
        with lock:
            outcomes.append((new, ok))

    threads = [
        threading.Thread(target=attempt, args=(JobStatus.PENDING, JobStatus.RUNNING)),
        threading.Thread(target=attempt, args=(JobStatus.PENDING, JobStatus.CANCELLED)),
        threading.Thread(target=attempt, args=(JobStatus.PENDING, JobStatus.FAILED)),
        threading.Thread(target=attempt, args=(JobStatus.PENDING, JobStatus.RUNNING)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [new for new, ok in outcomes if ok]
    assert len(winners) == 1, f"应恰好一个获胜，实际 {outcomes}"
    assert _db_status(job.job_id) == winners[0]


def test_terminal_write_lost_race_does_not_clobber_retry(tasks_db):
    """worker 收尾的终态写入输给 retry（terminal → pending）时，不得把重试任务打回终态。

    复现竞态：worker 持有 FAILED 前的旧快照，retry 先把任务转回 pending 并入队；
    worker 此刻收尾 mark_cancelled —— 原子转换必须失败，DB 保持 pending。
    """
    job = web_tasks.create_job({"source_type": "url", "url": "https://example.test/v"})
    job.mark_failed("boom")
    assert web_tasks.get_job(job.job_id).status == JobStatus.FAILED

    # retry 把 failed → pending（内存对象已不再代表 DB 真相）
    web_tasks.retry_job(job.job_id)
    assert _db_status(job.job_id) == JobStatus.PENDING

    # 迟到的 worker 收尾：按旧快照 failed 发起 cancelled 转换 → 必须失败
    ok = job.mark_cancelled("cancelled by user", expected_status=JobStatus.FAILED)
    assert ok is False
    assert _db_status(job.job_id) == JobStatus.PENDING


def test_mark_completed_lost_race_falls_back_to_save(tasks_db, monkeypatch):
    """mark_completed 转换意外失败时回退盲写：完成产物已在磁盘，绝不丢 completed 终态。"""
    job = web_tasks.create_job({"source_type": "url", "url": "https://example.test/v"})
    job.mark_running()

    original = web_tasks.transition_job

    def losing_transition(job_id, expected, new, *, fields=None):
        return False  # 强制所有转换失败（白盒：仅覆盖回退分支）

    monkeypatch.setattr(web_tasks, "transition_job", losing_transition)
    job.mark_completed({"summary": "/tmp/x.md"}, elapsed=1.0)
    monkeypatch.setattr(web_tasks, "transition_job", original)

    row = web_tasks.get_job(job.job_id)
    assert row.status == JobStatus.COMPLETED
    assert row.result_paths == {"summary": "/tmp/x.md"}
    events = [e["event"] for e in row.progress.to_list()]
    assert "finalized" in events and "completed" in events


def test_double_run_same_job_single_claim(tasks_db):
    """重复入队保护：两个执行方并发认领同一 pending 任务，恰好一个执行（transcriber 调一次）。"""
    calls = {"n": 0}
    lock = threading.Lock()

    def fake_run(source, transcriber, summarizer, output_dir, *, polisher=None, polisher_title="",
                 on_event=None, cancel_check=None, subtitle_config=None):
        with lock:
            calls["n"] += 1
        out = Path(output_dir) / "v.summary.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("# ok", encoding="utf-8")
        return out

    db.init_db()
    job = web_tasks.create_job({"source_type": "url", "url": "https://example.test/v"})
    original_run = web_tasks.run
    web_tasks.run = fake_run
    try:
        barrier = threading.Barrier(2)

        def worker() -> None:
            barrier.wait()
            web_tasks._run_job_inner(job)

        t1, t2 = threading.Thread(target=worker), threading.Thread(target=worker)
        t1.start(); t2.start(); t1.join(); t2.join()
    finally:
        web_tasks.run = original_run

    assert calls["n"] == 1, "两个执行方必须恰好一个赢得 pending→running 认领"
    assert _db_status(job.job_id) == JobStatus.COMPLETED
    assert [e["event"] for e in web_tasks.get_job(job.job_id).progress.to_list()].count("started") == 1


def test_cancel_pending_after_worker_claimed_falls_to_running_branch(tasks_db):
    """cancel 读到 pending、但 worker 已原子认领（DB=running）时：
    pending→cancelled 转换失败 → 重读按 running 处理（设停止标志），绝不盲写 cancelled。"""
    job = web_tasks.create_job({"source_type": "url", "url": "https://example.test/v"})
    # 模拟竞态：cancel 的内存快照仍是 pending，DB 已被 worker 认领为 running
    db.init_db()
    with db.get_conn() as conn:
        conn.execute("UPDATE jobs SET status = ? WHERE job_id = ?", (JobStatus.RUNNING, job.job_id))

    assert web_tasks.cancel_job(job.job_id) is True
    # 状态保持 running（未被盲写覆盖），停止标志已设
    assert _db_status(job.job_id) == JobStatus.RUNNING
    assert job.job_id in web_tasks._cancel_flags


def test_cancel_running_dropped_when_already_terminal(tasks_db):
    """cancel 读到 running、worker 恰已收尾（DB=completed）时：取消请求作废，
    DB 不得被写回 running（否则重启后 resume 会把已完成任务再跑一遍）。"""
    job = web_tasks.create_job({"source_type": "url", "url": "https://example.test/v"})
    job.mark_running()
    # 模拟竞态：内存快照 running，DB 已 completed
    db.init_db()
    with db.get_conn() as conn:
        conn.execute("UPDATE jobs SET status = ? WHERE job_id = ?", (JobStatus.COMPLETED, job.job_id))

    assert web_tasks.cancel_job(job.job_id) is False
    assert _db_status(job.job_id) == JobStatus.COMPLETED
    fresh = web_tasks.get_job(job.job_id, refresh=True)
    assert "cancel_requested" not in [e["event"] for e in fresh.progress.to_list()]


def test_concurrent_retry_exactly_one_wins(tasks_db):
    """两个并发 retry：恰好一个获胜重建任务，另一个报状态竞争（ValueError → 400）。"""
    job = web_tasks.create_job({"source_type": "url", "url": "https://example.test/v"})
    job.mark_completed({"summary": "/tmp/x.md"})

    results: list[str] = []
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def attempt() -> None:
        barrier.wait()
        try:
            web_tasks.retry_job(job.job_id)
            outcome = "ok"
        except ValueError:
            outcome = "race-lost"
        with lock:
            results.append(outcome)

    t1, t2 = threading.Thread(target=attempt), threading.Thread(target=attempt)
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(results) == ["ok", "race-lost"]
    assert _db_status(job.job_id) == JobStatus.PENDING
    assert web_tasks.get_job(job.job_id).retry_count == 1


def test_delete_job_lost_race_keeps_row(tasks_db):
    """delete 读时终态、删前被 retry 转回 pending：条件删除必须失败并保留任务行。"""
    job = web_tasks.create_job({"source_type": "url", "url": "https://example.test/v"})
    job.mark_completed({"summary": "/tmp/x.md"})

    # 模拟竞态：delete_job 的终态预检已过，retry 恰先把任务转回 pending
    web_tasks.retry_job(job.job_id)

    with pytest.raises(ValueError, match="状态已变化"):
        web_tasks._delete_terminal_job_row(job.job_id)
    assert _db_status(job.job_id) == JobStatus.PENDING

    # 正常终态删除仍然成功
    job.mark_failed("boom")
    web_tasks._delete_terminal_job_row(job.job_id)
    db.init_db()
    with db.get_conn() as conn:
        assert conn.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job.job_id,)).fetchone() is None


# ---------------------------------------------------------------- resume

def test_resume_skips_cancel_requested_running_job(tasks_db, monkeypatch):
    """取消意图已落库后进程重启：resume 必须落 cancelled 终态，不得把任务再跑一遍。"""
    job = web_tasks.create_job({"source_type": "url", "url": "https://example.test/v"})
    job.mark_running()
    # 模拟「cancel 已受理、worker 未及收尾时进程崩溃」：running + cancel_requested
    events = job.progress.to_list() + [{"event": JobEvent.CANCEL_REQUESTED, "payload": {"reason": "stop requested"}}]
    db.init_db()
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET progress = ? WHERE job_id = ?",
            (json.dumps(events, ensure_ascii=False), job.job_id),
        )
    web_tasks._jobs.clear()

    resumed: list[str] = []
    monkeypatch.setattr(web_tasks, "enqueue_job", lambda j: resumed.append(j.job_id))
    web_tasks.resume_pending_jobs()

    assert resumed == [], "已请求停止的任务不得被 resume 重新入队"
    row = web_tasks.get_job(job.job_id)
    assert row.status == JobStatus.CANCELLED
    assert "cancelled" in (row.error or "")


def test_resume_requeues_pending_and_running(tasks_db, monkeypatch):
    """普通 pending/running 任务 resume：重新入队且状态归位 pending、注册内存缓存。"""
    p = web_tasks.create_job({"source_type": "url", "url": "https://example.test/p"})
    r = web_tasks.create_job({"source_type": "url", "url": "https://example.test/r"})
    r.mark_running()
    web_tasks._jobs.clear()

    resumed: list[str] = []
    monkeypatch.setattr(web_tasks, "enqueue_job", lambda j: resumed.append(j.job_id))
    web_tasks.resume_pending_jobs()

    assert sorted(resumed) == sorted([p.job_id, r.job_id])
    for job_id in (p.job_id, r.job_id):
        row = web_tasks.get_job(job_id)
        assert row.status == JobStatus.PENDING
        assert job_id in web_tasks._jobs


# ---------------------------------------------------------------- 单实例锁（P0-1）

def test_scheduler_lock_acquire_idempotent_and_release(tasks_db):
    web_tasks._scheduler_lock_handles.clear()
    web_tasks.acquire_scheduler_lock()
    # 本进程重复获取（同 DB 目录）幂等：不会自锁
    web_tasks.acquire_scheduler_lock()
    web_tasks.release_scheduler_lock()
    # 释放后可重新获取
    web_tasks.acquire_scheduler_lock()
    web_tasks.release_scheduler_lock()
    web_tasks._scheduler_lock_handles.clear()


def test_scheduler_lock_conflict(tasks_db):
    """同一锁文件被另一句柄持有（等价第二进程）时，acquire 必须给出明确错误。"""
    import fcntl
    import os

    web_tasks._scheduler_lock_handles.clear()
    lock_path = web_tasks.scheduler_lock_path()
    # 独立 fd 先持有锁（flock 语义下同进程不同 fd 同样互斥），再清空进程内注册表
    # 模拟「另一个进程」已持锁的场景
    other_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(other_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        web_tasks._scheduler_lock_handles.clear()
        with pytest.raises(web_tasks.SingleInstanceError, match="单进程"):
            web_tasks.acquire_scheduler_lock()
    finally:
        os.close(other_fd)
        web_tasks._scheduler_lock_handles.clear()


def test_scheduler_lock_blocks_second_process(tasks_db):
    """真·跨进程：子进程持有锁期间，本进程 acquire 必须失败；子进程退出后可重新获取。"""
    import subprocess
    import sys
    import time

    web_tasks._scheduler_lock_handles.clear()
    lock_path = web_tasks.scheduler_lock_path()
    holder = subprocess.Popen(
        [
            sys.executable, "-c",
            "import fcntl, os, time\n"
            f"fd = os.open({str(lock_path)!r}, os.O_CREAT | os.O_RDWR, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "print('held', flush=True)\n"
            "time.sleep(15)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        line = holder.stdout.readline().strip()
        assert line == "held"
        with pytest.raises(web_tasks.SingleInstanceError, match="单进程"):
            web_tasks.acquire_scheduler_lock()
    finally:
        holder.kill()
        holder.wait(timeout=5)

    # 锁随进程死亡自动释放：子进程退出后可获取
    web_tasks.acquire_scheduler_lock()
    web_tasks.release_scheduler_lock()
    web_tasks._scheduler_lock_handles.clear()


def test_lifespan_acquires_and_releases_lock(tasks_db, monkeypatch):
    """TestClient lifespan 进入即持锁、退出即释放（重启/收尾语义）。"""
    from fastapi.testclient import TestClient

    from video_to_summary.web import app as web_app

    monkeypatch.setattr(db, "LEGACY_LLM_FILE", tasks_db / "missing.json")
    monkeypatch.setattr(db, "LEGACY_TEMPLATE_FILE", tasks_db / "missing2.json")
    web_tasks._scheduler_lock_handles.clear()
    with TestClient(web_app.app) as client:
        res = client.get("/api/v1/health")
        assert res.status_code == 200
        assert web_tasks.scheduler_lock_path().name in str(web_tasks._scheduler_lock_handles) or web_tasks._scheduler_lock_handles
    assert not web_tasks._scheduler_lock_handles
    web_tasks._scheduler_lock_handles.clear()
