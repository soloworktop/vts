"""SQLite 存储层：管理配置数据（LLM 两槽位 / 模板）与任务历史。

设计要点：
- 使用标准库 ``sqlite3``，零新增运行时依赖。
- 每次操作用短连接 + WAL 模式，保证 Web 后台线程与多请求并发安全。
- 数据库路径解析（``resolve_database_path``，纯函数可单测）：``VIDEO_TO_SUMMARY_DB``
  环境变量优先；未设置且包处于源码检出布局（本文件向上两级存在 ``pyproject.toml``
  与 ``src/video_to_summary/``）时锚定 ``<检出根>/data/app.db``（现有自部署用户的数据
  位置不变）；安装为包时落到平台用户数据目录（macOS / Windows / XDG），避免数据写进
  site-packages（venv 重建即丢、系统 Python 无写权限时启动失败）。目录缺失自动创建，
  创建失败抛提示设置 ``VIDEO_TO_SUMMARY_DB`` 的可行动报错。
- 数据库文件含真实 API Key（自 ``llm_profiles.json`` 迁移而来），启动时 chmod 0o600，
  并已加入 ``.gitignore``（``data/``），勿提交。
- 首次运行时自动把旧 JSON 文件（``llm_profiles.json`` / ``summary_templates.json``）
  一次性迁移入库；迁移路径默认锚定数据库同目录（绝不做 CWD 相对解析，避免换目录启动时
  把工作目录下同名文件里的明文 Key 静默复制进新库）；导入的 Key 一律经 Fernet 加密落盘。
  迁移后数据库为唯一事实源，原文件保留不动。
- 服务重启时，遗留的非终态任务（pending/running）由 ``web/tasks.resume_pending_jobs``
  重新入队恢复执行（而非标记 failed），保证任务连续性。

存储边界（哪些入库、哪些维持文件）：
- 入库：LLM 配置（summary/asr 两槽位，存于 llm_profiles 表）、自定义总结模板、任务历史（jobs 元数据 + 进度）。
- 维持文件：输出产物（``output/<job_id>/`` 下的 audio/txt/segments/summary），
  DB 只记录这些文件的路径（``jobs.result_paths``）；``.env`` 作为进程级启动默认值。
"""

import json
import logging
import os
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger("video_to_summary.db")


def _looks_like_source_checkout(package_file: Path) -> bool:
    """判定包是否处于源码检出布局：``__file__`` 向上两级存在 pyproject.toml 与 src/video_to_summary/。

    源码检出（``<repo>/src/video_to_summary/db.py``）时 ``parents[2]`` = 仓库根；
    pip 安装为包（site-packages）时该目录是 ``<venv>/lib/pythonX.Y`` 之类，无
    ``pyproject.toml`` / ``src/video_to_summary/`` → 判定为「安装形态」。
    """
    root = package_file.resolve().parents[2]
    return (root / "pyproject.toml").is_file() and (root / "src" / "video_to_summary").is_dir()


def _platform_data_dir(platform: str, home: Path, environ: dict) -> Path:
    """平台用户数据目录（安装形态下的数据库/模型等用户数据落点）。

    - macOS：``~/Library/Application Support/VTS``
    - Windows：``%LOCALAPPDATA%\\VTS``（LOCALAPPDATA 未设置时回落 ``~/AppData/Local/VTS``）
    - 其它（Linux 等）：``$XDG_DATA_HOME/vts``（未设置 XDG_DATA_HOME 时 ``~/.local/share/vts``）
    """
    if platform == "darwin":
        return home / "Library" / "Application Support" / "VTS"
    if platform == "win32":
        local = environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "VTS"
        return home / "AppData" / "Local" / "VTS"
    xdg = environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "vts"
    return home / ".local" / "share" / "vts"


def resolve_database_path(
    env: str | None = None,
    package_file: str | Path | None = None,
    platform: str | None = None,
    home: str | Path | None = None,
    environ: dict | None = None,
) -> Path:
    """解析 SQLite 数据库路径（纯函数，平台/家目录/环境全部可注入，便于单测）。

    优先级：
    1. ``VIDEO_TO_SUMMARY_DB`` 环境变量非空 → 直接用（行为不变，Docker/桌面等显式配置不受影响）；
    2. 未设置且包处于源码检出布局 → ``<检出根>/data/app.db``（现有自部署用户的数据位置不变）；
    3. 未设置且安装为包 → 平台用户数据目录（见 :func:`_platform_data_dir`）下的 ``app.db``。

    参数缺省时读取真实运行环境（``__file__`` / ``sys.platform`` / ``Path.home()`` /
    ``os.environ``），导入期调用即得到生产路径。
    """
    if env:
        return Path(env)
    pkg = Path(package_file) if package_file is not None else Path(__file__).resolve()
    if _looks_like_source_checkout(pkg):
        return pkg.parents[2] / "data" / "app.db"
    plat = platform or sys.platform
    home_dir = Path(home) if home is not None else Path.home()
    return _platform_data_dir(plat, home_dir, dict(environ or {})) / "app.db"


# 数据库路径：可用环境变量 VIDEO_TO_SUMMARY_DB 覆盖；未设置时按「源码检出 → 平台用户
# 数据目录」解析（见 resolve_database_path），不再假定仓库检出——非 editable 安装后
# 数据不会落进 site-packages。解析结果在 init_db() 的 INFO 日志中可见。
DATABASE_PATH = resolve_database_path(env=os.environ.get("VIDEO_TO_SUMMARY_DB"))

# 旧版 JSON 存储文件（一次性迁移来源；测试中可 monkeypatch 到临时绝对路径）。
# 安全约定：默认值为 None 哨兵 = 「与 DATABASE_PATH 同目录」。历史上这里曾是 CWD 相对
# 路径（Path("llm_profiles.json")），导致从任何目录启动服务都会把该目录下的同名文件
# （含明文 API Key）静默迁入新建的库——现已改为显式哨兵 + 运行时按 DB 目录推导，
# 彻底消除对进程启动目录的依赖。
LEGACY_LLM_FILE: Path | None = None
LEGACY_TEMPLATE_FILE: Path | None = None

SCHEMA_VERSION = 9

# 版本化增量迁移：key = 目标 schema 版本，value = 需按序执行的步骤列表。
# 步骤可以是 SQL 字符串，也可以是接收 conn 的可调用对象（需要 Python 数据
# 转换时使用，如 v4 模板重建）。init 时按 meta.schema_version 顺序执行，
# 避免手工 ALTER 导致的历史库升级遗漏。
def _migrate_summary_templates_v4(conn: sqlite3.Connection) -> None:
    """v3→v4：模板从 sections/optional_sections/hints 三层结构收敛为单段 prompt。

    旧数据按「章节序号+章节名：hint」合成等价提示词，不丢信息；已是新结构的库
    （含全新库）直接跳过。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(summary_templates)")}
    if "prompt" in cols or not cols:
        return
    conn.execute(
        "CREATE TABLE summary_templates_v4 ("
        " name TEXT PRIMARY KEY,"
        " prompt TEXT NOT NULL DEFAULT '',"
        " created_at REAL NOT NULL,"
        " updated_at REAL NOT NULL)"
    )
    for r in conn.execute(
        "SELECT name, sections, hints, created_at, updated_at FROM summary_templates"
    ).fetchall():
        conn.execute(
            "INSERT INTO summary_templates_v4 (name, prompt, created_at, updated_at) VALUES (?,?,?,?)",
            (
                r["name"],
                _synthesize_template_prompt(r["sections"], r["hints"]),
                r["created_at"],
                r["updated_at"],
            ),
        )
    conn.execute("DROP TABLE summary_templates")
    conn.execute("ALTER TABLE summary_templates_v4 RENAME TO summary_templates")


def _migrate_encrypt_plaintext_keys_v5(conn: sqlite3.Connection) -> None:
    """v4→v5：历史版本以明文落库的密钥统一加密（幂等，enc:v1: 前缀跳过）。

    背景：加密机制修复前保存的 profile（如旧版 UI/导入路径）曾以明文写入
    llm_profiles.api_key。单条加密失败不阻断启动（保留原值并 WARNING，
    cryptography 缺失时 encrypt_secret 会原样返回，同样落入此分支）。
    """
    from . import crypto

    rows = conn.execute(
        "SELECT id, api_key FROM llm_profiles WHERE api_key != ''"
    ).fetchall()
    migrated = 0
    for row in rows:
        stored = row["api_key"]
        if crypto.is_encrypted(stored):
            continue
        encrypted = crypto.encrypt_secret(stored)
        if not crypto.is_encrypted(encrypted):
            logger.warning("migration v5: llm_profiles.api_key 加密不可用（cryptography 缺失？），保留原值")
            continue
        conn.execute("UPDATE llm_profiles SET api_key = ? WHERE id = ?", (encrypted, row["id"]))
        migrated += 1

    if migrated:
        logger.info("migration v5: encrypted %d plaintext secret(s)", migrated)


def _synthesize_template_prompt(sections_json: str, hints_json: str) -> str:
    """把旧三层结构（sections/hints 的 JSON 文本）合成为单段提示词，不丢章节信息。"""
    try:
        sections = json.loads(sections_json or "[]")
        hints = json.loads(hints_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return "总结视频文稿内容"
    if not isinstance(sections, list) or not sections:
        return "总结视频文稿内容"
    hints = hints if isinstance(hints, dict) else {}
    lines = []
    for i, section in enumerate(sections, start=1):
        hint = hints.get(section) or ""
        lines.append(f"{i}. {section}" + (f"：{hint}" if hint else ""))
    return "按以下章节整理总结：\n" + "\n".join(lines)


def _migrate_create_jobs_fts_v8(conn: sqlite3.Connection) -> None:
    """v7→v8：jobs_fts 全文索引表（历史检索用，存产物派生文本、可随时重建）。

    分词器优先 trigram（支持中文子串 MATCH，sqlite ≥3.34 内置）；极端旧版
    sqlite 无 trigram 时回退默认 unicode61——此时中文全文匹配能力受限，
    调用方（web/fts_index.py）探测后自动走 LIKE 降级路径。fts5 整体缺失
    （极少数发行版裁剪）时 fail-open 跳过建表：应用照常启动，检索退化为
    仅元数据匹配，schema 版本照常推进（避免每次启动重复执行迁移）。"""
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts USING fts5("
            "text, job_id UNINDEXED, kind UNINDEXED, tokenize='trigram')"
        )
    except sqlite3.OperationalError as exc:
        logger.warning("migration v8: trigram tokenizer 不可用（%s），回退 unicode61", exc)
        try:
            conn.execute("DROP TABLE IF EXISTS jobs_fts")
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts USING fts5("
                "text, job_id UNINDEXED, kind UNINDEXED)"
            )
        except sqlite3.OperationalError as exc2:
            logger.warning("migration v8: fts5 整体不可用（%s），跳过全文索引表（检索降级）", exc2)


def _migrate_add_summary_edited_at_v9(conn: sqlite3.Connection) -> None:
    """v8→v9：jobs 加 `summary_edited_at` 列（总结编辑标记，NULL=未编辑）。

    幂等守卫（PRAGMA 探列，已存在跳过）：与 v2 的裸 ALTER 不同，本迁移之后
    仍可能有「仅回退 schema_version 模拟旧库」的用例/工具在 v9 形态库上重跑
    迁移链，裸 ALTER 会 duplicate column 崩溃。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    if "summary_edited_at" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN summary_edited_at REAL")


MIGRATIONS: dict[int, list] = {
    # 版本号沿用与外部构建共用的 schema 编号（D9：同一份核心代码多形态同源），
    # 因此这里保留历史空档（3 / 7 号迁移属外部扩展能力，不在本构建范围）——
    # 编号不对齐会让不同形态在同一 schema_version 下写出互不兼容的表结构。
    2: [
        # 任务列表增强：源/平台、最近重试时间、重试次数
        "ALTER TABLE jobs ADD COLUMN source TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE jobs ADD COLUMN retried_at REAL",
        "ALTER TABLE jobs ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0",
    ],
    4: [
        # 模板管理简化：三层章节结构 → 单段提示词（Python 数据转换见函数体）
        _migrate_summary_templates_v4,
    ],
    5: [
        # 存量明文密钥统一加密（Python 数据转换见函数体）
        _migrate_encrypt_plaintext_keys_v5,
    ],
    6: [
        # 历史任务标签系统：labels + job_labels 关联表（只进迁移，不进 _SCHEMA_SQL，
        # 新库从 version 0 顺序执行获得全部表）
        "CREATE TABLE IF NOT EXISTS labels ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " name TEXT NOT NULL UNIQUE,"
        " created_at REAL NOT NULL"
        ")",
        "CREATE TABLE IF NOT EXISTS job_labels ("
        " job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,"
        " label_id INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,"
        " PRIMARY KEY (job_id, label_id)"
        ")",
        "CREATE INDEX IF NOT EXISTS idx_job_labels_label ON job_labels(label_id)",
    ],
    8: [
        # 历史全文检索索引（web/fts_index.py 管理，trigram 优先见函数体）
        _migrate_create_jobs_fts_v8,
    ],
    9: [
        # 总结编辑标记：NULL=未编辑；编辑覆盖写回 summary 产物文件后记录时间
        _migrate_add_summary_edited_at_v9,
    ],
}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS llm_profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    purpose TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'openai',
    api_key TEXT NOT NULL DEFAULT '',
    base_url TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_routing (
    purpose TEXT PRIMARY KEY,
    profile_id TEXT REFERENCES llm_profiles(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS summary_templates (
    name TEXT PRIMARY KEY,
    prompt TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    title TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT 'url',
    payload TEXT NOT NULL DEFAULT '{}',
    result_paths TEXT,
    error TEXT,
    progress TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_profiles_purpose ON llm_profiles(purpose);
"""

_initialized = False
_init_lock = threading.Lock()
# 数据库 schema 版本高于本 App 支持的 SCHEMA_VERSION 时记录该版本号（None=正常），
# 供 /api/health 与前端提示「请升级 App」。fail-open：迁移全增量，降级读安全。
_db_newer_version: int | None = None


def db_newer_version() -> int | None:
    """本地数据库由更新版本的 App 创建时返回其 schema 版本号，否则 None。

    调用方据此提示用户升级；数据库照常可读（增量迁移下旧版读新版库安全）。
    """
    return _db_newer_version


def _resolve_path() -> Path:
    return Path(DATABASE_PATH)


def _legacy_llm_file() -> Path:
    """legacy LLM 配置 JSON 的实际路径：显式覆盖优先，否则锚定数据库所在目录。"""
    if LEGACY_LLM_FILE is not None:
        return Path(LEGACY_LLM_FILE)
    return _resolve_path().parent / "llm_profiles.json"


def _legacy_templates_file() -> Path:
    """legacy 模板 JSON 的实际路径：规则同 :func:`_legacy_llm_file`。"""
    if LEGACY_TEMPLATE_FILE is not None:
        return Path(LEGACY_TEMPLATE_FILE)
    return _resolve_path().parent / "summary_templates.json"


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """短连接上下文管理器：自动提交/回滚，用完即关，避免跨线程共享连接。

    WAL 模式在 init_db 时设置一次（持久于库文件），此处只做连接级 PRAGMA。
    """
    conn = sqlite3.connect(str(_resolve_path()), timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """惰性初始化：建表 → 版本化迁移 → 一次性迁移旧 JSON → 恢复遗留任务。幂等。"""
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        db_path = _resolve_path()
        # 目录缺失自动创建；创建/打开失败给出可行动报错（提示 VIDEO_TO_SUMMARY_DB），
        # 不静默崩在无权限上——安装形态下默认目录可能位于不可写位置。
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(
                f"无法创建数据库目录 {db_path.parent}（{exc}）。"
                "请检查该目录的写权限，或设置 VIDEO_TO_SUMMARY_DB 指向可写路径。"
            ) from exc
        try:
            with get_conn() as conn:
                conn.execute("PRAGMA journal_mode=WAL")  # WAL 仅在初始化设置一次
                conn.executescript(_SCHEMA_SQL)
                _apply_migrations(conn)
                _migrate_legacy_llm(conn)
                _migrate_legacy_templates(conn)
                _seed_default_llm(conn)
        except sqlite3.OperationalError as exc:
            # 目录可建但库打不开（已存在目录无写权限等）：同样给出可行动提示
            raise RuntimeError(
                f"无法打开数据库 {db_path}（{exc}）。"
                "请检查数据库目录的写权限，或设置 VIDEO_TO_SUMMARY_DB 指向可写路径。"
            ) from exc
        try:
            # 数据库含真实 API Key，限制为仅当前用户可读写
            os.chmod(db_path, 0o600)
        except OSError:
            pass
        logger.info("sqlite database ready at %s", db_path)
        _initialized = True


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """按 meta.schema_version 顺序执行 MIGRATIONS 中的增量迁移，并始终写入最新版本号。

    注意：迁移执行后必须无条件写回 schema_version（而非仅在 current < SCHEMA_VERSION 时），
    否则当 MIGRATIONS 把 current 推进到 SCHEMA_VERSION 时不会落库，下次初始化会重复 ALTER。

    例外：数据库 schema **高于**本 App（current > SCHEMA_VERSION）时**不回写**标记——
    否则旧版会把版本号降级回自己的 SCHEMA_VERSION，升级回跳时新版会重跑已执行的
    迁移（目前靠 v9 幂等守卫兜底，但这是隐患）；fail-open 继续运行并记录版本供提示。
    """
    global _db_newer_version
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    current = int(row["value"]) if row and row["value"] else 0
    if current > SCHEMA_VERSION:
        _db_newer_version = current
        logger.warning(
            "database schema v%s 高于本 App 支持的 v%s——请升级 App；本实例 fail-open 继续运行",
            current, SCHEMA_VERSION,
        )
        return
    _db_newer_version = None
    for version in sorted(MIGRATIONS):
        if version <= current:
            continue
        for step in MIGRATIONS[version]:
            if callable(step):
                step(conn)
            else:
                conn.execute(step)
        current = version
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )


def reset() -> None:
    """测试钩子：清除初始化标志，使下次 init_db 针对新的 DATABASE_PATH 重建。"""
    global _initialized, _db_newer_version
    with _init_lock:
        _initialized = False
        _db_newer_version = None


def _migrate_legacy_llm(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT COUNT(*) AS c FROM llm_profiles").fetchone()
    if row["c"] > 0:
        return
    legacy = _legacy_llm_file()
    if not legacy.exists():
        return
    try:
        data = json.loads(legacy.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(data, dict):
        return

    # 惰性导入：crypto 依赖 cryptography（函数内 import 兼容 PyInstaller --collect）
    from .crypto import encrypt_secret

    profiles = data.get("llm_profiles", {}) or {}
    routing = data.get("llm_routing", {}) or {}
    now = time.time()
    imported = 0
    enc_prefix = "enc:v1:"  # 与 crypto.py 密文前缀一致：已是密文则原样保留
    for pid, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        raw_key = profile.get("api_key") or ""
        if raw_key.startswith(enc_prefix):
            stored_key = raw_key
        elif raw_key:
            # 明文 Key 不允许直接落盘：与 Web 配置写入路径保持同一加密标准
            stored_key = encrypt_secret(raw_key)
        else:
            stored_key = ""
        conn.execute(
            """INSERT OR REPLACE INTO llm_profiles
               (id, name, purpose, provider, api_key, base_url, model, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                profile.get("id") or pid,
                profile.get("name") or pid,
                profile.get("purpose") or "summary",
                profile.get("provider") or "openai",
                stored_key,
                profile.get("base_url") or "",
                profile.get("model") or "",
                now,
                now,
            ),
        )
        imported += 1
    for purpose, profile_id in routing.items():
        if profile_id:
            conn.execute(
                "INSERT OR REPLACE INTO llm_routing(purpose, profile_id) VALUES (?,?)",
                (str(purpose), str(profile_id)),
            )
    if imported:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('legacy_imported_llm_profiles', ?)",
            (json.dumps({"count": imported, "at": now}),),
        )
        logger.info("imported %d legacy llm profile(s) from %s (keys encrypted)", imported, legacy)


def _migrate_legacy_templates(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT COUNT(*) AS c FROM summary_templates").fetchone()
    if row["c"] > 0:
        return
    legacy = _legacy_templates_file()
    if not legacy.exists():
        return
    try:
        data = json.loads(legacy.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(data, dict):
        return

    now = time.time()
    imported = 0
    for name, payload in data.items():
        if not isinstance(payload, dict):
            continue
        # 旧 JSON 为三层章节结构：合成为单段提示词入库（v4 模型）
        prompt = _synthesize_template_prompt(
            json.dumps(payload.get("sections", []), ensure_ascii=False),
            json.dumps(payload.get("hints", {}), ensure_ascii=False),
        )
        conn.execute(
            """INSERT OR REPLACE INTO summary_templates
               (name, prompt, created_at, updated_at)
               VALUES (?,?,?,?)""",
            (
                str(name),
                prompt,
                now,
                now,
            ),
        )
        imported += 1
    if imported:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('legacy_imported_templates', ?)",
            (json.dumps({"count": imported, "at": now}),),
        )


def _seed_default_llm(conn: sqlite3.Connection) -> None:
    """空库时播种默认 LLM 配置（summary/asr/polish 三个 profile + 路由，api_key 留空）。

    只在从未播种且无任何已有配置时执行一次（meta.llm_defaults_seeded 标记），
    用户已有配置（含旧 JSON 迁移导入）时绝不覆盖。默认槽位的 base_url 已预置，
    用户仅需在 Web「LLM 配置」中填入 API Key 与模型名。
    """
    from .config import DEFAULT_LLM_PROFILES

    seeded = conn.execute("SELECT 1 FROM meta WHERE key='llm_defaults_seeded'").fetchone()
    if seeded:
        return
    count = conn.execute("SELECT COUNT(*) AS c FROM llm_profiles").fetchone()["c"]
    if count > 0:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('llm_defaults_seeded', '1')")
        return

    now = time.time()
    for profile in DEFAULT_LLM_PROFILES:
        conn.execute(
            """INSERT INTO llm_profiles
                   (id, name, purpose, provider, api_key, base_url, model, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                profile["id"],
                profile["name"],
                profile["purpose"],
                profile["provider"],
                "",  # api_key 留空，由用户在 UI 填入
                profile["base_url"],
                profile["model"],
                now,
                now,
            ),
        )
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('llm_defaults_seeded', '1')")
    logger.info("seeded default LLM slots (summary/asr)")


def get_setting(key: str) -> str | None:
    """读取 meta 表中的应用级偏好设置；不存在返回 None。"""
    init_db()
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    """写入 meta 表中的应用级偏好设置（upsert）。"""
    init_db()
    with get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))


__all__ = [
    "DATABASE_PATH",
    "LEGACY_LLM_FILE",
    "LEGACY_TEMPLATE_FILE",
    "get_conn",
    "init_db",
    "reset",
    "get_setting",
    "set_setting",
]
