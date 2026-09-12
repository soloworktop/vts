"""jobs_fts 全文检索索引（schema v8，索引文本 = 产物派生数据）。

「输出产物不入库」约定的**显式例外**：jobs_fts 表存的是从
``output/<job_id>/*.txt``/``*.summary.md`` 提取的索引文本——产物本体仍在
磁盘，索引可随时删除并由本模块重建（``backfill_missing_index``），不属于
产物入库。单文件截断上限防止超长转写撑爆 DB（截断点之后的内容搜不到）。

分词器：迁移侧优先 trigram（中文子串 MATCH，sqlite ≥3.34）。本模块运行时
探测 trigram 可用性：可用走 MATCH 短语查询；不可用（unicode61 回退或表未
建成）走 ``LIKE '%q%'`` 全表扫降级——千级任务量内可接受。任务/产物操作入口：
- 任务完成（``tasks.Job.mark_completed``）→ ``index_job_artifacts``
- 任务删除（``tasks.delete_job``）→ ``remove_job_from_index``
- 应用启动（``app.lifespan``）→ ``backfill_missing_index``（幂等补漏）
"""

import logging
import sqlite3
from pathlib import Path
from typing import Optional

from .. import db

logger = logging.getLogger("video_to_summary.web.fts_index")

# 单文件索引截断上限（字符）：超长转写只索引前缀
_MAX_INDEX_CHARS = 2_000_000

# 检索命中的 job_id 上限：结果集要回 jobs 表 IN (...) 查询，老版本 sqlite
# 的变量数上限（999）不能打满，留出余量
_SEARCH_ID_LIMIT = 900

_trigram_supported: Optional[bool] = None


def trigram_supported() -> bool:
    """sqlite 是否内置 trigram 分词器（内存探测一次，进程内缓存）。"""
    global _trigram_supported
    if _trigram_supported is None:
        try:
            conn = sqlite3.connect(":memory:")
            try:
                conn.execute("CREATE VIRTUAL TABLE temp.fts_probe USING fts5(a, tokenize='trigram')")
                _trigram_supported = True
            finally:
                conn.close()
        except sqlite3.Error:
            _trigram_supported = False
    return _trigram_supported


def fts_available() -> bool:
    """jobs_fts 表是否已建成（v8 迁移在 fts5 完全不可用时 fail-open 跳过）。"""
    try:
        db.init_db()
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='jobs_fts'"
            ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def index_job_artifacts(job_id: str, result_paths: Optional[dict]) -> None:
    """重建单任务的全文索引（先删后插，幂等）。

    只索引 summary/transcript 两类文本产物；文件缺失或为空跳过该类
    （记录-only 任务合法存在，仅搜不到正文）。索引失败只记日志，
    绝不影响任务完成链路。
    """
    if not fts_available():
        return
    rows: list[tuple[str, str]] = []
    for kind, key in (("transcript", "transcript"), ("summary", "summary")):
        path = (result_paths or {}).get(key)
        if not path:
            continue
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            logger.info("fts index: artifact missing for job %s kind=%s", job_id, kind)
            continue
        if not text.strip():
            continue
        rows.append((kind, text[:_MAX_INDEX_CHARS]))
    try:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM jobs_fts WHERE job_id = ?", (job_id,))
            for kind, text in rows:
                conn.execute(
                    "INSERT INTO jobs_fts(text, job_id, kind) VALUES (?,?,?)",
                    (text, job_id, kind),
                )
    except sqlite3.Error:
        logger.warning("fts index: failed to index job %s", job_id, exc_info=True)


def remove_job_from_index(job_id: str) -> None:
    """删除任务的索引行（best-effort，与 delete_job 的 DB 删除解耦）。"""
    if not fts_available():
        return
    try:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM jobs_fts WHERE job_id = ?", (job_id,))
    except sqlite3.Error:
        logger.warning("fts index: failed to remove job %s", job_id, exc_info=True)


def backfill_missing_index() -> int:
    """为缺失索引的 completed 任务补建索引（启动时调用，幂等）。

    判定「缺失」= jobs_fts 中无该 job_id 的行（删除产物文件后重跑会
    先删后插，不依赖此函数）。返回本次补建的任务数。
    """
    if not fts_available():
        return 0
    db.init_db()
    with db.get_conn() as conn:
        completed = conn.execute(
            "SELECT job_id, result_paths FROM jobs WHERE status = ?",
            ("completed",),
        ).fetchall()
        indexed = {
            r["job_id"]
            for r in conn.execute("SELECT DISTINCT job_id FROM jobs_fts").fetchall()
        }
    import json

    count = 0
    for row in completed:
        if row["job_id"] in indexed:
            continue
        try:
            result_paths = json.loads(row["result_paths"]) if row["result_paths"] else None
        except (TypeError, ValueError):
            result_paths = None
        index_job_artifacts(row["job_id"], result_paths)
        count += 1
    if count:
        logger.info("fts index: backfilled %d job(s)", count)
    return count


def search_jobs_meta(query: str, limit: int = _SEARCH_ID_LIMIT) -> dict[str, dict]:
    """全文检索：正文（jobs_fts）+ 元数据（title/source/payload LIKE）合并。

    返回 ``{job_id: {snippet, kinds}}``（保序：FTS rank 优先，元数据命中
    其后按 created_at 排序由列表查询完成）。``snippet`` 仅正文命中时有值
    （含 ``<mark>`` 高亮标记，前端转义后回填）。查询前后空白剥离；正文
    匹配按能力自动在 MATCH 短语与 LIKE 间切换（trigram 对 <3 字符查询
    无子串匹配能力，同样降级 LIKE）。
    """
    q = (query or "").strip()
    if not q or not fts_available():
        return {}
    # LIKE 通配符转义：用户输入中的 %/_ 按字面量匹配（配合 ESCAPE '\\'）
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{escaped}%"
    results: dict[str, dict] = {}
    try:
        with db.get_conn() as conn:
            if trigram_supported() and len(q) >= 3:
                # 短语语法 + 内部引号翻倍：用户输入中的 FTS 操作符（AND/OR/NOT/
                # 邻近语法等）一律按字面量处理，杜绝查询语法注入。
                # snippet() 与 GROUP BY 不兼容（"unable to use function snippet
                # in the requested context"）：先按 rank 取匹配行，Python 侧
                # 聚合去重（每 job 保留 rank 最优一条的片段），片段再按 job 单查
                rows = conn.execute(
                    "SELECT job_id, kind FROM jobs_fts WHERE jobs_fts MATCH ? ORDER BY rank",
                    (phrase := '"' + q.replace('"', '""') + '"',),
                ).fetchall()
                for r in rows:
                    # 容量上限先于插入判断（此前写在 setdefault 之后恒为 False，
                    # 截断失效会击穿 IN 子句的 sqlite 变量数上限）
                    if r["job_id"] not in results and len(results) >= limit:
                        break
                    entry = results.setdefault(r["job_id"], {"snippet": "", "kinds": []})
                    if r["kind"] not in entry["kinds"]:
                        entry["kinds"].append(r["kind"])
                # 截断到 limit 个任务后再取片段（每 job 一条轻量查询）
                for job_id in list(results)[:limit]:
                    snip = conn.execute(
                        "SELECT snippet(jobs_fts, 0, '<mark>', '</mark>', '…', 16)"
                        " FROM jobs_fts WHERE jobs_fts MATCH ? AND job_id = ? ORDER BY rank LIMIT 1",
                        (phrase, job_id),
                    ).fetchone()
                    if snip:
                        results[job_id]["snippet"] = snip[0] or ""
            else:
                rows = conn.execute(
                    "SELECT job_id, group_concat(DISTINCT kind) AS kinds"
                    " FROM jobs_fts WHERE text LIKE ? ESCAPE '\\'"
                    " GROUP BY job_id LIMIT ?",
                    (like, limit),
                ).fetchall()
                for r in rows:
                    results[r["job_id"]] = {
                        "snippet": "",
                        "kinds": [k for k in (r["kinds"] or "").split(",") if k],
                    }
            # 元数据命中（标题/源/payload 内的 URL/路径）并入——兼容「按标题找」
            if len(results) < limit:
                meta_rows = conn.execute(
                    "SELECT job_id FROM jobs WHERE title LIKE ? OR source LIKE ? OR payload LIKE ?"
                    " LIMIT ?",
                    (like, like, like, limit - len(results)),
                ).fetchall()
                for r in meta_rows:
                    results.setdefault(r["job_id"], {"snippet": "", "kinds": []})
    except sqlite3.Error:
        logger.warning("fts search failed for query %r", q, exc_info=True)
        return {}
    return results
