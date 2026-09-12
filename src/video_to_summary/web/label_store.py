"""历史任务标签系统：labels / job_labels 关联表的存取层。

设计要点（见 docs/task-labels-plan.md 决策 1/2/3）：
- 独立表而非 jobs.payload JSON：重命名/合并/级联删除都走 SQL；标签与处理配置
  无关，绝不写入 payload（retry 用全局默认重建 payload，挂 payload 会丢标签）
- 「未分类」为虚拟概念（`constants.UNCATEGORIZED_LABEL`）不落库：无任何标签
  关联的任务即未分类；同名真实标签创建被拒绝
- 任务侧按名字操作（自动 get-or-create，可自由输入新标签）；管理页按 id 操作
  （重命名/删除/合并只作用于已入库标签）
"""

from datetime import datetime
from typing import Dict, List, Optional

from .. import db
from ..constants import UNCATEGORIZED_LABEL

MAX_LABEL_LEN = 24
MAX_LABELS_PER_JOB = 8


class LabelStoreError(ValueError):
    """标签业务校验失败（重名/空/超长/超量/保留名/相同 id 合并等）。"""


def normalize_label_names(names) -> List[str]:
    """strip、去空、去重（保序）；超长/超量/含保留名抛 LabelStoreError。"""
    if names is None:
        return []
    out: List[str] = []
    seen = set()
    for raw in names:
        name = str(raw).strip()
        if not name:
            continue
        if name == UNCATEGORIZED_LABEL:
            raise LabelStoreError(f"「{UNCATEGORIZED_LABEL}」为保留名，不能作为标签")
        if len(name) > MAX_LABEL_LEN:
            raise LabelStoreError(f"标签「{name}」过长（>{MAX_LABEL_LEN} 字符）")
        if name not in seen:
            seen.add(name)
            out.append(name)
    if len(out) > MAX_LABELS_PER_JOB:
        raise LabelStoreError(f"每个任务最多 {MAX_LABELS_PER_JOB} 个标签")
    return out


def _now() -> float:
    return datetime.now().timestamp()


def _get_or_create_label_id(conn, name: str) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO labels (name, created_at) VALUES (?, ?)",
        (name, _now()),
    )
    row = conn.execute("SELECT id FROM labels WHERE name = ?", (name,)).fetchone()
    return row["id"]


# ---------------------------------------------------------------- 任务侧

def set_job_labels(job_id: str, names) -> List[str]:
    """设置任务的标签集合（校验 → get-or-create → 单事务 DELETE+INSERT）。

    返回归一化后的标签名列表（与最终入库一致）。
    """
    db.init_db()
    normalized = normalize_label_names(names)
    with db.get_conn() as conn:
        label_ids = [_get_or_create_label_id(conn, name) for name in normalized]
        conn.execute("DELETE FROM job_labels WHERE job_id = ?", (job_id,))
        conn.executemany(
            "INSERT OR IGNORE INTO job_labels (job_id, label_id) VALUES (?, ?)",
            [(job_id, lid) for lid in label_ids],
        )
    return normalized


def get_job_labels(job_id: str) -> List[str]:
    db.init_db()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT l.name FROM job_labels jl JOIN labels l ON jl.label_id = l.id "
            "WHERE jl.job_id = ? ORDER BY l.name",
            (job_id,),
        ).fetchall()
    return [r["name"] for r in rows]


def all_job_labels() -> Dict[str, List[str]]:
    """全量 JOIN 后 Python 分组 {job_id: [names]}（按 name 排序）。"""
    db.init_db()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT jl.job_id AS job_id, l.name AS name "
            "FROM job_labels jl JOIN labels l ON jl.label_id = l.id "
            "ORDER BY l.name"
        ).fetchall()
    grouped: Dict[str, List[str]] = {}
    for r in rows:
        grouped.setdefault(r["job_id"], []).append(r["name"])
    return grouped


# ---------------------------------------------------------------- 管理侧

def list_labels() -> dict:
    """全部标签（含使用计数）与未分类任务数。"""
    db.init_db()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT l.id, l.name, COUNT(jl.job_id) AS count "
            "FROM labels l LEFT JOIN job_labels jl ON jl.label_id = l.id "
            "GROUP BY l.id ORDER BY count DESC, l.name ASC"
        ).fetchall()
        uncategorized = conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE job_id NOT IN (SELECT DISTINCT job_id FROM job_labels)"
        ).fetchone()["c"]
    return {
        "labels": [{"id": r["id"], "name": r["name"], "count": r["count"]} for r in rows],
        "uncategorized_count": uncategorized,
    }


def create_label(name: str) -> dict:
    """新建标签；重名/空/超长/保留名 -> LabelStoreError。"""
    db.init_db()
    normalized = normalize_label_names([name])
    if not normalized:
        raise LabelStoreError("标签名不能为空")
    name = normalized[0]
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO labels (name, created_at) VALUES (?, ?)",
            (name, _now()),
        )
        if cur.rowcount == 0:
            raise LabelStoreError(f"标签「{name}」已存在")
        label_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    return {"id": label_id, "name": name, "count": 0}


def rename_label(label_id: int, name: str) -> dict:
    """重命名；撞名 -> LabelStoreError；不存在 -> KeyError。"""
    db.init_db()
    normalized = normalize_label_names([name])
    if not normalized:
        raise LabelStoreError("标签名不能为空")
    name = normalized[0]
    with db.get_conn() as conn:
        target = conn.execute("SELECT id FROM labels WHERE id = ?", (label_id,)).fetchone()
        if target is None:
            raise KeyError(f"label not found: {label_id}")
        clash = conn.execute("SELECT id FROM labels WHERE name = ? AND id != ?", (name, label_id)).fetchone()
        if clash is not None:
            raise LabelStoreError(f"标签「{name}」已存在")
        conn.execute("UPDATE labels SET name = ? WHERE id = ?", (name, label_id))
    return {"id": label_id, "name": name}


def delete_label(label_id: int) -> None:
    """删除标签；不存在 -> KeyError；关联由 FK 级联解绑。"""
    db.init_db()
    with db.get_conn() as conn:
        cur = conn.execute("DELETE FROM labels WHERE id = ?", (label_id,))
    if cur.rowcount == 0:
        raise KeyError(f"label not found: {label_id}")


def merge_labels(source_id: int, target_id: int) -> None:
    """把 source 的所有关联迁移到 target 并删除 source；同 id -> LabelStoreError。"""
    db.init_db()
    if source_id == target_id:
        raise LabelStoreError("不能合并到自身")
    with db.get_conn() as conn:
        for lid in (source_id, target_id):
            if conn.execute("SELECT id FROM labels WHERE id = ?", (lid,)).fetchone() is None:
                raise KeyError(f"label not found: {lid}")
        conn.execute(
            "UPDATE OR IGNORE job_labels SET label_id = ? WHERE label_id = ?",
            (target_id, source_id),
        )
        conn.execute("DELETE FROM labels WHERE id = ?", (source_id,))


__all__ = [
    "MAX_LABEL_LEN",
    "MAX_LABELS_PER_JOB",
    "LabelStoreError",
    "normalize_label_names",
    "set_job_labels",
    "get_job_labels",
    "all_job_labels",
    "list_labels",
    "create_label",
    "rename_label",
    "delete_label",
    "merge_labels",
]
