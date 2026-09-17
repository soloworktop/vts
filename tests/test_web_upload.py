"""浏览器上传任务源（POST /api/v1/jobs/upload）单元层测试。

覆盖：上传建任务主链路、大小上限 413、扩展名/labels 校验 400、文件名穿越清洗、
托管上传的删除连带回收（delete_job）、孤儿上传清扫（sweep_orphan_uploads）、
health 暴露 upload_max_mb。与 e2e 层（tests/e2e/test_api_upload.py，真实调度）
互补；本层 patch enqueue_job，不跑 pipeline。
"""

import pytest
from fastapi.testclient import TestClient

from video_to_summary.constants import JobStatus
from video_to_summary.web.app import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolate_db(tmp_path, monkeypatch):
    """每个测试独立使用临时 SQLite、临时上传/输出目录，并禁用真实后台任务调度。"""
    from video_to_summary import db as store_db
    from video_to_summary.web import app as web_app
    from video_to_summary.web import tasks as web_tasks

    monkeypatch.setattr(store_db, "DATABASE_PATH", tmp_path / "app.db")
    monkeypatch.setattr(store_db, "LEGACY_LLM_FILE", tmp_path / "missing_llm_profiles.json")
    monkeypatch.setattr(store_db, "LEGACY_TEMPLATE_FILE", tmp_path / "missing_summary_templates.json")
    store_db.reset()
    web_tasks._jobs.clear()
    web_tasks._cancel_flags.clear()
    monkeypatch.setattr(web_app, "enqueue_job", lambda job: None)
    monkeypatch.setattr(web_tasks, "enqueue_job", lambda job: None)
    # 上传目录与输出目录都锚定 tmp_path（upload_base 每次调用时读 env）
    monkeypatch.setenv("VIDEO_TO_SUMMARY_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("VIDEO_TO_SUMMARY_OUTPUT_DIR", str(tmp_path / "output"))
    yield


def _upload(name: str = "clip.mp3", content: bytes = b"\xff\xfb" + b"0" * 256, **form) -> object:
    data = {k: v for k, v in form.items() if v is not None}
    return client.post(
        "/api/v1/jobs/upload",
        files={"file": (name, content, "audio/mpeg")},
        data=data,
    )


def test_upload_creates_job_and_stores_file(tmp_path) -> None:
    res = _upload("sample.mp3", title="上传任务", labels='["系列A"]')
    assert res.status_code == 200
    job_id = res.json()["job_id"]
    assert res.json()["status"] == "pending"

    detail = client.get(f"/api/v1/jobs/{job_id}").json()
    # 任务以 local 源入库，audio_path 指向服务端托管的上传文件
    assert detail["source_type"] == "local"
    stored = detail["source_path"]
    assert str(tmp_path / "uploads") in stored
    # 原子落盘：最终文件存在，无 .part 残留
    from pathlib import Path

    stored_path = Path(stored)
    assert stored_path.is_file()
    assert stored_path.read_bytes() == b"\xff\xfb" + b"0" * 256
    assert not list(stored_path.parent.glob("*.part"))
    assert stored_path.parent.parent == (tmp_path / "uploads").resolve()
    # 表单字段生效
    assert detail["title"] == "上传任务"
    assert detail["labels"] == ["系列A"]


def test_upload_without_title_defaults_to_filename() -> None:
    res = _upload("clip.mp4")
    assert res.status_code == 200
    detail = client.get(f"/api/v1/jobs/{res.json()['job_id']}").json()
    # 与既有本地文件源同一派生口径：标题 = 文件名（自动派生，可被真实标题回写覆盖）
    assert detail["title"] == "clip.mp4"


def test_upload_rejects_unsupported_extension() -> None:
    res = _upload("notes.txt")
    assert res.status_code == 400
    assert "不支持的文件类型" in res.json()["detail"]

    res = _upload("noext")
    assert res.status_code == 400
    assert "无扩展名" in res.json()["detail"]


def test_upload_rejects_malformed_labels() -> None:
    res = _upload(labels="{bad json")
    assert res.status_code == 400
    assert "labels" in res.json()["detail"]

    res = _upload(labels='["a", 1]')
    assert res.status_code == 400


def test_upload_rejects_missing_file() -> None:
    res = client.post("/api/v1/jobs/upload", data={"title": "x"})
    assert res.status_code == 422  # FastAPI 必填字段校验


def test_upload_oversize_returns_413(monkeypatch) -> None:
    monkeypatch.setenv("VTS_UPLOAD_MAX_MB", "1")
    big = b"0" * (1024 * 1024 + 1)
    res = _upload("big.mp3", content=big)
    assert res.status_code == 413
    assert "大小上限" in res.json()["detail"]


def test_upload_sanitizes_traversal_filename(tmp_path) -> None:
    from pathlib import Path

    res = _upload("../../evil.mp4")
    assert res.status_code == 200
    stored = Path(client.get(f"/api/v1/jobs/{res.json()['job_id']}").json()["source_path"])
    # 目录成分被剥离，文件只落在服务端生成的 uuid 子目录内
    assert stored.name == "evil.mp4"
    assert stored.parent.parent == (tmp_path / "uploads").resolve()
    assert ".." not in str(stored.relative_to((tmp_path / "uploads").resolve()))

    res = _upload("..\\..\\win.mp4")
    assert res.status_code == 200
    stored = Path(client.get(f"/api/v1/jobs/{res.json()['job_id']}").json()["source_path"])
    assert stored.name == "win.mp4"


def test_upload_long_filename_preserves_extension() -> None:
    from pathlib import Path

    # 超过 80 字符上限的合法文件名：限长只截 stem，扩展名保留、白名单不被误拒
    for name in ("很" * 100 + ".mp4", "a" * 90 + ".mp4"):
        res = _upload(name)
        assert res.status_code == 200, res.json()
        stored = Path(client.get(f"/api/v1/jobs/{res.json()['job_id']}").json()["source_path"]).name
        assert stored.endswith(".mp4")
        assert len(stored) <= 80
        assert stored != ".mp4"  # 截断后不能只剩后缀


def test_upload_name_collapsed_to_suffix_gets_fallback() -> None:
    from pathlib import Path

    # 危险字符折叠（? → -）+ 清洗后 stem 为空：回退固定名并保留扩展名
    res = _upload("????.mp4")
    assert res.status_code == 200
    stored = Path(client.get(f"/api/v1/jobs/{res.json()['job_id']}").json()["source_path"]).name
    assert stored == "upload.mp4"


def test_upload_storage_failure_returns_500_with_hint(tmp_path, monkeypatch) -> None:
    from video_to_summary.web import app as web_app

    # 上传目录父路径被普通文件占据 → mkdir 抛 NotADirectoryError → 500 + 可行动指引
    blocker = tmp_path / "afile"
    blocker.write_text("not a dir", encoding="utf-8")
    monkeypatch.setattr(web_app, "upload_base", lambda: str(blocker))
    res = _upload("x.mp3")
    assert res.status_code == 500
    assert "VIDEO_TO_SUMMARY_UPLOAD_DIR" in res.json()["detail"]


def test_failed_job_creation_cleans_up_uploaded_file(tmp_path) -> None:
    from pathlib import Path

    # 建任务失败（未知模板）→ 已落盘的上传文件必须被回收，不留孤儿
    res = _upload("orphan.mp3", summary_template="不存在的模板")
    assert res.status_code == 400
    assert res.json()["detail"].startswith("unknown summary template")
    # 上传目录整体被回收：uploads 下没有任何 uuid 子目录残留
    from video_to_summary.web.tasks import upload_base

    base = Path(upload_base())
    assert base.exists() and list(base.iterdir()) == []


def _mark_terminal(job_id: str) -> None:
    from video_to_summary.web import tasks as web_tasks

    job = web_tasks.get_job(job_id)
    job.status = JobStatus.COMPLETED
    job.save()


def test_delete_job_removes_managed_upload(tmp_path) -> None:
    res = _upload("deleteme.mp3", title="待删除")
    job_id = res.json()["job_id"]
    detail = client.get(f"/api/v1/jobs/{job_id}").json()
    from pathlib import Path

    stored = Path(detail["source_path"])
    job_dir = tmp_path / "output" / job_id
    _mark_terminal(job_id)

    del_res = client.delete(f"/api/v1/jobs/{job_id}")
    assert del_res.status_code == 200
    # 托管上传文件（uuid 子目录整体）与产物目录一并回收
    assert not stored.exists()
    assert not stored.parent.exists()
    assert not job_dir.exists()


def test_delete_job_keeps_user_local_file(tmp_path) -> None:
    # 用户自己的本地文件（uploads 之外）永不删除——托管判定不得误伤
    user_file = tmp_path / "elsewhere" / "mine.mp3"
    user_file.parent.mkdir(parents=True)
    user_file.write_bytes(b"\xff\xfb" + b"0" * 16)

    res = client.post(
        "/api/v1/jobs",
        json={"source_type": "local", "audio_path": str(user_file), "title": "用户文件"},
    )
    job_id = res.json()["job_id"]
    _mark_terminal(job_id)
    assert client.delete(f"/api/v1/jobs/{job_id}").status_code == 200
    assert user_file.exists()


def test_sweep_orphan_uploads(tmp_path) -> None:
    from video_to_summary.web.tasks import sweep_orphan_uploads

    uploads = tmp_path / "uploads"
    # 孤儿目录：没有任何任务引用
    orphan = uploads / ("a" * 32)
    orphan.mkdir(parents=True)
    (orphan / "lost.mp3").write_bytes(b"x" * 8)
    # 被引用目录：上传建任务后残留的合法文件（iterdir 顺序不保证，按内容定位）
    res = _upload("kept.mp3")
    kept_dir = next(p for p in uploads.iterdir() if (p / "kept.mp3").exists())
    assert kept_dir != orphan

    removed = sweep_orphan_uploads()
    assert removed == 1
    assert not orphan.exists()
    assert kept_dir.exists()
    assert (kept_dir / "kept.mp3").exists()


def test_sweep_keeps_dir_with_nested_referenced_file(tmp_path) -> None:
    """引用判定用前缀包含：深层嵌套引用的目录不得被误判为孤儿。"""
    from video_to_summary.web.tasks import sweep_orphan_uploads

    uploads = tmp_path / "uploads"
    nested_file = uploads / ("b" * 32) / "sub" / "deep.mp3"
    nested_file.parent.mkdir(parents=True)
    nested_file.write_bytes(b"x" * 8)
    # 任务直接引用嵌套深层文件（手动填路径可达的形态）
    res = client.post(
        "/api/v1/jobs",
        json={"source_type": "local", "audio_path": str(nested_file), "title": "嵌套引用"},
    )
    assert res.status_code == 200

    assert sweep_orphan_uploads() == 0
    assert nested_file.exists()


def test_health_exposes_upload_max_mb(monkeypatch) -> None:
    monkeypatch.delenv("VTS_UPLOAD_MAX_MB", raising=False)
    assert client.get("/api/v1/health").json()["upload_max_mb"] == 2048
    monkeypatch.setenv("VTS_UPLOAD_MAX_MB", "64")
    assert client.get("/api/v1/health").json()["upload_max_mb"] == 64
