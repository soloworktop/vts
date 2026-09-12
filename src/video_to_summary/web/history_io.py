"""历史任务数据导出/导入（备份与迁移语义，v0.4.0）。

导出包为 zip：
    manifest.json   {format, format_version, schema_version, app_version, exported_at, counts}
    jobs.json       [{"job": {白名单字段}, "labels": [名字]}]
    templates.json  [自定义模板行]（内置模板随版本内置，不导出）
    artifacts/<job_id>/<文件名>   （result_paths 指向且实际存在的产物文件）

**不导出**（并写明理由）：`llm_profiles`/`llm_routing`（Fernet 密文绑定本机
enc_key，不可移植且属敏感凭证）、`meta`（应用级偏好与迁移标记，设备绑定）、
全局任务默认设置。

**只导终态任务**（completed/failed/cancelled）：running/pending 导入后会被
启动恢复机制重新入队执行，属不可接受语义。

**schema 迭代兼容**（本模块的核心设计）：
1. fail-closed：`manifest.schema_version > 本机 SCHEMA_VERSION` 或未知
   `format_version` → 拒绝导入（绝不猜测式导入未知结构）；
2. 旧包向前归一：`IMPORT_JOB_UPGRADES[version]` 逐版本 step 函数（与
   `db.MIGRATIONS` 同哲学，新 schema 加列时在此登记默认值填充）；
3. 白名单写库：导入不裸 INSERT 导出行——按当前版本已知字段列表写入
   （未知键丢弃），列集合与 `tasks.Job.save` 一致；
4. 原子性：全部导入写在单个 sqlite 事务内，任一步失败整体回滚。

**导入规则**：job_id 已存在 → 跳过并计数（重复导入同一包幂等，要重灌先删）；
标签按名字 get-or-create 重映射（labels.id 是库内自增）；模板同名或与内置
冲突 → 跳过；产物成员逐个 resolve 校验防 zip 路径穿越，还原后重写
result_paths 为本机路径（包内缺失的产物保留原路径，前端走 404 优雅降级）。
导入成功后刷新内存 `_jobs` 缓存并复用 `fts_index.index_job_artifacts` 建检索
索引。导出/导入是本机历史数据操作，不做任何门控（历史记录始终可读可迁移）。
"""

import io
import json
import logging
import re
import time
import zipfile
from pathlib import Path
from typing import Any, Optional

from .. import db
from ..constants import JobStatus

logger = logging.getLogger("video_to_summary.web.history_io")

FORMAT_NAME = "vts-history-export"
FORMAT_VERSION = 1

# 导出/导入的任务字段白名单（与 tasks.Job.save 的列集合一致）。
# 旧版本包缺失的列由 _normalize_job 补默认值；未知键一律丢弃。
_JOB_FIELDS = (
    "job_id",
    "status",
    "title",
    "source_type",
    "source",
    "retried_at",
    "retry_count",
    "payload",
    "result_paths",
    "error",
    "progress",
    "created_at",
    "updated_at",
    "summary_edited_at",
)

_TERMINAL_STATUSES = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class HistoryBundleError(ValueError):
    """导出包不合法（格式/版本/结构错误），对应用户可读的 400 文案。"""


# ---------------------------------------------------------------- 导出

def _export_dir() -> Path:
    """产物根目录（与 web 层 output_base 同源；经 tasks 延迟导入避免环）。"""
    from .tasks import output_base

    return Path(output_base())


def collect_export_jobs() -> tuple[list[dict], dict[str, list[str]], int]:
    """收集终态任务 + 各自标签名；返回 (jobs, labels_map, skipped_nonterminal)。"""
    db.init_db()
    placeholders = ",".join("?" * len(_TERMINAL_STATUSES))
    with db.get_conn() as conn:
        rows = conn.execute(
            f"SELECT job_id, status, title, source_type, source, retried_at, retry_count,"
            f" payload, result_paths, error, progress, created_at, updated_at, summary_edited_at"
            f" FROM jobs WHERE status IN ({placeholders})"
            f" ORDER BY created_at DESC, job_id DESC",
            sorted(_TERMINAL_STATUSES),
        ).fetchall()
        skipped = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status NOT IN" f" ({placeholders})",
            sorted(_TERMINAL_STATUSES),
        ).fetchone()["n"]
        label_rows = conn.execute(
            "SELECT jl.job_id, l.name FROM job_labels jl JOIN labels l ON jl.label_id = l.id"
            " ORDER BY l.name"
        ).fetchall()

    labels_map: dict[str, list[str]] = {}
    for r in label_rows:
        labels_map.setdefault(r["job_id"], []).append(r["name"])

    jobs: list[dict] = []
    for r in rows:
        try:
            payload = json.loads(r["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        try:
            result_paths = json.loads(r["result_paths"]) if r["result_paths"] else None
        except (TypeError, ValueError):
            result_paths = None
        try:
            progress = json.loads(r["progress"] or "[]")
        except (TypeError, ValueError):
            progress = []
        jobs.append(
            {
                "job_id": r["job_id"],
                "status": r["status"],
                "title": r["title"] or "",
                "source_type": r["source_type"] or "url",
                "source": r["source"] or "",
                "retried_at": r["retried_at"],
                "retry_count": int(r["retry_count"] or 0),
                "payload": payload,
                "result_paths": result_paths,
                "error": r["error"] or "",
                "progress": progress,
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "summary_edited_at": r["summary_edited_at"] if "summary_edited_at" in r.keys() else None,
                "labels": labels_map.pop(r["job_id"], []),
            }
        )
    return jobs, labels_map, int(skipped)


def _iter_job_artifacts(job: dict, output_root: Path):
    """产出 (归一文件名, 绝对路径)：result_paths 指向且真实存在、且确在
    ``output_root/<job_id>/`` 内的产物。归一文件名 = 产物 basename。"""
    job_dir = (output_root / job["job_id"]).resolve()
    paths = job.get("result_paths") or {}
    if not isinstance(paths, dict):
        return
    seen: set[str] = set()
    for key in sorted(paths):
        raw = str(paths.get(key) or "")
        if not raw:
            continue
        try:
            p = Path(raw).resolve()
        except OSError:
            continue
        if not p.is_file():
            continue
        # 只搬运本任务输出目录内的文件（防 result_paths 被篡改指向任意外部文件）
        if job_dir not in p.parents:
            continue
        name = p.name
        if not _SAFE_NAME_RE.match(name) or name in seen:
            continue
        seen.add(name)
        yield name, p


def build_export_bundle(app_version: str = "") -> tuple[Path, dict]:
    """构建导出 zip（写临时文件，返回路径与 manifest；调用方负责清理临时文件）。"""
    import tempfile

    from ..version import get_version

    jobs, _, skipped_nonterminal = collect_export_jobs()
    output_root = _export_dir()

    # 先收集产物清单（路径校验一遍），保证 manifest counts 一次写对、无需重写包
    artifact_plan: list[tuple[str, str, Path]] = []
    for job in jobs:
        for name, path in _iter_job_artifacts(job, output_root):
            artifact_plan.append((job["job_id"], name, path))

    manifest = {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "schema_version": db.SCHEMA_VERSION,
        "app_version": app_version or get_version(),
        "exported_at": time.time(),
        "counts": {
            "jobs": len(jobs),
            "skipped_nonterminal": skipped_nonterminal,
            "artifacts": len(artifact_plan),
        },
    }

    import os as _os

    # mkstemp 只借路径名（zipfile 按名重建文件）；fd 必须显式关闭，长驻进程防止文件句柄遗留
    tmp_fd, tmp_name = tempfile.mkstemp(prefix="vts-export-", suffix=".zip")
    _os.close(tmp_fd)
    tmp = Path(tmp_name)
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            zf.writestr("jobs.json", json.dumps(jobs, ensure_ascii=False, indent=2))
            templates = _collect_custom_templates()
            zf.writestr("templates.json", json.dumps(templates, ensure_ascii=False, indent=2))
            for job_id, name, path in artifact_plan:
                # 产物可能在导出期间被并发重试/删除清空：消失的容忍跳过
                # （manifest counts 按 plan 计，快照语义），绝不因单个文件中断打包
                if not path.is_file():
                    continue
                zf.write(path, f"artifacts/{job_id}/{name}")
    except BaseException:
        # 失败路径必须清理临时文件（app 层的 BackgroundTask 只在成功返回时注册）
        tmp.unlink(missing_ok=True)
        raise
    return tmp, manifest


def _collect_custom_templates() -> list[dict]:
    """自定义模板全表（内置模板来自代码常量 SUMMARY_TEMPLATES，不入库不导出）。"""
    from ..summarizers.openai import SUMMARY_TEMPLATES

    db.init_db()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT name, prompt, created_at, updated_at FROM summary_templates"
        ).fetchall()
    builtin = set(SUMMARY_TEMPLATES)
    return [
        {"name": r["name"], "prompt": r["prompt"], "created_at": r["created_at"], "updated_at": r["updated_at"]}
        for r in rows
        if r["name"] not in builtin
    ]


# ---------------------------------------------------------------- 导入

# schema 逐版本升级 step：key = 包内 schema_version，把 job dict 从该版本
# 形态升到 version+1。首个需要登记的版本出现时（未来给 jobs 加列）在此补充，
# 与 db.MIGRATIONS 同哲学；当前已知历史缺口（v2 增列）由 _normalize_job 的
# 显式默认值兜底。
IMPORT_JOB_UPGRADES: dict[int, Any] = {}


def _safe_extract_bundle(data: bytes, target: Path) -> None:
    """解包导出 zip：逐成员校验 resolve 后必须落在 target 内（防路径穿越）。"""
    try:
        # macOS 上 mkdtemp 路径含符号链接（/var → /private/var）：必须与
        # resolve 后的根比较，否则合法成员会被误判为穿越
        root = target.resolve()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for member in zf.infolist():
                if member.file_size > _MAX_MEMBER_BYTES:
                    raise HistoryBundleError(f"包内文件过大：{member.filename}")
                dest = (target / member.filename).resolve()
                if dest != root and root not in dest.parents:
                    raise HistoryBundleError(f"包内存在非法路径：{member.filename}")
            target.mkdir(parents=True, exist_ok=True)
            zf.extractall(target)
    except zipfile.BadZipFile as exc:
        raise HistoryBundleError("不是有效的导出包（zip 解析失败）") from exc


_MAX_MEMBER_BYTES = 1024 * 1024 * 1024  # 单成员 1GB 上限（防御性）


def _parse_manifest(bundle_dir: Path) -> dict:
    try:
        manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HistoryBundleError("导出包缺少 manifest.json") from exc
    except (TypeError, ValueError) as exc:
        raise HistoryBundleError("manifest.json 不是有效的 JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT_NAME:
        raise HistoryBundleError("不是本应用的导出包（format 不匹配）")
    if manifest.get("format_version") != FORMAT_VERSION:
        raise HistoryBundleError(
            f"导出包格式版本不受支持（{manifest.get('format_version')}），请升级应用后重试"
        )
    try:
        schema_version = int(manifest.get("schema_version", 0))
    except (TypeError, ValueError) as exc:
        raise HistoryBundleError("manifest.schema_version 非法") from exc
    if schema_version > db.SCHEMA_VERSION:
        raise HistoryBundleError(
            f"导出包来自更新的数据库版本（v{schema_version} > v{db.SCHEMA_VERSION}），请先升级应用再导入"
        )
    manifest["schema_version"] = schema_version
    return manifest


def _normalize_job(raw: Any, from_version: int) -> Optional[dict]:
    """把包内 job dict 归一到当前 schema 形态：逐版本升级 + 白名单字段 + 默认值。

    返回 None 表示该条不合法（缺 job_id / 非终态 / 类型不符），调用方跳过。
    """
    if not isinstance(raw, dict):
        return None
    job: dict = dict(raw)
    version = from_version
    while version < db.SCHEMA_VERSION:
        step = IMPORT_JOB_UPGRADES.get(version)
        if step is not None:
            step(job)
        version += 1
    job_id = str(job.get("job_id") or "")
    # 安全校验：job_id 会被拼进产物还原路径（output_root / job_id + mkdir）。
    # 白名单 [A-Za-z0-9._-] 与本机 uuid4().hex 兼容；显式排除 "."/".."——
    # 它们在字符集内但作为路径分量指向父目录，构成穿越。不合法按
    # skipped_invalid 跳过（与逐行容错语义一致）。
    if not job_id or job_id in {".", ".."} or not _SAFE_NAME_RE.match(job_id):
        return None
    status = str(job.get("status") or "")
    if status not in _TERMINAL_STATUSES:
        return None  # 防御性：导出侧已保证终态，导入侧再守一道
    payload = job.get("payload")
    result_paths = job.get("result_paths")
    progress = job.get("progress")
    normalized = {
        "job_id": job_id,
        "status": status,
        "title": str(job.get("title") or ""),
        "source_type": str(job.get("source_type") or "url"),
        "source": str(job.get("source") or ""),
        "retried_at": job.get("retried_at") if isinstance(job.get("retried_at"), float) else None,
        "retry_count": int(job.get("retry_count") or 0),
        "payload": payload if isinstance(payload, dict) else {},
        "result_paths": result_paths if isinstance(result_paths, dict) else None,
        "error": str(job.get("error") or ""),
        "progress": progress if isinstance(progress, list) else [],
        # 与逐行 skip 的容错语义一致：非数值时间戳回落当前时间而非整包 500
        "created_at": _safe_float(job.get("created_at"), time.time()),
        "updated_at": _safe_float(job.get("updated_at"), time.time()),
        # 旧包（schema v9 前导出）无此字段 → None（未编辑）
        "summary_edited_at": float(job["summary_edited_at"]) if isinstance(job.get("summary_edited_at"), (int, float)) and not isinstance(job.get("summary_edited_at"), bool) else None,
    }
    labels = job.get("labels")
    normalized["labels"] = [str(x) for x in labels] if isinstance(labels, list) else []
    return normalized


def import_bundle(data: bytes, output_root: Path) -> dict:
    """导入导出包（原子：全部 DB 写在单事务内；产物还原与索引在提交后进行）。

    返回计数：{imported, skipped_existing, skipped_invalid, labels_created,
    templates_created, templates_skipped, artifacts_restored}。
    """
    import shutil
    import tempfile

    from ..summarizers.openai import SUMMARY_TEMPLATES
    from .label_store import _get_or_create_label_id, normalize_label_names

    workdir = Path(tempfile.mkdtemp(prefix="vts-import-"))
    try:
        _safe_extract_bundle(data, workdir)
        manifest = _parse_manifest(workdir)
        from_version = manifest["schema_version"]
        try:
            raw_jobs = json.loads((workdir / "jobs.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, TypeError, ValueError) as exc:
            raise HistoryBundleError("导出包缺少 jobs.json 或内容非法") from exc
        if not isinstance(raw_jobs, list):
            raise HistoryBundleError("jobs.json 必须是数组")
        try:
            raw_templates = json.loads((workdir / "templates.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, TypeError, ValueError):
            raw_templates = []

        jobs: list[dict] = []
        skipped_invalid = 0
        for raw in raw_jobs:
            normalized = _normalize_job(raw, from_version)
            if normalized is None:
                skipped_invalid += 1
                continue
            jobs.append(normalized)

        db.init_db()
        counters = {
            "imported": 0,
            "skipped_existing": 0,
            "skipped_invalid": skipped_invalid,
            "labels_created": 0,
            "templates_created": 0,
            "templates_skipped": 0,
            "artifacts_restored": 0,
        }
        imported_ids: list[str] = []
        imported_paths: dict[str, Optional[dict]] = {}
        existing_label_ids: set[int] = set()

        with db.get_conn() as conn:
            before_labels = conn.execute("SELECT COUNT(*) AS n FROM labels").fetchone()["n"]
            before_templates = conn.execute("SELECT COUNT(*) AS n FROM summary_templates").fetchone()["n"]

            for job in jobs:
                exists = conn.execute(
                    "SELECT 1 FROM jobs WHERE job_id = ?", (job["job_id"],)
                ).fetchone()
                if exists:
                    counters["skipped_existing"] += 1
                    continue
                # 列集合与 tasks.Job.save 一致（白名单字段，未知键已被归一丢弃）
                conn.execute(
                    """INSERT INTO jobs
                           (job_id, status, title, source_type, source, retried_at, retry_count,
                            payload, result_paths, error, progress, created_at, updated_at,
                            summary_edited_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        job["job_id"],
                        job["status"],
                        job["title"],
                        job["source_type"],
                        job["source"],
                        job["retried_at"],
                        job["retry_count"],
                        json.dumps(job["payload"], ensure_ascii=False),
                        json.dumps(job["result_paths"], ensure_ascii=False) if job["result_paths"] else None,
                        job["error"],
                        json.dumps(job["progress"], ensure_ascii=False),
                        job["created_at"],
                        job["updated_at"],
                        job["summary_edited_at"],
                    ),
                )
                counters["imported"] += 1
                imported_ids.append(job["job_id"])
                imported_paths[job["job_id"]] = job["result_paths"]

            # 标签：按名字 get-or-create（labels.id 库内自增，跨库不可移植）重映射
            for job in jobs:
                if job["job_id"] not in imported_ids:
                    continue
                for name in normalize_label_names(job["labels"]):
                    label_id = _get_or_create_label_id(conn, name)
                    conn.execute(
                        "INSERT OR IGNORE INTO job_labels (job_id, label_id) VALUES (?, ?)",
                        (job["job_id"], label_id),
                    )

            # 自定义模板：同名或与内置冲突跳过（内置模板随版本内置，覆盖只读数据不明智）
            for tpl in raw_templates if isinstance(raw_templates, list) else []:
                if not isinstance(tpl, dict):
                    continue
                name = str(tpl.get("name") or "").strip()
                prompt = str(tpl.get("prompt") or "")
                if not name or not prompt.strip() or name in SUMMARY_TEMPLATES:
                    counters["templates_skipped"] += 1
                    continue
                exists = conn.execute(
                    "SELECT 1 FROM summary_templates WHERE name = ?", (name,)
                ).fetchone()
                if exists:
                    counters["templates_skipped"] += 1
                    continue
                conn.execute(
                    "INSERT INTO summary_templates (name, prompt, created_at, updated_at) VALUES (?,?,?,?)",
                    (name, prompt, float(tpl.get("created_at") or time.time()), float(tpl.get("updated_at") or time.time())),
                )
                counters["templates_created"] += 1

            after_labels = conn.execute("SELECT COUNT(*) AS n FROM labels").fetchone()["n"]
            after_templates = conn.execute("SELECT COUNT(*) AS n FROM summary_templates").fetchone()["n"]
            counters["labels_created"] = int(after_labels - before_labels)
            counters["templates_created"] = int(after_templates - before_templates)

        # ---- 事务已提交：产物还原 → result_paths 重写 → 缓存/索引刷新 ----
        for job_id in imported_ids:
            artifacts_dir = workdir / "artifacts" / job_id
            restored: dict[str, str] = {}
            if artifacts_dir.is_dir():
                target_dir = output_root / job_id
                target_dir.mkdir(parents=True, exist_ok=True)
                for member in sorted(artifacts_dir.iterdir()):
                    if not member.is_file() or not _SAFE_NAME_RE.match(member.name):
                        continue
                    dest = target_dir / member.name
                    shutil.copyfile(member, dest)
                    restored[member.name] = str(dest)
                    counters["artifacts_restored"] += 1
            if restored:
                result_paths = imported_paths.get(job_id) or {}
                rewritten = dict(result_paths)
                for key, old_path in result_paths.items():
                    basename = Path(str(old_path)).name
                    if basename in restored:
                        rewritten[key] = restored[basename]
                _rewrite_result_paths(job_id, rewritten)
                imported_paths[job_id] = rewritten

        _refresh_job_cache(imported_ids)
        _index_imported_jobs(imported_ids, imported_paths)
        return counters
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _rewrite_result_paths(job_id: str, result_paths: dict) -> None:
    """产物还原后把 result_paths 重写为本机新路径（与任务行一起保持一致）。"""
    db.init_db()
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET result_paths = ? WHERE job_id = ?",
            (json.dumps(result_paths, ensure_ascii=False), job_id),
        )


def _refresh_job_cache(job_ids: list[str]) -> None:
    """把导入的任务重建进内存缓存（get_job 内存优先，保持双写一致）。"""
    from .tasks import Job, _jobs

    if not job_ids:
        return
    db.init_db()
    placeholders = ",".join("?" * len(job_ids))
    with db.get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE job_id IN ({placeholders})", job_ids
        ).fetchall()
    for row in rows:
        try:
            _jobs[row["job_id"]] = Job.from_db(row)
        except Exception:  # noqa: BLE001 - 单条缓存重建失败不影响导入结果
            logger.warning("import: failed to cache job %s", row["job_id"], exc_info=True)


def _index_imported_jobs(job_ids: list[str], imported_paths: dict[str, Optional[dict]]) -> None:
    """导入的 completed 任务建全文索引（复用任务完成时的同一入口，best-effort）。"""
    from ..constants import JobStatus as _JobStatus
    from .fts_index import index_job_artifacts

    for job_id in job_ids:
        paths = imported_paths.get(job_id)
        if not paths:
            continue
        try:
            with db.get_conn() as conn:
                row = conn.execute(
                    "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
            if row and row["status"] == _JobStatus.COMPLETED:
                index_job_artifacts(job_id, paths)
        except Exception:  # noqa: BLE001 - 索引失败不影响导入结果
            logger.warning("import: fts index failed for job %s", job_id, exc_info=True)


def _safe_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
