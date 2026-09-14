"""Web 后台任务：Job 模型、SQLite 持久化、有界并发、启动恢复与取消。

改进点（对应设计评审问题）：
- 有界并发：``VIDEO_TO_SUMMARY_MAX_CONCURRENT`` 控制同时运行任务数（信号量）。
- 启动恢复：``resume_pending_jobs()`` 在 Web 启动时把 DB 中未完成任务重新入队。
- 取消：``cancel_job()`` 支持 pending 直接终止、running 在阶段边界中止。
- 内存缓存有界：任务进入终态后从 ``_jobs`` 移除（DB 已持久化），避免无界增长。
- LLM 配置批量解析：``resolve_llm_kwargs_map`` 一次读取 LLM 槽位，避免重复查询。
- 资源释放：polisher 持有的 OpenAI 客户端在任务结束时显式 close。
"""

import asyncio
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional

from video_to_summary.config import SubtitleConfig
from video_to_summary.constants import JobEvent, JobStatus, SourceType
from video_to_summary.pipeline import run
from video_to_summary.sources.local import LocalAudioSource
from video_to_summary.sources.url import URLAudioSource
from video_to_summary.utils import infer_source_platform
from video_to_summary.web.llm_store import resolve_llm_kwargs_map

from .. import db
from . import hooks

logger = logging.getLogger("video_to_summary.tasks")


class JobCancelledError(BaseException):
    """任务被中止（用户停止或超时）时抛出，由 run_job 捕获并标记为停止/失败。

    继承 BaseException 而非 Exception，避免被 pipeline 业务层的
    ``except Exception`` 容错 catch 吞掉（如字幕提取失败回退音频）。

    ``user_cancelled`` 区分来源：用户手动停止 → 终态 cancelled；超时 → 终态 failed。
    """

    def __init__(self, job_id: str, reason: str = "cancelled by user", *, user_cancelled: bool = True) -> None:
        super().__init__(f"job {job_id} {reason}")
        self.job_id = job_id
        self.reason = reason
        self.user_cancelled = user_cancelled


def _job_timeout_seconds() -> float:
    """单任务超时（秒）；0 或未设置为不限制。阶段边界检查，阻塞调用内部无法中断。"""
    try:
        return max(0.0, float(os.environ.get("VIDEO_TO_SUMMARY_JOB_TIMEOUT", "0")))
    except (TypeError, ValueError):
        return 0.0


class JobProgress:
    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []

    def push(self, event: str, payload: dict | None = None) -> None:
        self._events.append({"event": event, "payload": payload or {}})

    def to_list(self) -> list[dict[str, Any]]:
        return list(self._events)


def _build_polisher(settings, cfg: dict) -> Any:
    # 需要 api_key：默认 profile 的 Key 留空，未填 Key 时优雅跳过而非报错
    if not getattr(settings, "polish_transcript", False) or not cfg.get("api_key"):
        return None
    from video_to_summary.polishers.llm import LLMTranscriptPolisher

    return LLMTranscriptPolisher(
        api_key=cfg.get("api_key", ""),
        model=cfg.get("model") or getattr(settings, "polish_model", None) or "",
        base_url=cfg.get("base_url"),
        prompt_preset=getattr(settings, "polish_preset", None) or "default",
    )


def _resolve_summary_template(name: str) -> dict:
    """把模板名解析为模板对象：DB 自定义 > 内置 > 回落 default。

    历史上这里把名字直接传给 OpenAISummarizer（只查内置表），导致自定义模板
    从未真正生效——现在统一经 template_store 解析为 {"prompt": ...}。"""
    from video_to_summary.summarizers.openai import get_summary_template
    from . import template_store

    try:
        return template_store.get_template(name)
    except Exception:  # noqa: BLE001 - 名字不存在/解析失败时回落通用，任务不因此失败
        logger.warning("summary template %r not found, falling back to default", name)
        return get_summary_template("通用")


def _validate_base_url(purpose: str, base_url: str | None) -> str | None:
    """任务期防御：LLM 接入地址必须带 http(s) 协议。

    非法地址（历史坏值/手滑写成的裸域名）若放行，openai SDK 只会抛出难定位的
    「URL 缺协议」连接错误；这里显式失败，文案可直接行动。
    """
    if base_url and not base_url.strip().lower().startswith(("http://", "https://")):
        raise RuntimeError(
            f"LLM 接入地址无效（{purpose} 用途的 base_url 未以 http:// 或 https:// 开头）："
            "请到「设置 → LLM 配置」修正后重试。"
        )
    return base_url


def _build_summarizer(settings, cfg: dict) -> Any:
    # 需要 api_key：默认 profile 的 Key 留空，未填 Key 时优雅降级为「未生成摘要」
    if not cfg.get("api_key"):
        return None
    from video_to_summary.summarizers.openai import OpenAISummarizer

    return OpenAISummarizer(
        api_key=cfg.get("api_key", ""),
        model=cfg.get("model") or settings.summary_model or "",
        base_url=_validate_base_url("summary", cfg.get("base_url")),
        template=_resolve_summary_template(settings.summary_template),
    )


def _build_transcriber(settings, asr_cfg: dict) -> Any:
    """构造转写器：开源版唯一引擎 = OpenAI 兼容 Whisper API（字幕优先已在上游兜住常见情况）。

    未配 Key 时仍构造实例：任务会在真正需要转写时才失败（有字幕的视频完全不需要它）。
    """
    from video_to_summary.config import DEFAULT_ASR_API_MODEL
    from video_to_summary.transcribers.openai_whisper_api import OpenAIWhisperAPITranscriber

    return OpenAIWhisperAPITranscriber(
        api_key=asr_cfg.get("api_key", "") or settings.asr_key or "",
        model=asr_cfg.get("model") or settings.asr_model or DEFAULT_ASR_API_MODEL,
        base_url=_validate_base_url("asr", asr_cfg.get("base_url") or settings.summary_base_url),
    )


def _job_title(payload: dict) -> str:
    title = (payload.get("title") or "").strip()
    if title:
        return title
    url = (payload.get("url") or "").strip()
    if url:
        return url
    audio = (payload.get("audio_path") or "").strip()
    if audio:
        return os.path.basename(audio)
    return ""


def output_base() -> str:
    """任务输出根目录：默认相对 cwd 的 ``output/``；桌面版通过
    ``VIDEO_TO_SUMMARY_OUTPUT_DIR`` 指向平台用户数据目录。"""
    return os.environ.get("VIDEO_TO_SUMMARY_OUTPUT_DIR", "output")


class Job:
    def __init__(self, job_id: str, payload: dict) -> None:
        self.job_id = job_id
        self.payload = payload
        self.status: str = JobStatus.PENDING
        self.progress = JobProgress()
        self.result_paths: dict[str, str] | None = None
        self.error: str | None = None
        self.created_at: float = time.time()
        self.title: str = _job_title(payload)
        self.source: str = infer_source_platform(payload.get("url") or "")
        self.retried_at: float | None = None
        self.retry_count: int = 0
        # 总结被用户编辑过的时间（NULL=未编辑）；编辑覆盖写回 summary 产物文件
        self.summary_edited_at: float | None = None

    def mark_running(self) -> None:
        self.status = JobStatus.RUNNING
        self.save()

    def mark_completed(self, result_paths: dict[str, str], elapsed: float | None = None) -> None:
        """任务完成终态：状态与终态事件**同一次落库**原子持久化。

        elapsed 可选（旧调用点不传时仍只推 FINALIZED，行为不变）；run_job 传入
        elapsed 后 FINALIZED → COMPLETED 依次 push、再一次性 save——消除旧实现
        「先落库 status=completed、后补 COMPLETED 事件二次落库」之间可被轮询
        读到的窗口（状态已终态但事件链缺 completed）。
        """
        self.status = JobStatus.COMPLETED
        self.result_paths = result_paths
        self.progress.push(JobEvent.FINALIZED, {"result_paths": result_paths})
        if elapsed is not None:
            self.progress.push(JobEvent.COMPLETED, {"elapsed": elapsed})
        self.save()
        # 全文检索索引（v8）：产物派生文本入 jobs_fts（先删后插，重试重跑安全）。
        # best-effort：索引失败只记日志，绝不影响任务完成链路
        from .fts_index import index_job_artifacts

        index_job_artifacts(self.job_id, result_paths)

    def mark_cancelled(self, reason: str) -> None:
        """用户手动停止的终态：与 failed 区分，前端展示「已停止」而非「失败」。"""
        self.status = JobStatus.CANCELLED
        self.error = reason
        self.progress.push(JobEvent.CANCELLED, {"reason": reason})
        self.save()

    def mark_failed(self, error: str) -> None:
        self.status = JobStatus.FAILED
        self.error = error
        self.progress.push(JobEvent.ERROR, {"error": error})
        self.save()

    def save(self) -> None:
        """把当前状态落库到 jobs 表（Upsert）。"""
        db.init_db()
        with db.get_conn() as conn:
            conn.execute(
                """INSERT INTO jobs
                       (job_id, status, title, source_type, source, retried_at, retry_count,
                        payload, result_paths, error, progress, created_at, updated_at,
                        summary_edited_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(job_id) DO UPDATE SET
                       status = excluded.status,
                       title = excluded.title,
                       source_type = excluded.source_type,
                       source = excluded.source,
                       retried_at = excluded.retried_at,
                       retry_count = excluded.retry_count,
                       payload = excluded.payload,
                       result_paths = excluded.result_paths,
                       error = excluded.error,
                       progress = excluded.progress,
                       updated_at = excluded.updated_at,
                       summary_edited_at = excluded.summary_edited_at""",
                (
                    self.job_id,
                    self.status,
                    self.title,
                    self.payload.get("source_type", SourceType.URL),
                    self.source,
                    self.retried_at,
                    self.retry_count,
                    json.dumps(self.payload, ensure_ascii=False),
                    json.dumps(self.result_paths, ensure_ascii=False) if self.result_paths else None,
                    self.error,
                    json.dumps(self.progress.to_list(), ensure_ascii=False),
                    self.created_at,
                    time.time(),
                    self.summary_edited_at,
                ),
            )

    @classmethod
    def from_db(cls, row) -> "Job":
        job = cls.__new__(cls)
        job.job_id = row["job_id"]
        job.payload = json.loads(row["payload"] or "{}")
        job.status = row["status"]
        job.result_paths = json.loads(row["result_paths"]) if row["result_paths"] else None
        job.error = row["error"]
        job.created_at = row["created_at"]
        job.title = row["title"] or _job_title(job.payload)
        job.source = row["source"] if "source" in row.keys() and row["source"] else infer_source_platform(job.payload.get("url") or "")
        job.retried_at = row["retried_at"] if "retried_at" in row.keys() else None
        job.retry_count = int(row["retry_count"] or 0) if "retry_count" in row.keys() else 0
        job.summary_edited_at = row["summary_edited_at"] if "summary_edited_at" in row.keys() else None
        job.progress = JobProgress()
        for event in json.loads(row["progress"] or "[]"):
            job.progress.push(event.get("event", ""), event.get("payload", {}))
        return job


# 运行中任务的实时内存缓存（终态后移除，DB 为最终事实源）
_jobs: dict[str, Job] = {}

# 取消标志集合：running 任务被请求取消时记录，阶段边界检查中止
_cancel_flags: set[str] = set()

# 并发控制信号量（惰性创建，防止多线程竞争初始化）
_job_semaphore: threading.BoundedSemaphore | None = None
_semaphore_lock = threading.Lock()


def _max_concurrent_jobs() -> int:
    try:
        return max(1, int(os.environ.get("VIDEO_TO_SUMMARY_MAX_CONCURRENT", "2")))
    except (TypeError, ValueError):
        return 2


def _acquire_semaphore() -> threading.BoundedSemaphore:
    global _job_semaphore
    if _job_semaphore is None:
        with _semaphore_lock:
            if _job_semaphore is None:
                _job_semaphore = threading.BoundedSemaphore(_max_concurrent_jobs())
    return _job_semaphore


def get_job(job_id: str) -> Job | None:
    # 运行中的任务优先返回内存实时对象（含实时进度），否则从 DB 恢复
    live = _jobs.get(job_id)
    if live is not None:
        return live
    db.init_db()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    return Job.from_db(row) if row else None


def set_summary_edited(job_id: str, edited_at: float) -> None:
    """记录「总结被用户编辑过」标记（DB + 内存缓存双写，与 get_job 读路径一致）。"""
    db.init_db()
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET summary_edited_at = ?, updated_at = ? WHERE job_id = ?",
            (edited_at, time.time(), job_id),
        )
    job = _jobs.get(job_id)
    if job is not None:
        job.summary_edited_at = edited_at


_STAGE_LABELS = {
    "subtitle": "读取字幕",
    "download": "下载中",
    "transcribe": "转写中",
    "polish": "优化中",
    "summarize": "总结中",
}


def _progress_label(status: str, progress_json: str) -> str:
    """从状态与进度事件计算列表展示的进度状态文案。"""
    if status == JobStatus.COMPLETED:
        return "已完成"
    if status == JobStatus.FAILED:
        return "失败"
    if status == JobStatus.CANCELLED:
        return "已停止"
    if status == JobStatus.PENDING:
        return "等待中"
    try:
        events = json.loads(progress_json or "[]")
    except (TypeError, ValueError):
        events = []
    done = {e.get("event") for e in events if (e.get("event") or "").endswith("_done")}
    for e in events:
        ev = e.get("event") or ""
        if ev.endswith("_start"):
            stage = ev[:-6]
            if f"{stage}_done" not in done:
                return _STAGE_LABELS.get(stage, f"{stage}中")
    return "运行中"


def _all_labels_map() -> dict:
    """{job_id: [labels]}：一次 JOIN 供列表批量带标签，避免逐条查询。"""
    from .label_store import all_job_labels

    return all_job_labels()


def _job_list_where(
    label: Optional[str],
    unlabeled: bool,
    job_ids: Optional[list[str]],
    params: list,
) -> str:
    """list_jobs/count_jobs 共用的 WHERE 组装（条件间 AND，参数顺序追加）。

    job_ids 由检索层传入（fts_index.search_jobs_meta 的命中集合），调用方
    负责控制在 _SEARCH_ID_LIMIT 以内（老 sqlite 变量数上限 999 不能打满）。
    """
    conditions: list[str] = []
    if unlabeled:
        conditions.append("job_id NOT IN (SELECT job_id FROM job_labels)")
    elif label:
        conditions.append("job_id IN (SELECT jl.job_id FROM job_labels jl JOIN labels l ON jl.label_id = l.id WHERE l.name = ?)")
        params.append(label)
    if job_ids is not None:
        if not job_ids:
            conditions.append("0 = 1")  # 空命中集：恒假条件，返回空结果
        else:
            conditions.append(f"job_id IN ({','.join('?' * len(job_ids))})")
            params.extend(job_ids)
    return f" WHERE {' AND '.join(conditions)}" if conditions else ""


def list_jobs(
    limit: int = 50,
    label: Optional[str] = None,
    unlabeled: bool = False,
    offset: int = 0,
    job_ids: Optional[list[str]] = None,
) -> list[dict]:
    # 从 DB 读取（跨重启持久），与内存实时缓存双写一致。
    # label / unlabeled / job_ids 在 SQL 侧过滤，保证 LIMIT 语义正确（先过滤后取前 N）。
    # 稳定排序：created_at 同秒的任务以 job_id 定序，翻页不重不漏。
    db.init_db()
    sql = (
        "SELECT job_id, status, title, source, source_type, payload, created_at, retried_at, retry_count, progress, error "
        "FROM jobs"
    )
    params: list = []
    sql += _job_list_where(label, unlabeled, job_ids, params)
    sql += " ORDER BY created_at DESC, job_id DESC LIMIT ? OFFSET ?"
    params.extend([limit, max(0, offset)])
    with db.get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    entries = []
    labels_map = _all_labels_map()
    for r in rows:
        # payload 中含原始 url / audio_path，解析后供前端展示源链接
        try:
            payload = json.loads(r["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        source_type = r["source_type"] or payload.get("source_type", SourceType.URL)
        entries.append(
            {
                "job_id": r["job_id"],
                "status": r["status"],
                "title": r["title"],
                "created_at": r["created_at"],
                "source": r["source"] or "",
                "source_type": source_type,
                "source_url": payload.get("url") or "",
                "source_path": payload.get("audio_path") or payload.get("file_path") or "",
                "summary_template": payload.get("summary_template") or "",
                "retried_at": r["retried_at"],
                "retry_count": r["retry_count"] or 0,
                "progress": _progress_label(r["status"], r["progress"]),
                "error": r["error"] or "",
                "labels": labels_map.get(r["job_id"], []),
            }
        )
    return entries


def count_jobs(
    label: Optional[str] = None,
    unlabeled: bool = False,
    job_ids: Optional[list[str]] = None,
) -> int:
    """同条件总数（分页响应的 total；SQL 侧过滤与 list_jobs 完全一致）。"""
    db.init_db()
    sql = "SELECT COUNT(*) FROM jobs"
    params: list = []
    sql += _job_list_where(label, unlabeled, job_ids, params)
    with db.get_conn() as conn:
        row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row else 0


def create_job(payload: dict) -> Job:
    from .label_store import normalize_label_names, set_job_labels

    # 标签是组织元数据，绝不写入 payload（retry 用全局默认重建 payload 会丢），
    # 单独落 job_labels 关联表。
    # 先归一化校验（保留名/超量/超长抛 LabelStoreError）再落库：
    # 否则 save 之后才失败会留下从未入队、列表可见的孤儿任务行
    labels = normalize_label_names(payload.pop("labels", None) or [])
    job_id = uuid.uuid4().hex
    job = Job(job_id=job_id, payload=payload)
    job.save()
    set_job_labels(job_id, labels)
    _jobs[job_id] = job
    return job


def enqueue_job(job: Job) -> None:
    """把任务交给事件循环后台执行（需在运行中的 asyncio 循环内调用）。"""
    task = asyncio.create_task(_run_job_task(job.job_id))
    # create_task 返回的 Task 若无强引用可能被 GC（官方文档明确警告）；完成后自动移除
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# 强引用集：防止 fire-and-forget 任务被垃圾回收（见 enqueue_job）
_background_tasks: set = set()

# 任务专用线程池：run_job 会在信号量上阻塞等待并发额度，若跑在默认 executor
# （≈min(32, cpu+4)）上，全量恢复数十个 pending 时会占满全部 worker，
# 饿死 /logs/export、/fs/pick 等其它 to_thread 调用者。容量 = 并发上限，
# 排队等信号量的任务只占本池线程，不影响其余 to_thread。
_job_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def _get_job_executor() -> ThreadPoolExecutor:
    global _job_executor
    if _job_executor is None:
        with _executor_lock:
            if _job_executor is None:
                _job_executor = ThreadPoolExecutor(
                    max_workers=_max_concurrent_jobs(), thread_name_prefix="vts-job"
                )
    return _job_executor


async def _run_job_task(job_id: str) -> None:
    job = get_job(job_id)
    if job is None:
        return
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(_get_job_executor(), run_job, job)


def resume_pending_jobs() -> None:
    """Web 启动时把 DB 中未完成的任务重新入队执行（进程重启后恢复连续性）。

    - **分批全量恢复**：循环取批直到无非终态任务——信号量本身已限制同时运行数，
      一次性 LIMIT 只会让超出部分永久停在 pending（饥饿），直到下次重启；
    - **注册内存缓存**：恢复的 Job 注册进 ``_jobs``，与 create/retry 路径一致，
      运行期进度事件对 ``get_job`` 实时可见（否则详情接口只能看到 DB 快照）。
    """
    db.init_db()

    batch = max(_max_concurrent_jobs() * 2, 20)
    # keyset 复合分页（created_at, job_id）：重入队的任务仍是 pending，
    # 按状态过滤的偏移分页会永远取回同一批行；游标推进保证循环终止
    last_created_at: float | None = None
    last_job_id: str | None = None
    while True:
        with db.get_conn() as conn:
            if last_job_id is None:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE status IN (?, ?) ORDER BY created_at, job_id LIMIT ?",
                    (JobStatus.PENDING, JobStatus.RUNNING, batch),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM jobs
                       WHERE status IN (?, ?)
                         AND (created_at > ? OR (created_at = ? AND job_id > ?))
                       ORDER BY created_at, job_id LIMIT ?""",
                    (JobStatus.PENDING, JobStatus.RUNNING, last_created_at, last_created_at, last_job_id, batch),
                ).fetchall()
        if not rows:
            break
        for row in rows:
            job = Job.from_db(row)
            job.status = JobStatus.PENDING
            job.save()
            _jobs[job.job_id] = job
            enqueue_job(job)
            logger.info("job %s resumed from previous run", job.job_id)
        if len(rows) < batch:
            break
        last_created_at = rows[-1]["created_at"]
        last_job_id = rows[-1]["job_id"]


_H1_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$")


def backfill_titles_from_outputs() -> int:
    """启动时回填历史任务标题：用 ``output/<job_id>/*.summary.md`` 的一级标题
    修正「URL 当标题」的旧数据（早期版本字幕路径没有回写真实标题）。

    只处理「自动派生」标题（空 / URL / 源文件名）的任务，用户显式输入的标题不动；
    产物文件缺失或读取失败时逐任务跳过（fail-safe）。返回回填的任务数。
    """
    db.init_db()
    updated = 0
    try:
        entries = list_jobs(limit=10_000)
    except Exception:  # noqa: BLE001 - 启动回填失败不影响服务
        return 0
    for entry in entries:
        if entry.get("status") != JobStatus.COMPLETED:
            continue
        job = _jobs.get(entry["job_id"]) or get_job(entry["job_id"])
        if job is None or not _is_auto_title(job):
            continue
        summary_md = _first_h1_title(Path(output_base()) / job.job_id)
        if not summary_md or summary_md == job.title:
            continue
        job.title = summary_md
        job.save()
        updated += 1
        logger.info("job %s title backfilled from summary: %r", job.job_id, summary_md)
    if updated:
        logger.info("backfilled %d job title(s) from outputs", updated)
    return updated


def _first_h1_title(job_dir: Path) -> str:
    """读任务产物目录下第一个 summary.md 的一级标题；任何失败返回空串。"""
    try:
        if not job_dir.is_dir():
            return ""
        for md in sorted(job_dir.glob("*.summary.md")):
            for line in md.read_text(encoding="utf-8").splitlines()[:10]:
                m = _H1_TITLE_RE.match(line)
                if m:
                    return m.group(1).strip()
    except Exception:  # noqa: BLE001 - 单任务产物读取失败不中断回填
        return ""
    return ""


def cancel_job(job_id: str) -> bool:
    """请求停止任务：pending 直接终止为 cancelled；running 设置停止标志，在阶段边界中止。

    运行中的任务无法强制中断阻塞调用（下载/转写），因此先记录停止请求并立即反馈给
    前端（CANCEL_REQUESTED 事件），实际在 pipeline 下一个阶段边界抛 JobCancelledError。

    返回 True 表示已受理（pending 已终止 / running 已设停止标志）；
    返回 False 表示任务不存在或已处于终态（completed/failed/cancelled），调用方应据此
    给出正确反馈，避免误以为停止请求已生效。
    """
    job = get_job(job_id)
    if job is None:
        return False
    if job.status == JobStatus.PENDING:
        # 终态事件由 mark_cancelled 统一推送（CANCELLED），此处不重复 push
        job.mark_cancelled("cancelled by user")
        _jobs.pop(job_id, None)
        return True
    if job.status == JobStatus.RUNNING:
        job.progress.push(JobEvent.CANCEL_REQUESTED, {"reason": "stop requested"})
        job.save()  # 取消意图落库：进程重启后 resume 不得把已请求停止的任务再跑一遍
        _cancel_flags.add(job_id)
        logger.info("job %s stop requested", job_id)
        return True
    # 终态任务（completed/failed/cancelled）无需停止，返回 False 让调用方感知
    logger.info("job %s already in terminal status: %s", job_id, job.status)
    return False


def retry_job(job_id: str, summary_template: str | None = None) -> Job:
    """重试任务：用当前全局默认配置重建 payload，清空输出目录后从头执行。

    仅支持终态（failed/completed/cancelled）；运行中任务不可重试，抛 ValueError。
    保留原始源信息（url/audio_path/source_type/title）与单任务选择的总结模板
    （summary_template：「重新生成」应产出与原先一致的内容，历史展示的模板保持准确）；
    调用方显式传入 summary_template 时校验存在性（未知抛 ValueError → 400）并替换模板，
    实现「重试时换模板」。其余配置项全部取自当前全局默认，确保重试任务使用最新配置
    而非创建时的快照。输出目录会被清空，pipeline 从第一阶段重新执行。
    """
    job = get_job(job_id)
    if job is None:
        raise KeyError(f"job not found: {job_id}")
    if job.status not in (JobStatus.FAILED, JobStatus.COMPLETED, JobStatus.CANCELLED):
        raise ValueError(f"job {job_id} is not retryable (status={job.status})")

    # 用当前全局默认配置重建 payload，只保留原始源信息与单任务总结模板
    from .settings_store import job_defaults_payload

    fresh_payload = job_defaults_payload()
    for key in ("source_type", "url", "audio_path", "title", "summary_template"):
        if job.payload.get(key) is not None:
            fresh_payload[key] = job.payload[key]
    if summary_template:
        from video_to_summary.summarizers.openai import TEMPLATE_ALIASES
        from .template_store import list_templates

        # 兼容别名（历史名 "default"）归一化为现名后再校验
        summary_template = TEMPLATE_ALIASES.get(summary_template, summary_template)
        if summary_template not in list_templates():
            raise ValueError(f"unknown summary template: {summary_template}")
        fresh_payload["summary_template"] = summary_template
    job.payload = fresh_payload

    # 清空输出目录，确保 pipeline 不命中旧缓存、从头执行
    job_dir = Path(output_base()) / job_id
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
        logger.info("job %s output dir cleared for retry: %s", job_id, job_dir)

    job.status = JobStatus.PENDING
    job.error = None
    job.result_paths = None
    job.progress = JobProgress()
    job.retry_count += 1
    job.retried_at = time.time()
    # 输出目录已清空重建，编辑稿随之失效——编辑标记必须同步清零
    job.summary_edited_at = None
    job.save()
    _jobs[job_id] = job
    _cancel_flags.discard(job_id)
    enqueue_job(job)
    logger.info("job %s retried #%d (status -> pending, fresh config)", job_id, job.retry_count)
    return job


def delete_job(job_id: str) -> None:
    """删除历史任务：删除 DB 记录 + 清理输出目录 + 移除内存缓存。

    仅支持终态（completed/failed/cancelled）；运行中/等待中任务需先 cancel 再删除，
    避免删掉正在写入的文件导致 pipeline 状态错乱。

    本地源的原始输入文件（audio_path 指向用户自己的文件）不删除，只清理任务产物目录
    ``output/<job_id>/``。删除不存在的任务抛 KeyError。

    删除顺序：先删 DB 记录（成功即视为任务已注销），再清理文件。若文件清理失败，
    DB 中已无记录，残留文件可由定期清理兜底；反之文件已删但 DB 记录残留的「幽灵记录」
    会导致前端列表出现无法加载详情的条目，体验更差。
    """
    job = get_job(job_id)
    if job is None:
        raise KeyError(f"job not found: {job_id}")
    if job.status in (JobStatus.PENDING, JobStatus.RUNNING):
        raise ValueError(f"job {job_id} is running or pending, cancel it first")

    job_dir = Path(output_base()) / job_id

    # 1) 删除 DB 记录（成功即视为任务已注销，后续文件清理失败不影响一致性）
    db.init_db()
    with db.get_conn() as conn:
        conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
    # 全文检索索引同步删除（jobs 表行删后索引行不会级联清理）
    from .fts_index import remove_job_from_index

    remove_job_from_index(job_id)
    logger.info("job %s db record deleted", job_id)

    # 2) 移除内存缓存与取消标志
    _jobs.pop(job_id, None)
    _cancel_flags.discard(job_id)

    # 3) 清理任务输出目录（audio/txt/segments/summary 等产物）
    if job_dir.exists():
        try:
            shutil.rmtree(job_dir)
            logger.info("job %s output dir removed: %s", job_id, job_dir)
        except OSError as exc:
            # 文件清理失败不回滚 DB 删除，记录日志便于事后排查残留
            logger.warning("job %s output dir cleanup failed (db record already deleted): %s", job_id, exc)


def _make_cancel_check(job_id: str, start_mono: float, timeout: float) -> Callable[[], None]:
    def _check() -> None:
        if job_id in _cancel_flags:
            raise JobCancelledError(job_id, reason="cancelled by user", user_cancelled=True)
        if timeout and (time.monotonic() - start_mono) > timeout:
            raise JobCancelledError(
                job_id, reason=f"timeout after {timeout:.0f}s", user_cancelled=False
            )

    return _check


def _is_auto_title(job: Job) -> bool:
    """任务标题是否为「自动派生」而非用户显式输入：空 / URL 形态 / 等于原始 URL / 等于源文件名。

    自动派生的标题允许被解析到的真实视频标题回写覆盖；用户显式输入的标题保留。
    URL 形态按前缀识别而非仅与 payload.url 全等：旧数据/retry 重建 payload 后
    不一定留有可全等的原始 URL。
    """
    title = (job.title or "").strip()
    if not title:
        return True
    if re.match(r"^https?://", title, re.IGNORECASE):
        return True
    payload = job.payload or {}
    if title == (payload.get("url") or "").strip():
        return True
    audio = (payload.get("audio_path") or "").strip()
    if audio and title == os.path.basename(audio):
        return True
    return False


# 上游 LLM/ASR 认证类错误特征：BYOK 模式下 Key 由用户自备，这类错误必须给出
# 可直接行动的配置指引，而不是把上游报文原样抛给用户
_AUTH_ERROR_RE = re.compile(
    r"(api[ _-]?key|unauthorized|authentication|invalid_api_key|\b401\b)",
    re.IGNORECASE,
)

# 站点风控/限流类错误特征：412 = B 站边缘 WAF 按 UA×IP 信誉对 /video/ HTML 页发起挑战
# （换成非浏览器 UA 即恢复）；429 = 平台限流（YouTube 自动字幕匿名访问被定点限流等）。
# 这类错误必须给出可直接行动的解法（自定义 UA / 登录 cookies / 代理），
# 而不是把上游内部报文原样抛给普通用户。
_HTTP_BLOCK_RE = re.compile(r"HTTP Error (412|429)\b|Too Many Requests", re.IGNORECASE)

# base_url 内嵌凭据（user:pass@host）会随上游异常文本回显，入库/回显前统一打码
_URL_CRED_RE = re.compile(r"//([^/@\s:]+):([^/@\s]+)@")


def _scrub_url_credentials(text: str) -> str:
    return _URL_CRED_RE.sub(r"//\1:***@", text)


def _user_facing_error(exc: Exception) -> str:
    """对外错误文案。

    开源版是 BYOK：Key / 接入地址 / 模型名全在用户手里，因此认证类错误不再是
    「联系维护人员」，而是引导用户去「设置 → LLM 配置」自查（这是默认路径，不是
    边缘场景）。站点风控类错误（412/429）同样给可行动解法（自定义 UA / cookies /
    代理），不暴露上游内部实现。其余错误（链接不可访问、字幕缺失等用户可行动的
    问题）原样保留，完整异常仍只进日志（logger.exception）。
    """
    text = str(exc)
    if _AUTH_ERROR_RE.search(text):
        return (
            "LLM 调用被拒绝（认证失败）：请在「设置 → LLM 配置」检查 API Key、"
            "接入地址（base_url）与模型名是否填写正确并已保存；若使用自建/第三方"
            "兼容端点，请确认其支持所用模型。也可在「设置 → 导出日志」导出诊断日志排查。"
        )
    if _HTTP_BLOCK_RE.search(text):
        return (
            "下载被站点风控/限流拦截（HTTP 412/429）。可按以下任一解法处理后重试："
            "① 自定义 User-Agent——设置环境变量 VTS_USER_AGENT=Wget/1.21.3"
            "（非浏览器 UA，可绕过按 UA 信誉发起的 412 挑战）；"
            "② 提供登录 cookies——设置环境变量 VTS_COOKIES_FILE 指向 cookies.txt"
            "（B 站 CC/AI 字幕同样需要登录态）；"
            "③ 配置代理后重试。"
            "详细步骤见 docker/README.md「B 站 412 风控」与 README.md 自部署 FAQ。"
        )
    return _scrub_url_credentials(text)


def run_job(job: Job) -> None:
    with _acquire_semaphore():
        _run_job_inner(job)


def _run_job_inner(job: Job) -> None:
    # 取消复活守卫：pending 任务可能在信号量排队期间被 cancel（置 cancelled 并移出
    # 内存缓存）。拿到信号量后若状态已终态，直接放弃执行——否则已停止任务会继续
    # 消耗下载/转写/LLM 额度，且终态被 mark_running 覆盖回 running。
    # 注意只拦终态：直接以 running 状态调 run_job 是测试/内部场景的合法用法。
    if job.status in (JobStatus.CANCELLED, JobStatus.FAILED, JobStatus.COMPLETED):
        logger.info("job %s skipped before start (status=%s)", job.job_id, job.status)
        return
    job.mark_running()
    job.progress.push(JobEvent.STARTED)
    logger.info("job %s started (title=%r)", job.job_id, job.title)

    polisher = None
    try:
        output_dir = os.path.join(output_base(), job.job_id)
        os.makedirs(output_dir, exist_ok=True)

        settings = _build_settings(job.payload, output_dir)
        source = _build_source(settings, job)

        cfg = resolve_llm_kwargs_map(
            {
                "summary": {
                    "api_key": settings.summary_key or settings.asr_key or "",
                    "base_url": settings.summary_base_url or "",
                    "model": settings.summary_model or "",
                },
                "asr": {
                    "api_key": settings.asr_key or settings.summary_key or "",
                    "base_url": settings.asr_base_url or settings.summary_base_url or "",
                    "model": settings.asr_model or settings.summary_model or "",
                },
                "polish": {
                    "api_key": settings.summary_key or settings.asr_key or "",
                    "base_url": settings.polish_base_url or "",
                    "model": settings.polish_model or "",
                },
            }
        )

        transcriber = _build_transcriber(settings, cfg["asr"])
        # StepASR 分片进度：转写器每完成一片推一条 transcribe_progress 事件，
        # 长音频（如 85 分钟 → 17 片）前端可见实时推进
        if hasattr(transcriber, "on_progress"):
            def _on_chunk_progress(done: int, total: int, _job=job) -> None:
                _job.progress.push(JobEvent.TRANSCRIBE_PROGRESS, {"done": done, "total": total})
            transcriber.on_progress = _on_chunk_progress
        # 下载实时进度：URL 源经 yt-dlp progress_hook 节流后推 download_progress
        # 事件（源侧已限频，链体积与转写分片同量级；本地文件源无此属性不桥接）
        if hasattr(source, "on_progress"):
            def _on_download_progress(payload: dict, _job=job) -> None:
                _job.progress.push(JobEvent.DOWNLOAD_PROGRESS, payload or {})
            source.on_progress = _on_download_progress
        summarizer = _build_summarizer(settings, cfg["summary"])
        polisher = _build_polisher(settings, cfg["polish"])
        # LLM 阶段因无 Key 被跳过时显式记录事件：前端完成任务后据此提示「仅生成转写，
        # 未生成总结」，避免用户把空摘要误读为成功
        if summarizer is None:
            job.progress.push(JobEvent.SUMMARIZE_SKIPPED, {"reason": "no_api_key"})
        if polisher is None and getattr(settings, "polish_transcript", False):
            job.progress.push(JobEvent.POLISH_SKIPPED, {"reason": "no_api_key"})

        t0 = time.perf_counter()
        start_mono = time.monotonic()
        timeout = _job_timeout_seconds()
        source_meta = getattr(source, "meta", None)
        # 单任务工作量指标：由事件侧捕获，完成后交给插件副作用（开源版无副作用）
        job_metrics: dict = {}

        def _on_event(event: str, payload: dict) -> None:
            job.progress.push(event, payload)
            # 解析到真实视频标题后实时回写（历史/列表展示真实标题）。
            # 下载与字幕两条路径都会携带 title（字幕路径跳过下载、无 download_done）；
            # 仅覆盖「自动派生」的标题（空/URL/文件名），用户显式输入的标题保留
            if event in (JobEvent.DOWNLOAD_DONE, JobEvent.SUBTITLE_DONE) and payload.get("title"):
                new_title = str(payload["title"]).strip()
                if new_title and new_title != job.title and _is_auto_title(job):
                    job.title = new_title
                    job.save()
            # 指标采集：本次真实送 ASR 的音频时长（缓存命中不计）/ 总结输入输出字数
            if event == JobEvent.TRANSCRIBE_DONE and not payload.get("cached"):
                job_metrics["asr_audio_seconds"] = payload.get("audio_seconds")
            elif event == JobEvent.SUMMARIZE_DONE:
                job_metrics["transcript_chars"] = int(payload.get("input_chars") or 0)
                job_metrics["summary_chars"] = int(payload.get("output_chars") or 0)

        summary_path = run(
            source,
            transcriber,
            summarizer,
            settings.output_dir,
            polisher=polisher,
            polisher_title=getattr(source_meta, "title", None) or job.title or job.job_id,
            on_event=_on_event,
            cancel_check=_make_cancel_check(job.job_id, start_mono, timeout),
            subtitle_config=SubtitleConfig(
                preference=getattr(settings, "subtitle_preference", None) or "auto",
                language=getattr(settings, "subtitle_language", None) or "auto",
            ),
        )
        elapsed = time.perf_counter() - t0

        # 会话内标题闭环：事件未携带标题的少数路径（如元数据缺失）完成后用产物 summary
        # 一级标题兜底回填，规则与启动回填 backfill_titles_from_outputs 一致——
        # 仅覆盖「自动派生」标题，用户显式输入的标题不动。mark_completed 会随终态一并落库
        summary_title = _first_h1_title(Path(output_dir))
        if summary_title and summary_title != job.title and _is_auto_title(job):
            job.title = summary_title
            logger.info("job %s title set from summary output: %r", job.job_id, summary_title)

        source_id = getattr(getattr(source, "meta", None), "source_id", None) or job.job_id
        result_paths = {
            "summary": str(summary_path),
            "transcript": os.path.join(output_dir, f"{source_id}.txt"),
            "polished": os.path.join(output_dir, f"{source_id}.polished.txt"),
            "segments": os.path.join(output_dir, f"{source_id}.segments.json"),
            "subtitle": os.path.join(output_dir, f"{source_id}.srt"),
        }
        # 终态与 COMPLETED 事件由 mark_completed 在同一次落库中原子持久化
        # （elapsed 取值时点与此处调用时点一致），无需再单独 push/save
        job.mark_completed(result_paths, elapsed=elapsed)
        # 任务完成副作用（插件挂载点）：开源版无注册者 = 空操作；任何副作用异常
        # 都在 hooks 内被吞掉并记日志，绝不影响任务终态
        hooks.run_job_completed_side_effects(
            job.job_id,
            {
                "asr_audio_seconds": job_metrics.get("asr_audio_seconds"),
                "transcript_chars": int(job_metrics.get("transcript_chars") or 0),
                "summary_chars": int(job_metrics.get("summary_chars") or 0),
            },
        )
        logger.info("job %s completed in %.2fs", job.job_id, elapsed)
    except JobCancelledError as exc:
        # CANCELLED/ERROR 终态事件由 mark_cancelled/mark_failed 统一推送，此处不重复
        if exc.user_cancelled:
            job.mark_cancelled(exc.reason)
            logger.info("job %s stopped: %s", job.job_id, exc.reason)
        else:
            job.mark_failed(exc.reason)
            logger.info("job %s timed out: %s", job.job_id, exc.reason)
    except Exception as exc:  # noqa: BLE001 - 任务边界捕获，转为失败状态
        # 原始错误全文只进日志；普通用户看到的是脱敏后的对外文案（不外露
        # 上游供应商、API Key、内部 URL）。管理员保留完整错误便于排查。
        job.mark_failed(_user_facing_error(exc))
        logger.exception("job %s failed", job.job_id)
    finally:
        if polisher is not None:
            polisher.close()
        _cancel_flags.discard(job.job_id)
        # 身份校验：终态落库后若 retry_job 已放入新 Job 对象，不得误弹新缓存条目
        if _jobs.get(job.job_id) is job:
            _jobs.pop(job.job_id, None)


def _build_settings(payload: dict, output_dir: str) -> Any:
    from video_to_summary.config import Settings

    return Settings.from_mapping({**payload, "output_dir": output_dir})


def _build_source(settings: Any, job: Job) -> Any:
    source_type = job.payload.get("source_type", SourceType.URL)

    if source_type == SourceType.LOCAL:
        audio_path = job.payload.get("audio_path") or job.payload.get("file_path")
        if not audio_path:
            raise ValueError("local source requires audio_path")
        return LocalAudioSource(
            Path(audio_path),
            title=job.payload.get("title"),
        )

    url = job.payload.get("url")
    if not url:
        raise ValueError("url source requires url")
    return URLAudioSource(
        url,
        output_dir=settings.output_dir,
        keep_video=settings.keep_video,
        cookies=settings.cookies,
        cookies_browser=getattr(settings, "cookies_browser", "") or "",
        user_agent=getattr(settings, "user_agent", None),
        proxy=settings.proxy,
        audio_format=settings.audio_format,
    )


__all__ = [
    "create_job",
    "get_job",
    "list_jobs",
    "run_job",
    "enqueue_job",
    "cancel_job",
    "retry_job",
    "delete_job",
    "resume_pending_jobs",
    "output_base",
]
