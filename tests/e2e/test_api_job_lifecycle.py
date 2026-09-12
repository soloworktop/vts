"""L1 E2E：真实 HTTP + 真实任务调度 + 真实 pipeline，fakes 只替换网络边界。

覆盖任务全生命周期：创建 → 阶段事件 → 产物落盘 → 文件回读 → 取消/重试/删除/
无 Key 降级。与 `tests/test_web.py`（TestClient + patch enqueue_job 的单元层）
互补，这里验证的是端到端装配是否成立。
"""

import pytest
import requests

from fakes import STYLE_RICH_MARKERS, FakeUrlSource

pytestmark = pytest.mark.e2e

TERMINAL = {"completed", "failed", "cancelled"}


def _events(detail: dict) -> list[str]:
    return [item["event"] for item in detail.get("progress", [])]


def _event_positions(detail: dict) -> dict[str, int]:
    return {event: idx for idx, event in enumerate(_events(detail))}


def test_local_job_happy_path(e2e_server, make_media_file, wait_job) -> None:
    media = make_media_file("happy.mp3")
    res = requests.post(
        f"{e2e_server.base_url}/api/v1/jobs",
        json={"source_type": "local", "audio_path": str(media), "title": "本地测试任务"},
        timeout=5,
    )
    assert res.status_code == 200
    created = res.json()
    job_id = created["job_id"]
    assert created["status"] == "pending"

    detail = wait_job(job_id, {"completed"})
    assert detail["status"] == "completed"
    assert detail["error"] in (None, "")

    # 分片进度事件（fake 转写器回报 1/1；真实 StepASR 为 i/N）已被 web 层接线入 progress
    assert "transcribe_progress" in _events(detail)

    # 事件链：下载 → 转写 → 摘要 → 完成
    pos = _event_positions(detail)
    assert pos["download_start"] < pos["transcribe_start"] < pos["summarize_start"] < pos["completed"]

    # 产物落盘（output/<job_id>/<source_id>.*，source_id = 文件 stem）
    job_dir = e2e_server.output_dir / job_id
    summary_path = job_dir / "happy.summary.md"
    assert summary_path.exists(), f"summary 缺失: {sorted(p.name for p in job_dir.iterdir())}"
    assert (job_dir / "happy.txt").exists()
    assert (job_dir / "happy.segments.json").exists()
    assert (job_dir / "happy.srt").exists()
    # 未启用文本优化 → 无 polished 产物
    assert not (job_dir / "happy.polished.txt").exists()

    summary_md = summary_path.read_text(encoding="utf-8")
    assert "# 本地测试任务" in summary_md
    for marker in STYLE_RICH_MARKERS:
        assert marker in summary_md, f"样式契约内容缺失: {marker}"

    transcript = (job_dir / "happy.txt").read_text(encoding="utf-8")
    assert "fake transcript line one" in transcript
    srt = (job_dir / "happy.srt").read_text(encoding="utf-8")
    assert "fake segment one" in srt

    # result_paths 与 /file 端点回读
    assert detail["result_paths"]["summary"] == str(summary_path)
    file_res = requests.get(
        f"{e2e_server.base_url}/api/v1/jobs/{job_id}/file",
        params={"path": detail["result_paths"]["summary"]},
        timeout=5,
    )
    assert file_res.status_code == 200
    assert "核心结论" in file_res.json()["content"]


def test_url_job_title_rewrite_and_events(e2e_server, make_media_file, wait_job, monkeypatch) -> None:
    from video_to_summary.web import tasks as web_tasks

    media = make_media_file()
    source = FakeUrlSource(media, title="E2E 视频标题", duration=42)
    monkeypatch.setattr(web_tasks, "_build_source", lambda settings, job: source)

    res = requests.post(
        f"{e2e_server.base_url}/api/v1/jobs",
        json={"source_type": "url", "url": "https://example.test/watch?v=e2e"},
        timeout=5,
    )
    assert res.status_code == 200
    job_id = res.json()["job_id"]

    detail = wait_job(job_id, {"completed"})
    # 不带 title 创建 → 标题自动派生为原始 URL；download_done 携带真实标题后回写并持久化
    # （用户显式输入的标题不回写覆盖，见 test_web.py::test_real_title_writeback_preserves_user_title）
    assert detail["title"] == "E2E 视频标题"
    done_payload = detail["progress"][_event_positions(detail)["download_done"]]["payload"]
    assert done_payload["title"] == "E2E 视频标题"
    assert done_payload["duration"] == 42

    summary_md = (e2e_server.output_dir / job_id / "e2e-fake-video.summary.md").read_text(encoding="utf-8")
    assert "# E2E 视频标题" in summary_md
    assert "example.test" in summary_md  # to_markdown 的「来源」行


def test_transcribe_failure_marks_failed(e2e_server, make_media_file, wait_job) -> None:
    e2e_server.transcriber.fail = RuntimeError("boom-transcribe")
    media = make_media_file()
    res = requests.post(
        f"{e2e_server.base_url}/api/v1/jobs",
        json={"source_type": "local", "audio_path": str(media), "title": "会失败的任务"},
        timeout=5,
    )
    job_id = res.json()["job_id"]

    detail = wait_job(job_id, {"failed"})
    assert detail["status"] == "failed"
    assert "boom-transcribe" in (detail["error"] or "")


def test_cancel_running_job(e2e_server, make_media_file, wait_job) -> None:
    e2e_server.transcriber.delay = 2.0
    media = make_media_file()
    res = requests.post(
        f"{e2e_server.base_url}/api/v1/jobs",
        json={"source_type": "local", "audio_path": str(media), "title": "要取消的任务"},
        timeout=5,
    )
    job_id = res.json()["job_id"]
    wait_job(job_id, {"running"})

    cancel_res = requests.post(f"{e2e_server.base_url}/api/v1/jobs/{job_id}/cancel", timeout=5)
    assert cancel_res.status_code == 200

    detail = wait_job(job_id, {"cancelled"})
    assert "cancelled" in _events(detail)


def test_retry_after_failure(e2e_server, make_media_file, wait_job) -> None:
    e2e_server.transcriber.fail = RuntimeError("first-attempt-fails")
    media = make_media_file("retry.mp3")
    res = requests.post(
        f"{e2e_server.base_url}/api/v1/jobs",
        json={"source_type": "local", "audio_path": str(media), "title": "重试任务"},
        timeout=5,
    )
    job_id = res.json()["job_id"]
    wait_job(job_id, {"failed"})

    # 预埋 stale 文件：retry 清空输出目录后必须消失
    job_dir = e2e_server.output_dir / job_id
    stale = job_dir / "stale-artifact.txt"
    stale.write_text("should be wiped", encoding="utf-8")

    e2e_server.transcriber.fail = None
    retry_res = requests.post(f"{e2e_server.base_url}/api/v1/jobs/{job_id}/retry", timeout=5)
    assert retry_res.status_code == 200

    detail = wait_job(job_id, {"completed"})
    assert detail["retry_count"] == 1
    assert detail["retried_at"]
    assert not stale.exists()
    assert (job_dir / "retry.summary.md").exists()


def test_delete_terminal_job(e2e_server, make_media_file, wait_job) -> None:
    media = make_media_file()
    res = requests.post(
        f"{e2e_server.base_url}/api/v1/jobs",
        json={"source_type": "local", "audio_path": str(media), "title": "待删除任务"},
        timeout=5,
    )
    job_id = res.json()["job_id"]
    wait_job(job_id, {"completed"})
    job_dir = e2e_server.output_dir / job_id
    assert job_dir.exists()

    del_res = requests.delete(f"{e2e_server.base_url}/api/v1/jobs/{job_id}", timeout=5)
    assert del_res.status_code == 200
    assert del_res.json()["deleted"] is True

    assert requests.get(f"{e2e_server.base_url}/api/v1/jobs/{job_id}", timeout=5).status_code == 404
    assert not job_dir.exists()
    jobs = requests.get(f"{e2e_server.base_url}/api/v1/jobs", timeout=5).json()["jobs"]
    assert all(j["job_id"] != job_id for j in jobs)


def test_no_key_degradation_marks_skip_event(e2e_server, make_media_file, wait_job, monkeypatch) -> None:
    from video_to_summary.web import tasks as web_tasks

    # 无可用 summarizer（等价于默认 profile 未填 Key 的降级路径）
    monkeypatch.setattr(web_tasks, "_build_summarizer", lambda settings, cfg: None)
    media = make_media_file()
    res = requests.post(
        f"{e2e_server.base_url}/api/v1/jobs",
        json={"source_type": "local", "audio_path": str(media), "title": "降级任务"},
        timeout=5,
    )
    job_id = res.json()["job_id"]

    detail = wait_job(job_id, {"completed"})
    # 不允许静默缺产物：必须显式推送 summarize_skipped
    assert "summarize_skipped" in _events(detail)
    job_dir = e2e_server.output_dir / job_id
    assert (job_dir / "sample.txt").exists()


def test_history_list_shape(e2e_server, make_media_file, wait_job) -> None:
    media = make_media_file()
    local_res = requests.post(
        f"{e2e_server.base_url}/api/v1/jobs",
        json={"source_type": "local", "audio_path": str(media), "title": "列表形状-本地"},
        timeout=5,
    )
    url_res = requests.post(
        f"{e2e_server.base_url}/api/v1/jobs",
        json={"source_type": "url", "url": "https://example.test/watch?v=shape", "title": "列表形状-URL"},
        timeout=5,
    )
    wait_job(local_res.json()["job_id"], TERMINAL)
    wait_job(url_res.json()["job_id"], TERMINAL)

    jobs = requests.get(f"{e2e_server.base_url}/api/v1/jobs", timeout=5).json()["jobs"]
    by_id = {j["job_id"]: j for j in jobs}
    assert set(by_id) >= {local_res.json()["job_id"], url_res.json()["job_id"]}

    local_row = by_id[local_res.json()["job_id"]]
    assert local_row["source_type"] == "local"
    assert local_row["source_path"] == str(media)
    assert local_row["status"] == "completed"
    assert local_row["created_at"]
    assert local_row["title"] == "列表形状-本地"

    url_row = by_id[url_res.json()["job_id"]]
    assert url_row["source_type"] == "url"
    assert url_row["source_url"] == "https://example.test/watch?v=shape"


def test_job_labels_end_to_end(e2e_server, make_media_file, wait_job) -> None:
    """标签端到端：创建带标签 → 列表/详情含 labels → PUT 修改 → 筛选生效。"""
    media = make_media_file()
    res = requests.post(
        f"{e2e_server.base_url}/api/v1/jobs",
        json={"source_type": "local", "audio_path": str(media), "title": "标签E2E", "labels": ["系列A", "投资"]},
        timeout=5,
    )
    job_id = res.json()["job_id"]
    wait_job(job_id, TERMINAL)

    # 列表与详情都带 labels
    row = next(j for j in requests.get(f"{e2e_server.base_url}/api/v1/jobs", timeout=5).json()["jobs"] if j["job_id"] == job_id)
    assert set(row["labels"]) == {"系列A", "投资"}
    detail = requests.get(f"{e2e_server.base_url}/api/v1/jobs/{job_id}", timeout=5).json()
    assert set(detail["labels"]) == {"系列A", "投资"}

    # PUT 修改标签
    put_res = requests.put(
        f"{e2e_server.base_url}/api/v1/jobs/{job_id}/labels",
        json={"labels": ["系列B"]}, timeout=5,
    )
    assert put_res.status_code == 200
    assert put_res.json()["labels"] == ["系列B"]

    # 筛选：按标签
    filtered = requests.get(f"{e2e_server.base_url}/api/v1/jobs", params={"label": "系列B"}, timeout=5).json()["jobs"]
    assert any(j["job_id"] == job_id for j in filtered)
    not_in = requests.get(f"{e2e_server.base_url}/api/v1/jobs", params={"label": "系列A"}, timeout=5).json()["jobs"]
    assert not any(j["job_id"] == job_id for j in not_in)

    # /api/v1/labels 计数
    labels_data = requests.get(f"{e2e_server.base_url}/api/v1/labels", timeout=5).json()
    by_name = {l["name"]: l for l in labels_data["labels"]}
    assert by_name["系列B"]["count"] >= 1
    # 系列A 被替换后关联解除，但标签行本身保留（count=0，可被管理页删除/合并）
    assert by_name["系列A"]["count"] == 0
