"""label_store 单元测试：标签系统核心存取逻辑（独立表、未分类虚拟化、级联）。"""

import pytest

from video_to_summary import db as store_db
from video_to_summary.constants import UNCATEGORIZED_LABEL
from video_to_summary.web import label_store
from video_to_summary.web.label_store import (
    LabelStoreError,
    create_label,
    delete_label,
    get_job_labels,
    list_labels,
    merge_labels,
    normalize_label_names,
    rename_label,
    set_job_labels,
)
from video_to_summary.web.tasks import create_job


@pytest.fixture(autouse=True)
def label_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store_db, "DATABASE_PATH", tmp_path / "app.db")
    monkeypatch.setattr(store_db, "LEGACY_LLM_FILE", tmp_path / "no1.json")
    monkeypatch.setattr(store_db, "LEGACY_TEMPLATE_FILE", tmp_path / "no2.json")
    store_db.reset()
    store_db.init_db()


def _job(payload=None):
    return create_job(payload or {"source_type": "url", "url": "https://example.test/x", "title": "t"})


def test_normalize_label_names():
    assert normalize_label_names([" 投资 ", "投资", "技术", "技术"]) == ["投资", "技术"]
    assert normalize_label_names(None) == []
    assert normalize_label_names(["", "  "]) == []
    with pytest.raises(LabelStoreError):
        normalize_label_names([UNCATEGORIZED_LABEL])
    with pytest.raises(LabelStoreError):
        normalize_label_names(["x" * (label_store.MAX_LABEL_LEN + 1)])
    with pytest.raises(LabelStoreError):
        normalize_label_names([str(i) for i in range(label_store.MAX_LABELS_PER_JOB + 1)])


def test_set_get_job_labels_roundtrip_and_replace():
    job = _job()
    assert get_job_labels(job.job_id) == []

    result = set_job_labels(job.job_id, ["投资", "A股"])
    assert result == ["投资", "A股"]
    assert get_job_labels(job.job_id) == ["A股", "投资"]  # 按 name 排序

    # 替换
    set_job_labels(job.job_id, ["基金"])
    assert get_job_labels(job.job_id) == ["基金"]
    # 清空
    set_job_labels(job.job_id, [])
    assert get_job_labels(job.job_id) == []


def test_get_or_create_auto_creates_shared_label():
    j1, j2 = _job(), _job()
    set_job_labels(j1.job_id, ["新标签"])
    set_job_labels(j2.job_id, ["新标签"])
    data = list_labels()
    row = next(l for l in data["labels"] if l["name"] == "新标签")
    assert row["count"] == 2  # 两个任务共用同一 label 行


def test_list_labels_counts_and_uncategorized():
    j1, j2, j3 = _job(), _job(), _job()
    set_job_labels(j1.job_id, ["A", "B"])
    set_job_labels(j2.job_id, ["A"])
    data = list_labels()
    by_name = {l["name"]: l for l in data["labels"]}
    assert by_name["A"]["count"] == 2
    assert by_name["B"]["count"] == 1
    assert data["uncategorized_count"] == 1  # j3


def test_create_rename_delete_label():
    created = create_label("新标签")
    with pytest.raises(LabelStoreError):
        create_label("新标签")  # 重名
    with pytest.raises(LabelStoreError):
        create_label(UNCATEGORIZED_LABEL)  # 保留名
    with pytest.raises(LabelStoreError):
        create_label("")  # 空

    renamed = rename_label(created["id"], "改名后")
    assert renamed["name"] == "改名后"
    create_label("撞名")
    with pytest.raises(LabelStoreError):
        rename_label(created["id"], "撞名")
    with pytest.raises(KeyError):
        rename_label(99999, "x")
    with pytest.raises(KeyError):
        delete_label(99999)

    delete_label(created["id"])
    assert not any(l["name"] == "改名后" for l in list_labels()["labels"])


def test_delete_label_unbinds_jobs():
    job = _job()
    set_job_labels(job.job_id, ["临时"])
    label_id = next(l["id"] for l in list_labels()["labels"] if l["name"] == "临时")
    delete_label(label_id)
    assert get_job_labels(job.job_id) == []  # 级联解绑 → 未分类
    assert list_labels()["uncategorized_count"] >= 1


def test_merge_labels_migrates_associations():
    j1, j2 = _job(), _job()
    set_job_labels(j1.job_id, ["旧名"])
    set_job_labels(j2.job_id, ["新名"])
    old_id = next(l["id"] for l in list_labels()["labels"] if l["name"] == "旧名")
    new_id = next(l["id"] for l in list_labels()["labels"] if l["name"] == "新名")

    merge_labels(old_id, new_id)
    assert get_job_labels(j1.job_id) == ["新名"]
    assert get_job_labels(j2.job_id) == ["新名"]
    assert not any(l["name"] == "旧名" for l in list_labels()["labels"])  # 源标签消失

    with pytest.raises(LabelStoreError):
        merge_labels(new_id, new_id)  # 合并自身
    with pytest.raises(KeyError):
        merge_labels(99999, new_id)


def test_delete_job_cascade_removes_associations():
    job = _job()
    set_job_labels(job.job_id, ["系列"])
    # 直接删 job 行验证 FK 级联（delete_job 接口要求终态，属另一层的测试）
    with store_db.get_conn() as conn:
        conn.execute("DELETE FROM jobs WHERE job_id = ?", (job.job_id,))
    # 关联已被 FK 级联清理，但标签本身（0 使用）保留
    data = list_labels()
    assert data["labels"] and any(l["name"] == "系列" and l["count"] == 0 for l in data["labels"])
