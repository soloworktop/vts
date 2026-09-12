"""历史数据导出/导入（web/history_io.py）单测。

DB 隔离同 test_fts_index（tmp DB + reset）；产物目录用独立 tmp_path 并
monkeypatch tasks.output_base（history_io._export_dir 经 tasks 延迟导入）。
"""

import io
import shutil
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from video_to_summary import db as store_db
from video_to_summary.web import history_io


@pytest.fixture()
def io_env(tmp_path, monkeypatch):
    from video_to_summary.web import tasks as web_tasks

    monkeypatch.setattr(store_db, "DATABASE_PATH", tmp_path / "app.db")
    monkeypatch.setattr(store_db, "LEGACY_LLM_FILE", tmp_path / "missing_llm.json")
    monkeypatch.setattr(store_db, "LEGACY_TEMPLATE_FILE", tmp_path / "missing_tpl.json")
    store_db.reset()
    out_dir = tmp_path / "output"
    monkeypatch.setattr(web_tasks, "output_base", lambda: str(out_dir))
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _make_completed_job(job_id: str, title: str, transcript: str, out_dir: Path, *, status: str = "completed") -> None:
    """直接落任务行 + 产物文件（聚焦 history_io 层行为）。"""
    store_db.init_db()
    job_dir = out_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    summary_path = job_dir / f"{job_id}.summary.md"
    transcript_path = job_dir / f"{job_id}.txt"
    summary_path.write_text(f"# {title}\n总结正文", encoding="utf-8")
    transcript_path.write_text(transcript, encoding="utf-8")
    with store_db.get_conn() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, status, title, source_type, source, payload, result_paths,"
            " error, progress, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                status,
                title,
                "url",
                "https://example.com/v",
                json.dumps({"url": "https://example.com/v", "source_type": "url"}),
                json.dumps({"summary": str(summary_path), "transcript": str(transcript_path)}),
                "" if status == "completed" else "boom",
                json.dumps([{"event": "started", "payload": {}}]),
                1000.0,
                1000.0,
            ),
        )
    if status != "completed":
        job_dir.joinpath(f"{job_id}.summary.md").unlink(missing_ok=True)
        job_dir.joinpath(f"{job_id}.txt").unlink(missing_ok=True)


def _export_to_bytes() -> bytes:
    tmp_path, manifest = history_io.build_export_bundle(app_version="test")
    try:
        return tmp_path.read_bytes(), manifest
    finally:
        tmp_path.unlink(missing_ok=True)


def test_export_bundle_shape_and_terminal_only(io_env):
    """包含 manifest/jobs.json/templates.json + 产物；非终态任务被跳过并计数。"""
    _make_completed_job("j1", "任务一", "转写正文关键词", io_env)
    _make_completed_job("j2", "失败任务", "x", io_env, status="failed")
    store_db.init_db()
    with store_db.get_conn() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, status, title, source_type, payload, progress, created_at, updated_at)"
            " VALUES ('j3', 'running', '运行中', 'url', '{}', '[]', 1.0, 1.0)"
        )

    data, manifest = _export_to_bytes()
    assert manifest["format"] == history_io.FORMAT_NAME
    assert manifest["schema_version"] == store_db.SCHEMA_VERSION
    assert manifest["counts"]["jobs"] == 2
    assert manifest["counts"]["skipped_nonterminal"] == 1
    # j1（completed）有 summary+transcript 两份产物；j2（failed）产物已被清理 → 0
    assert manifest["counts"]["artifacts"] == 2

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        jobs = json.loads(zf.read("jobs.json"))
    assert {"manifest.json", "jobs.json", "templates.json"} <= names
    assert "artifacts/j1/j1.summary.md" in names
    assert {j["job_id"] for j in jobs} == {"j1", "j2"}
    by_id = {j["job_id"]: j for j in jobs}
    assert by_id["j1"]["labels"] == []
    assert by_id["j1"]["payload"]["url"] == "https://example.com/v"


def test_import_roundtrip_restores_jobs_labels_templates_artifacts(io_env):
    """roundtrip：任务/标签/模板/产物/result_paths 重写全部还原。"""
    from video_to_summary.web.label_store import set_job_labels

    _make_completed_job("j1", "任务一", "转写正文", io_env)
    set_job_labels("j1", ["迁移标签"])
    with store_db.get_conn() as conn:
        conn.execute(
            "INSERT INTO summary_templates (name, prompt, created_at, updated_at) VALUES (?,?,?,?)",
            ("自定义模板", "用自定义风格总结", 1.0, 1.0),
        )
    data, _manifest = _export_to_bytes()

    # 清空库模拟另一台机器（保留产物目录本身不影响：导入会重写计数）
    with store_db.get_conn() as conn:
        for table in ("job_labels", "labels", "summary_templates", "jobs"):
            conn.execute(f"DELETE FROM {table}")
    counters = history_io.import_bundle(data, io_env)

    assert counters["imported"] == 1
    assert counters["artifacts_restored"] == 2
    assert counters["labels_created"] == 1
    assert counters["templates_created"] == 1
    with store_db.get_conn() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE job_id='j1'").fetchone()
        labels = [r["name"] for r in conn.execute(
            "SELECT l.name FROM job_labels jl JOIN labels l ON jl.label_id = l.id WHERE jl.job_id='j1'"
        ).fetchall()]
        tpl = conn.execute("SELECT prompt FROM summary_templates WHERE name='自定义模板'").fetchone()
    assert job["title"] == "任务一"
    assert labels == ["迁移标签"]
    assert tpl is not None
    # result_paths 已重写为本机新路径且文件真实存在
    restored = json.loads(job["result_paths"])
    for key in ("summary", "transcript"):
        assert Path(restored[key]).is_file()
        assert str(io_env) in restored[key]


def _wipe_jobs() -> None:
    """清空任务侧表（模拟导入到另一台机器的空库）。"""
    store_db.init_db()
    with store_db.get_conn() as conn:
        for table in ("job_labels", "labels", "summary_templates", "jobs"):
            conn.execute(f"DELETE FROM {table}")


def test_import_is_idempotent_on_rerun(io_env):
    """重复导入同一包：全部 skipped_existing，不产生重复行。"""
    _make_completed_job("j1", "任务一", "正文", io_env)
    data, _m = _export_to_bytes()
    _wipe_jobs()
    first = history_io.import_bundle(data, io_env)
    assert first["imported"] == 1
    second = history_io.import_bundle(data, io_env)
    assert second["imported"] == 0
    assert second["skipped_existing"] == 1


def test_import_rejects_newer_schema_version(io_env):
    """fail-closed：包 schema_version 高于本机 → 拒绝（绝不猜测式导入）。"""
    from video_to_summary.web import history_io as hio

    _make_completed_job("j1", "任务一", "正文", io_env)
    data, _m = _export_to_bytes()
    # 篡改 manifest 的 schema_version 为未来版本
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        entries = {i.filename: zf.read(i.filename) for i in zf.infolist()}
    manifest = json.loads(entries["manifest.json"])
    manifest["schema_version"] = store_db.SCHEMA_VERSION + 99
    entries["manifest.json"] = json.dumps(manifest).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)

    with pytest.raises(hio.HistoryBundleError, match="升级应用"):
        history_io.import_bundle(buf.getvalue(), io_env)


def test_import_rejects_unknown_format(io_env):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"format": "someone-else", "format_version": 1, "schema_version": 1}))
    with pytest.raises(history_io.HistoryBundleError, match="format"):
        history_io.import_bundle(buf.getvalue(), io_env)


def test_import_rejects_bad_zip(io_env):
    with pytest.raises(history_io.HistoryBundleError, match="zip"):
        history_io.import_bundle(b"not a zip at all", io_env)


def test_import_rejects_path_traversal_member(io_env):
    """zip 成员路径穿越（../ 逃逸）被拒绝，不落任何文件。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps({
            "format": history_io.FORMAT_NAME, "format_version": 1,
            "schema_version": store_db.SCHEMA_VERSION,
        }))
        zf.writestr("../evil.txt", "pwned")
    with pytest.raises(history_io.HistoryBundleError, match="非法路径"):
        history_io.import_bundle(buf.getvalue(), io_env)


def test_import_normalizes_old_missing_columns(io_env):
    """旧 schema 包（缺 v2 增列 source/retried_at/retry_count）按默认值归一导入。"""
    # 构造手工旧形态包：job dict 只含 v1 时代字段（缺 v2 增列）
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps({
            "format": history_io.FORMAT_NAME, "format_version": 1,
            "schema_version": 1,
        }))
        zf.writestr("jobs.json", json.dumps([{
            "job_id": "old1",
            "status": "completed",
            "title": "旧版本任务",
            "source_type": "url",
            "payload": {"url": "https://example.com/old"},
            "progress": [],
            "created_at": 1.0,
            "updated_at": 1.0,
        }]))
        zf.writestr("templates.json", "[]")
    counters = history_io.import_bundle(buf.getvalue(), io_env)
    assert counters["imported"] == 1
    with store_db.get_conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id='old1'").fetchone()
    assert row["source"] == ""           # v2 增列缺省
    assert row["retry_count"] == 0
    assert row["retried_at"] is None


def test_import_result_paths_without_artifacts_kept(io_env):
    """包内缺产物的任务：记录-only 导入，result_paths 保留原值（前端 404 降级）。"""
    _make_completed_job("j1", "任务一", "正文", io_env)
    data, _m = _export_to_bytes()
    # 从包里剔除产物，只留记录
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        entries = {i.filename: zf.read(i.filename) for i in zf.infolist() if not i.filename.startswith("artifacts/")}
    _wipe_jobs()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    counters = history_io.import_bundle(buf.getvalue(), io_env)
    assert counters["imported"] == 1 and counters["artifacts_restored"] == 0
    with store_db.get_conn() as conn:
        row = conn.execute("SELECT result_paths FROM jobs WHERE job_id='j1'").fetchone()
    paths = json.loads(row["result_paths"])
    assert paths["summary"]  # 原路径保留（指向导出机器的位置）


# ---------------------------------------------------------------- 修复回归（v0.4.0 审查）

def test_import_rejects_traversal_job_id(io_env):
    """job_id 含路径穿越分量（../、绝对路径、. / ..）→ skipped_invalid，
    绝不写 output 根目录之外的任何文件。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps({
            "format": history_io.FORMAT_NAME, "format_version": 1,
            "schema_version": store_db.SCHEMA_VERSION,
        }))
        zf.writestr("jobs.json", json.dumps([
            {"job_id": "../evil", "status": "completed", "title": "穿越", "payload": {}, "progress": []},
            {"job_id": "..", "status": "completed", "title": "父目录", "payload": {}, "progress": []},
            {"job_id": "/abs/path", "status": "completed", "title": "绝对", "payload": {}, "progress": []},
        ]))
        zf.writestr("templates.json", "[]")
    counters = history_io.import_bundle(buf.getvalue(), io_env)
    assert counters["imported"] == 0
    assert counters["skipped_invalid"] == 3
    with store_db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 0
    # output 目录之外无残留（escape 只可能落在 output 父目录，即 tmp_path 根）
    assert not (io_env.parent / "evil").exists()


def test_import_rolls_back_on_midway_failure(io_env, monkeypatch):
    """写库中途失败 → 整体回滚：jobs/labels/templates/job_labels 行数与导入前一致。"""
    from video_to_summary.web import label_store

    _make_completed_job("j1", "任务一", "正文", io_env)
    # 带标签：让导入走到标签重映射分支，中途失败点才有机会触发
    from video_to_summary.web.label_store import set_job_labels

    set_job_labels("j1", ["回滚标签"])
    data, _m = _export_to_bytes()
    _wipe_jobs()

    def _boom(conn, name):
        raise RuntimeError("midway failure")

    monkeypatch.setattr(label_store, "_get_or_create_label_id", _boom)
    with pytest.raises(RuntimeError):
        history_io.import_bundle(data, io_env)

    store_db.init_db()
    with store_db.get_conn() as conn:
        for table in ("jobs", "job_labels", "labels", "summary_templates"):
            n = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            assert n == 0, f"{table} 未回滚"


def test_export_empty_library_roundtrip(io_env):
    """空库导出：manifest counts 全 0，包仍可被幂等导入。"""
    data, manifest = _export_to_bytes()
    assert manifest["counts"]["jobs"] == 0
    counters = history_io.import_bundle(data, io_env)
    assert counters["imported"] == 0
    assert counters["skipped_existing"] == 0


def test_export_import_roundtrip_carries_summary_edited_at(io_env):
    """v9 编辑标记随包导出并在导入端还原；旧包缺失字段补 None（未编辑）。"""
    from video_to_summary.web import history_io

    out_dir = io_env
    _make_completed_job("job-edit", "编辑任务", "转写内容", out_dir)
    with store_db.get_conn() as conn:
        conn.execute("UPDATE jobs SET summary_edited_at = ? WHERE job_id = 'job-edit'", (1770000000.0,))

    bundle, _ = history_io.build_export_bundle()
    data = bundle.read_bytes()
    bundle.unlink(missing_ok=True)

    # 导入端 = 另一个空库 + 空产物目录（同 fixture 再跑一次等价隔离：直接改输出根不可行，
    # 这里以「先删后导」保证导入侧落到空库）
    with store_db.get_conn() as conn:
        conn.execute("DELETE FROM jobs WHERE job_id = 'job-edit'")
    shutil.rmtree(out_dir / "job-edit")

    counters = history_io.import_bundle(data, out_dir)
    assert counters["imported"] == 1

    with store_db.get_conn() as conn:
        row = conn.execute("SELECT summary_edited_at FROM jobs WHERE job_id = 'job-edit'").fetchone()
    assert row["summary_edited_at"] == pytest.approx(1770000000.0)


def test_import_old_bundle_without_summary_edited_at_defaults_none(io_env):
    """旧格式包（schema v8 前导出）无该字段 → 导入为 None（未编辑），不报错。"""
    import json as _json
    import zipfile

    from video_to_summary.web import history_io

    out_dir = io_env
    manifest = {
        "format": history_io.FORMAT_NAME,
        "format_version": history_io.FORMAT_VERSION,
        "schema_version": 8,
        "app_version": "test",
        "exported_at": 1.0,
        "counts": {"jobs": 1, "skipped_nonterminal": 0, "artifacts": 0},
    }
    job = {
        "job_id": "job-old",
        "status": "completed",
        "title": "旧包任务",
        "source_type": "url",
        "source": "",
        "retried_at": None,
        "retry_count": 0,
        "payload": {},
        "result_paths": None,
        "error": "",
        "progress": [],
        "created_at": 1.0,
        "updated_at": 1.0,
        "labels": [],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", _json.dumps(manifest))
        zf.writestr("jobs.json", _json.dumps([job]))
        zf.writestr("templates.json", "[]")
    counters = history_io.import_bundle(buf.getvalue(), out_dir)
    assert counters["imported"] == 1
    with store_db.get_conn() as conn:
        row = conn.execute("SELECT summary_edited_at FROM jobs WHERE job_id = 'job-old'").fetchone()
    assert row["summary_edited_at"] is None
