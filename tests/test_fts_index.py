"""jobs_fts 全文检索索引（web/fts_index.py）单测：索引/检索/补漏/降级。

DB 隔离仿照 e2e 层：tmp DB + reset（本文件不依赖 test_web 的 isolate_db，
保持文件独立可跑）。产物文本全部落 tmp_path，零网络。
"""

import json
from pathlib import Path

import pytest

from video_to_summary import db as store_db
from video_to_summary.web import fts_index


@pytest.fixture()
def fts_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store_db, "DATABASE_PATH", tmp_path / "app.db")
    monkeypatch.setattr(store_db, "LEGACY_LLM_FILE", tmp_path / "missing_llm.json")
    monkeypatch.setattr(store_db, "LEGACY_TEMPLATE_FILE", tmp_path / "missing_tpl.json")
    store_db.reset()
    yield tmp_path


def _insert_job(job_id: str, title: str, result_paths: dict | None, status: str = "completed") -> None:
    """直接落一行任务（绕过 Job 模型，聚焦索引层行为）。"""
    store_db.init_db()
    with store_db.get_conn() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, status, title, source_type, payload, result_paths,"
            " progress, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                status,
                title,
                "url",
                json.dumps({"url": "https://example.com/v", "source_type": "url"}),
                json.dumps(result_paths) if result_paths else None,
                "[]",
                1.0,
                1.0,
            ),
        )


def _write_artifacts(tmp_path: Path, job_id: str, transcript: str, summary: str) -> dict:
    out = tmp_path / "output" / job_id
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{job_id}.txt").write_text(transcript, encoding="utf-8")
    (out / f"{job_id}.summary.md").write_text(summary, encoding="utf-8")
    return {
        "transcript": str(out / f"{job_id}.txt"),
        "summary": str(out / f"{job_id}.summary.md"),
    }


def test_fts_table_created_by_migration(fts_db):
    """v8 迁移建 jobs_fts；fts 可用性探测为真（sqlite 自带 fts5）。"""
    store_db.init_db()
    assert store_db.SCHEMA_VERSION >= 8
    with store_db.get_conn() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='jobs_fts'"
        ).fetchone()
    assert row is not None
    assert fts_index.fts_available() is True


def test_index_and_fulltext_search_roundtrip(fts_db, tmp_path):
    """正文命中：转写关键词可搜到，片段含 <mark>，kinds 标明来源。"""
    _insert_job("job1", "一期访谈", _write_artifacts(tmp_path, "job1", "今天我们聊强化学习与机器人", "# 访谈\n强化学习专题"))
    fts_index.index_job_artifacts("job1", {
        "transcript": str(tmp_path / "output" / "job1" / "job1.txt"),
        "summary": str(tmp_path / "output" / "job1" / "job1.summary.md"),
    })

    hits = fts_index.search_jobs_meta("强化学习")
    assert "job1" in hits
    assert set(hits["job1"]["kinds"]) == {"transcript", "summary"}
    assert "<mark>" in hits["job1"]["snippet"]

    # 无关词不命中；元数据（标题）命中兜底可用
    assert fts_index.search_jobs_meta("量子纠缠") == {}
    meta_hits = fts_index.search_jobs_meta("一期访谈")
    assert "job1" in meta_hits


def test_index_rebuild_is_idempotent(fts_db, tmp_path):
    """先删后插：重复索引/内容变更不产生重复行或旧词残留。"""
    paths = _write_artifacts(tmp_path, "job1", "旧关键词内容", "# s")
    fts_index.index_job_artifacts("job1", paths)
    (Path(paths["transcript"])).write_text("新的内容关于 telescope", encoding="utf-8")
    fts_index.index_job_artifacts("job1", paths)

    assert fts_index.search_jobs_meta("旧关键词") == {}
    assert "job1" in fts_index.search_jobs_meta("telescope")


def test_remove_job_from_index(fts_db, tmp_path):
    paths = _write_artifacts(tmp_path, "job1", "可检索的文本", "# s")
    fts_index.index_job_artifacts("job1", paths)
    fts_index.remove_job_from_index("job1")
    assert fts_index.search_jobs_meta("可检索的文本") == {}


def test_index_record_only_job_is_safe(fts_db):
    """记录-only 任务（无产物/文件已清理）：索引空集不报错、搜不到正文。"""
    _insert_job("job2", "无产物任务", None)
    fts_index.index_job_artifacts("job2", None)
    assert fts_index.search_jobs_meta("无产物任务")  # 元数据（标题）仍可命中


def test_index_truncates_oversized_artifact(fts_db, tmp_path, monkeypatch):
    """超长产物截断索引（默认 2MB，测试 monkeypatch 到 100 字符）。"""
    monkeypatch.setattr(fts_index, "_MAX_INDEX_CHARS", 100)
    tail_marker = "尾部标记词"
    text = "a" * 200 + tail_marker
    paths = _write_artifacts(tmp_path, "job1", text, "# s")
    fts_index.index_job_artifacts("job1", paths)
    assert fts_index.search_jobs_meta(tail_marker) == {}
    assert fts_index.search_jobs_meta("aaa")  # 前缀在索引内


def test_backfill_missing_index(fts_db, tmp_path):
    """启动补漏：只为无索引行的 completed 任务建索引，幂等。"""
    _insert_job("job1", "已索引", _write_artifacts(tmp_path, "job1", "第一份内容", "# s"))
    _insert_job("job2", "未索引", _write_artifacts(tmp_path, "job2", "第二份内容", "# s"))
    fts_index.index_job_artifacts("job1", {
        "transcript": str(tmp_path / "output" / "job1" / "job1.txt"),
        "summary": str(tmp_path / "output" / "job1" / "job1.summary.md"),
    })

    count = fts_index.backfill_missing_index()
    assert count == 1
    assert "job2" in fts_index.search_jobs_meta("第二份内容")
    # 幂等：再跑一遍无补建
    assert fts_index.backfill_missing_index() == 0


def test_search_degrades_when_fts_unavailable(fts_db, tmp_path, monkeypatch):
    """fts 表不可用（极端裁剪 sqlite）：守卫生效时检索返回空 dict 不抛错。

    先建立真实索引行再关守卫——若守卫被误删，查询真实表会命中、测试变红
    （对降级守卫有判别力）。"""
    paths = _write_artifacts(tmp_path, "job1", "可检索的降级关键词", "# s")
    fts_index.index_job_artifacts("job1", paths)
    assert fts_index.search_jobs_meta("降级关键词")
    monkeypatch.setattr(fts_index, "fts_available", lambda: False)
    assert fts_index.search_jobs_meta("降级关键词") == {}
    # 索引入口在守卫关闭时同样静默跳过（不抛错）
    fts_index.index_job_artifacts("job1", paths)


def test_search_caps_hit_limit_in_match_path(fts_db, tmp_path):
    """MATCH 路径命中数超 limit 时截断（防 IN 子句击穿 sqlite 变量数上限）。"""
    for i in range(5):
        paths = _write_artifacts(tmp_path, f"cap{i}", f"共同命中词 number{i}", "# s")
        fts_index.index_job_artifacts(f"cap{i}", paths)
    hits = fts_index.search_jobs_meta("共同命中词", limit=2)
    assert len(hits) == 2


def test_search_empty_and_whitespace_query(fts_db, tmp_path):
    paths = _write_artifacts(tmp_path, "job1", "内容", "# s")
    fts_index.index_job_artifacts("job1", paths)
    assert fts_index.search_jobs_meta("") == {}
    assert fts_index.search_jobs_meta("   ") == {}


# ---------------------------------------------------------------- 一致性矩阵（P1-10）
# 约定：DB = 任务元数据 / filesystem = 产物事实源 / FTS = 派生索引（可随时重建）。
# 以下用例锁定三者间失败组合的可观测行为与可修复性。

def test_completed_job_with_missing_summary_file(fts_db, tmp_path):
    """DB=completed 但 summary 文件缺失：检索不崩（该类跳过）、不产出错误命中。"""
    result_paths = _write_artifacts(tmp_path, "job-missing", "转写正文在这里", "总结正文")
    (Path(result_paths["summary"])).unlink()  # 文件系统侧丢失

    fts_index.index_job_artifacts("job-missing", result_paths)
    hits = fts_index.search_jobs_meta("总结正文")
    assert "job-missing" not in hits, "缺失文件的 kind 不得产生索引行"
    hits2 = fts_index.search_jobs_meta("转写正文")
    assert "job-missing" in hits2, "仍在盘的转写文本照常可检索"


def test_startup_backfill_recovers_missing_index(fts_db, tmp_path):
    """FTS 行缺失（索引失败残留 / v8 升级存量）：启动 backfill 从盘上产物恢复索引。"""
    result_paths = _write_artifacts(tmp_path, "job-backfill", "恢复测试转写", "恢复测试总结")
    _insert_job("job-backfill", "回填任务", result_paths)
    # 模拟索引丢失：无任何 jobs_fts 行
    assert fts_index.search_jobs_meta("恢复测试转写") == {}

    assert fts_index.backfill_missing_index() == 1
    hits = fts_index.search_jobs_meta("恢复测试转写")
    assert "job-backfill" in hits


def test_backfill_skips_job_whose_artifacts_gone(fts_db, tmp_path):
    """DB=completed 但产物文件全部缺失：backfill 安全跳过（不建空行、不崩）。

    返回值语义是「处理的无索引任务数」（含产物缺失者），因此只断言不产生索引行。"""
    _insert_job("job-gone", "无产物任务", {"transcript": "/nonexistent/x.txt", "summary": "/nonexistent/x.md"})
    fts_index.backfill_missing_index()
    with store_db.get_conn() as conn:
        rows = conn.execute("SELECT COUNT(*) AS c FROM jobs_fts WHERE job_id = 'job-gone'").fetchone()
    assert rows["c"] == 0, "产物缺失的任务不得产生空索引行"


def test_retry_reindex_replaces_old_content(fts_db, tmp_path):
    """completed → retry → completed：旧 summary 文本不得残留在 FTS（先删后插）。"""
    result_paths = _write_artifacts(tmp_path, "job-retry-fts", "重试转写第一版", "重试总结第一版")
    _insert_job("job-retry-fts", "重试任务", result_paths)
    fts_index.index_job_artifacts("job-retry-fts", result_paths)
    assert "job-retry-fts" in fts_index.search_jobs_meta("重试总结第一版")

    # retry：清空产物目录 → 新产物（不同内容）→ 完成时重新索引
    from pathlib import Path

    import shutil

    shutil.rmtree(tmp_path / "output" / "job-retry-fts")
    new_paths = _write_artifacts(tmp_path, "job-retry-fts", "重试转写第二版", "重试总结第二版")
    fts_index.index_job_artifacts("job-retry-fts", new_paths)

    assert "job-retry-fts" not in fts_index.search_jobs_meta("第一版"), "旧 summary 不得残留索引"
    assert "job-retry-fts" in fts_index.search_jobs_meta("第二版")


def test_delete_job_removes_fts_rows(fts_db, tmp_path):
    """删除任务：DB 行与 FTS 行同步消失（检索不再命中）。"""
    result_paths = _write_artifacts(tmp_path, "job-del-fts", "删除测试转写", "删除测试总结")
    _insert_job("job-del-fts", "删除任务", result_paths)
    fts_index.index_job_artifacts("job-del-fts", result_paths)
    assert "job-del-fts" in fts_index.search_jobs_meta("删除测试总结")

    fts_index.remove_job_from_index("job-del-fts")
    assert "job-del-fts" not in fts_index.search_jobs_meta("删除测试总结")

    # delete_job 的完整序列还会删 jobs 行：元数据命中（标题 LIKE）随之消失
    with store_db.get_conn() as conn:
        conn.execute("DELETE FROM jobs WHERE job_id = 'job-del-fts'")
    assert "job-del-fts" not in fts_index.search_jobs_meta("删除任务"), "元数据命中必须随 DB 行删除"
