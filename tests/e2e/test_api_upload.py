"""E2E：浏览器上传任务源（POST /api/v1/jobs/upload，真实 HTTP + 真实任务调度）。

与单元层（tests/test_web_upload.py，patch enqueue_job）互补：这里验证
multipart 上传 → 流式落盘 uploads/<uuid>/ → 与本地文件源完全相同的 pipeline
消费 → 托管上传随任务删除回收 的端到端装配。
"""

from pathlib import Path

import pytest
import requests

pytestmark = pytest.mark.e2e

MEDIA_BYTES = b"\xff\xfb" + b"0" * 256


def _upload(base_url: str, name: str = "e2e-upload.mp3", content: bytes = MEDIA_BYTES, **form):
    data = {k: v for k, v in form.items() if v is not None}
    return requests.post(
        f"{base_url}/api/v1/jobs/upload",
        files={"file": (name, content, "audio/mpeg")},
        data=data,
        timeout=30,
    )


def test_upload_job_happy_path_and_artifacts(e2e_server, wait_job) -> None:
    res = _upload(e2e_server.base_url, title="上传E2E")
    assert res.status_code == 200
    job_id = res.json()["job_id"]
    assert res.json()["status"] == "pending"

    detail = wait_job(job_id, {"completed"})
    assert detail["status"] == "completed"
    assert detail["source_type"] == "local"

    # 上传文件落在托管目录 output/uploads/<uuid>/e2e-upload.mp3（原子落盘，无 .part 残留）
    stored = Path(detail["source_path"])
    uploads_root = e2e_server.output_dir / "uploads"
    assert uploads_root == stored.parent.parent
    assert stored.is_file()
    assert stored.read_bytes() == MEDIA_BYTES
    assert not list(stored.parent.glob("*.part"))

    # pipeline 按本地文件源消费（source_id = 文件 stem），产物在 output/<job_id>/
    job_dir = e2e_server.output_dir / job_id
    summary_path = job_dir / "e2e-upload.summary.md"
    assert summary_path.exists(), f"summary 缺失: {sorted(p.name for p in job_dir.iterdir())}"
    assert (job_dir / "e2e-upload.txt").exists()
    assert (job_dir / "e2e-upload.srt").exists()
    assert "# 上传E2E" in summary_path.read_text(encoding="utf-8")


def test_upload_job_delete_removes_managed_upload(e2e_server, wait_job) -> None:
    res = _upload(e2e_server.base_url, name="delete-me.mp3")
    job_id = res.json()["job_id"]
    detail = wait_job(job_id, {"completed"})

    stored = Path(detail["source_path"])
    job_dir = e2e_server.output_dir / job_id
    del_res = requests.delete(f"{e2e_server.base_url}/api/v1/jobs/{job_id}", timeout=5)
    assert del_res.status_code == 200
    # 托管上传（uuid 子目录整体）与产物目录一并回收
    assert not stored.exists()
    assert not stored.parent.exists()
    assert not job_dir.exists()


def test_upload_rejects_unsupported_extension(e2e_server) -> None:
    res = _upload(e2e_server.base_url, name="notes.txt", content=b"hello")
    assert res.status_code == 400
    assert "不支持的文件类型" in res.json()["detail"]


def test_upload_rejects_malformed_labels(e2e_server) -> None:
    res = _upload(e2e_server.base_url, labels="{bad")
    assert res.status_code == 400


def test_upload_oversize_returns_413(e2e_server, monkeypatch) -> None:
    # 上限在请求时读取（uvicorn 与 pytest 同进程），monkeypatch 直接生效
    monkeypatch.setenv("VTS_UPLOAD_MAX_MB", "1")
    res = _upload(e2e_server.base_url, content=b"0" * (1024 * 1024 + 1))
    assert res.status_code == 413
    assert "大小上限" in res.json()["detail"]
