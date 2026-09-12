"""db 层测试：schema 初始化、旧 JSON 一次性迁移、任务持久化、模板入库、重启恢复。"""

import json
import sqlite3

import pytest

from video_to_summary import db as store_db


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    monkeypatch.setattr(store_db, "DATABASE_PATH", tmp_path / "app.db")
    monkeypatch.setattr(store_db, "LEGACY_LLM_FILE", tmp_path / "missing_llm_profiles.json")
    monkeypatch.setattr(store_db, "LEGACY_TEMPLATE_FILE", tmp_path / "missing_summary_templates.json")
    store_db.reset()
    return tmp_path / "app.db"


def test_init_db_creates_schema(db_path) -> None:
    store_db.init_db()
    assert db_path.exists()
    with store_db.get_conn() as conn:
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"meta", "llm_profiles", "llm_routing", "summary_templates", "jobs"} <= tables


def test_migrate_legacy_llm_profiles_once(tmp_path, monkeypatch, db_path) -> None:
    legacy = tmp_path / "llm_profiles.json"
    legacy.write_text(
        json.dumps(
            {
                "llm_profiles": {
                    "p1": {
                        "id": "p1",
                        "name": "n",
                        "purpose": "summary",
                        "provider": "openai",
                        "api_key": "SECRET",
                        "base_url": "https://x",
                        "model": "m",
                    },
                },
                "llm_routing": {"summary": "p1"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(store_db, "LEGACY_LLM_FILE", legacy)

    store_db.init_db()
    with store_db.get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM llm_profiles").fetchone()["c"]
        assert count == 1
        routing = conn.execute(
            "SELECT profile_id FROM llm_routing WHERE purpose='summary'"
        ).fetchone()
        assert routing["profile_id"] == "p1"

    # 再次 init（幂等）：表非空时不重复导入
    store_db.reset()
    store_db.init_db()
    with store_db.get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM llm_profiles").fetchone()["c"]
        assert count == 1


def test_migrate_legacy_templates_once(tmp_path, monkeypatch, db_path) -> None:
    legacy = tmp_path / "summary_templates.json"
    legacy.write_text(
        json.dumps(
            {
                "custom": {
                    "sections": ["甲", "乙"],
                    "optional_sections": ["乙"],
                    "hints": {"甲": "提示"},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(store_db, "LEGACY_TEMPLATE_FILE", legacy)

    store_db.init_db()
    from video_to_summary.web.template_store import get_template

    # v4 模型：旧三层结构合成单段提示词
    assert get_template("custom")["prompt"] == "按以下章节整理总结：\n1. 甲：提示\n2. 乙"


def test_migration_v4_converts_sections_to_prompt(tmp_path, monkeypatch) -> None:
    """v3 库（sections 三层结构）升级到 v4：自动重建为单段 prompt，且幂等。"""
    db_file = tmp_path / "v3.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE summary_templates (
            name TEXT PRIMARY KEY,
            sections TEXT NOT NULL DEFAULT '[]',
            optional_sections TEXT NOT NULL DEFAULT '[]',
            hints TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        INSERT INTO summary_templates (name, sections, optional_sections, hints, created_at, updated_at)
        VALUES ('旧模板', '["核心观点","数据"]', '[]', '{"核心观点":"压缩","数据":"带数字"}', 1.0, 1.0);
        INSERT INTO meta(key, value) VALUES ('schema_version', '3');
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(store_db, "DATABASE_PATH", db_file)
    store_db.reset()
    store_db.init_db()

    with store_db.get_conn() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(summary_templates)")}
        assert "prompt" in cols and "sections" not in cols
        row = conn.execute("SELECT prompt FROM summary_templates WHERE name='旧模板'").fetchone()
        assert row["prompt"] == "按以下章节整理总结：\n1. 核心观点：压缩\n2. 数据：带数字"
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"] == str(
            store_db.SCHEMA_VERSION
        )

    # 幂等：再次 init 不报错、结构不变
    store_db.reset()
    store_db.init_db()
    with store_db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM summary_templates").fetchone()["c"] == 1


def test_migration_v5_encrypts_plaintext_keys(tmp_path, monkeypatch) -> None:
    """v4 库（含明文密钥）升级到 v5：明文统一加密、已加密跳过、空值不动，幂等。"""
    import sqlite3 as _sqlite3

    from video_to_summary import crypto

    db_file = tmp_path / "v4.db"
    conn = _sqlite3.connect(str(db_file))
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE llm_profiles (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, purpose TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT 'openai',
            api_key TEXT NOT NULL DEFAULT '', base_url TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        INSERT INTO meta(key, value) VALUES ('schema_version', '4');
        INSERT INTO llm_profiles (id, name, purpose, api_key, base_url, model, created_at, updated_at)
            VALUES ('p1', '明文', 'asr', 'plain-secret-123', '', '', 1.0, 1.0);
        INSERT INTO llm_profiles (id, name, purpose, api_key, base_url, model, created_at, updated_at)
            VALUES ('p2', '已加密', 'summary', 'PLACEHOLDER', '', '', 1.0, 1.0);
        INSERT INTO llm_profiles (id, name, purpose, api_key, base_url, model, created_at, updated_at)
            VALUES ('p3', '空', 'polish', '', '', '', 1.0, 1.0);
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(store_db, "DATABASE_PATH", db_file)
    store_db.reset()
    crypto.reset()  # 绑定到本 tmp DB 同目录的 enc_key

    # p2 预置"已加密"值：须在 DATABASE_PATH 就位后生成（同 enc_key 才可解密）
    pre_encrypted = crypto.encrypt_secret("already-encrypted-value")
    with _sqlite3.connect(str(db_file)) as c:
        c.execute("UPDATE llm_profiles SET api_key=? WHERE id='p2'", (pre_encrypted,))

    store_db.init_db()

    with store_db.get_conn() as conn:
        p1 = conn.execute("SELECT api_key FROM llm_profiles WHERE id='p1'").fetchone()["api_key"]
        p2 = conn.execute("SELECT api_key FROM llm_profiles WHERE id='p2'").fetchone()["api_key"]
        p3 = conn.execute("SELECT api_key FROM llm_profiles WHERE id='p3'").fetchone()["api_key"]
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"]

    # 明文 → 加密且可还原
    assert crypto.is_encrypted(p1)
    assert crypto.decrypt_secret(p1) == "plain-secret-123"
    # 已加密 → 原样跳过（Fernet 非确定性，字节一致即未被重加密）
    assert p2 == pre_encrypted
    # 空 → 不动
    assert p3 == ""
    # 版本写回
    assert version == str(store_db.SCHEMA_VERSION)

    # 幂等：再次 init 不重复加密
    store_db.reset()
    store_db.init_db()
    with store_db.get_conn() as conn:
        p1b = conn.execute("SELECT api_key FROM llm_profiles WHERE id='p1'").fetchone()["api_key"]
    assert crypto.decrypt_secret(p1b) == "plain-secret-123"


def test_job_survives_restart(tmp_path, monkeypatch, db_path) -> None:
    from video_to_summary.web import tasks as web_tasks

    store_db.init_db()
    job = web_tasks.create_job({"source_type": "url", "url": "https://x", "title": "T"})
    job.mark_running()
    job.mark_completed({"summary": "out/x.summary.md"})

    # 模拟服务重启：清空内存缓存，只能从 DB 恢复
    web_tasks._jobs.clear()
    restored = web_tasks.get_job(job.job_id)
    assert restored is not None
    assert restored.status == "completed"
    assert restored.result_paths == {"summary": "out/x.summary.md"}
    assert restored.title == "T"

    listed = web_tasks.list_jobs()
    assert listed and listed[0]["job_id"] == job.job_id


def test_init_keeps_non_terminal_jobs_for_resume(db_path) -> None:
    from video_to_summary.web import tasks as web_tasks

    store_db.init_db()
    job = web_tasks.create_job({"source_type": "url", "url": "https://x"})
    job.mark_running()
    web_tasks._jobs.clear()

    # 重启后 init_db 不再把非终态任务标记 failed，保留状态供 resume 恢复执行
    store_db.reset()
    store_db.init_db()
    restored = web_tasks.get_job(job.job_id)
    assert restored.status == "running"


def test_resume_pending_jobs_requeues(monkeypatch, db_path) -> None:
    from video_to_summary.web import tasks as web_tasks

    # 0 插件构建无任何拒绝分支（resume 已无门控语义），resume 直接走恢复机制本身
    store_db.init_db()
    job = web_tasks.create_job({"source_type": "url", "url": "https://x"})
    job.mark_running()
    web_tasks._jobs.clear()

    enqueued: list[str] = []
    monkeypatch.setattr(web_tasks, "enqueue_job", lambda j: enqueued.append(j.job_id))
    web_tasks.resume_pending_jobs()
    assert job.job_id in enqueued
    # 恢复任务必须注册进内存缓存，运行期进度事件对 get_job 才可见
    assert job.job_id in web_tasks._jobs


def test_resume_batch_exhaustion(monkeypatch, db_path) -> None:
    """分批全量恢复：超过单批 LIMIT 的积压任务也要入队（否则永久 pending 饥饿）。"""
    from video_to_summary.web import tasks as web_tasks

    store_db.init_db()
    created = [
        web_tasks.create_job({"source_type": "url", "url": f"https://x/{i}"}).job_id
        for i in range(25)
    ]
    web_tasks._jobs.clear()

    enqueued: list[str] = []
    monkeypatch.setattr(web_tasks, "enqueue_job", lambda j: enqueued.append(j.job_id))
    web_tasks.resume_pending_jobs()
    assert sorted(enqueued) == sorted(created)


def test_template_roundtrip(db_path) -> None:
    from video_to_summary.web.template_store import (
        get_template,
        list_templates,
        save_template,
    )

    store_db.init_db()
    assert "custom-db" not in list_templates()
    save_template("custom-db", "用投资备忘录风格总结，重点写持仓变化")
    assert "custom-db" in list_templates()
    assert get_template("custom-db") == {"prompt": "用投资备忘录风格总结，重点写持仓变化"}
    with pytest.raises(ValueError):
        save_template("bad", "")


def test_list_templates_orders_builtins_then_customs(db_path) -> None:
    """内置模板按声明序（使用频率）在前且只读；自定义按名称排后。"""
    from video_to_summary.summarizers.openai import SUMMARY_TEMPLATES
    from video_to_summary.web.template_store import (
        delete_template,
        get_template,
        list_templates,
        save_template,
    )

    store_db.init_db()
    assert list_templates() == list(SUMMARY_TEMPLATES.keys())

    save_template("我的模板", "总结要点")
    expected = list(SUMMARY_TEMPLATES.keys()) + ["我的模板"]
    assert list_templates() == expected
    # 内置模板只读：覆盖/删除被拒绝（ValueError → API 400）
    with pytest.raises(ValueError, match="不可编辑"):
        save_template("通用", "覆盖内置的通用")
    with pytest.raises(ValueError, match="不可编辑"):
        delete_template("通用")
    # 兼容别名仍解析到内置「通用」
    assert get_template("default") == SUMMARY_TEMPLATES["通用"]


def test_seed_default_llm_profiles_on_empty_db(db_path) -> None:
    store_db.init_db()
    with store_db.get_conn() as conn:
        profiles = conn.execute("SELECT * FROM llm_profiles ORDER BY purpose").fetchall()
        routing = {r["purpose"]: r["profile_id"] for r in conn.execute("SELECT purpose, profile_id FROM llm_routing")}
    assert len(profiles) == 2
    assert {p["purpose"] for p in profiles} == {"summary", "asr"}
    assert {p["id"] for p in profiles} == {"summary", "asr"}
    assert all(p["api_key"] == "" for p in profiles)  # key 留空，由用户填入
    assert routing == {}  # 两槽位模型无路由概念
    # 再次 init 不重复播种
    store_db.reset()
    store_db.init_db()
    with store_db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM llm_profiles").fetchone()["c"] == 2


def test_seed_not_rerun_after_user_clears(db_path) -> None:
    store_db.init_db()
    with store_db.get_conn() as conn:
        conn.execute("DELETE FROM llm_profiles")
    store_db.reset()
    store_db.init_db()
    with store_db.get_conn() as conn:
        # meta 已标记，用户清空后不再自动播种
        assert conn.execute("SELECT COUNT(*) AS c FROM llm_profiles").fetchone()["c"] == 0


def test_fresh_db_has_enriched_job_columns(db_path) -> None:
    store_db.init_db()
    with store_db.get_conn() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert {"source", "retried_at", "retry_count"} <= cols
    ver = None
    with store_db.get_conn() as conn:
        ver = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"]
    assert ver == str(store_db.SCHEMA_VERSION)


def test_migration_v1_to_latest_adds_columns_and_tables(db_path) -> None:
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta(key, value) VALUES ('schema_version', '1');
        CREATE TABLE jobs (
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
        """
    )
    conn.commit()
    conn.close()

    store_db.reset()
    store_db.init_db()
    with store_db.get_conn() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
        assert {"source", "retried_at", "retry_count"} <= cols
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"] == str(
            store_db.SCHEMA_VERSION
        )
        # v6 新增标签表 / v8 新增全文索引表（开源核心表结构）
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"labels", "job_labels", "jobs_fts"} <= tables


def test_legacy_import_defaults_anchor_to_db_dir_not_cwd(tmp_path, monkeypatch) -> None:
    """安全回归：默认迁移路径锚定 DB 同目录、与 CWD 无关，且明文 Key 必须加密落盘。

    历史缺陷：LEGACY_LLM_FILE 曾是 Path("llm_profiles.json")（CWD 相对路径），
    从任何目录启动服务都会把该目录下同名文件里的明文 API Key 静默复制进新库。
    """
    db_dir = tmp_path / "data"
    monkeypatch.setattr(store_db, "DATABASE_PATH", db_dir / "app.db")
    # 故意不 patch LEGACY_*（保持 None 哨兵），并切换 CWD 证明路径推导不再受启动目录影响
    monkeypatch.chdir(tmp_path)
    db_dir.mkdir()

    (db_dir / "llm_profiles.json").write_text(
        json.dumps(
            {
                "llm_profiles": {
                    "p1": {
                        "id": "p1",
                        "name": "n",
                        "purpose": "summary",
                        "provider": "openai",
                        "api_key": "PLAINTEXT_SECRET",
                        "base_url": "https://x",
                        "model": "m",
                    },
                },
                "llm_routing": {"summary": "p1"},
            }
        ),
        encoding="utf-8",
    )

    store_db.reset()
    store_db.init_db()

    from video_to_summary.crypto import decrypt_secret

    with store_db.get_conn() as conn:
        row = conn.execute("SELECT * FROM llm_profiles WHERE id='p1'").fetchone()
        assert row is not None
        # 明文不落盘：与 Web 配置写入路径同一加密标准
        assert row["api_key"].startswith("enc:v1:")
        assert decrypt_secret(row["api_key"]) == "PLAINTEXT_SECRET"
        recorded = conn.execute(
            "SELECT value FROM meta WHERE key='legacy_imported_llm_profiles'"
        ).fetchone()
    payload = json.loads(recorded["value"])
    assert payload["count"] == 1
    assert isinstance(payload["at"], float)


def test_legacy_import_keeps_already_encrypted_key(tmp_path, monkeypatch) -> None:
    """迁移源里已是密文（enc:v1: 前缀）的 Key 应原样保留，避免二次加密。"""
    db_dir = tmp_path / "data"
    db_dir.mkdir()
    legacy = db_dir / "llm_profiles.json"
    monkeypatch.setattr(store_db, "LEGACY_LLM_FILE", legacy)
    monkeypatch.setattr(store_db, "DATABASE_PATH", db_dir / "app.db")
    legacy.write_text(
        json.dumps(
            {
                "llm_profiles": {
                    "c1": {"id": "c1", "name": "n", "purpose": "asr",
                           "provider": "openai", "api_key": "enc:v1:ZmFrZQ", "model": "m"},
                },
            }
        ),
        encoding="utf-8",
    )
    store_db.reset()
    store_db.init_db()
    with store_db.get_conn() as conn:
        row = conn.execute("SELECT api_key FROM llm_profiles WHERE id='c1'").fetchone()
    assert row["api_key"] == "enc:v1:ZmFrZQ"


def test_decrypt_secret_strict_failure(tmp_path, monkeypatch):
    """加密密文解不开必须抛错（不得静默把密文当明文），legacy 明文原样返回。"""
    from video_to_summary import crypto

    monkeypatch.setattr(store_db, "DATABASE_PATH", tmp_path / "x.db")
    crypto.reset()

    # legacy 明文：未加密 → 原样即密钥本身
    assert crypto.decrypt_secret("PLAINTEXT") == "PLAINTEXT"
    assert crypto.decrypt_secret("") == ""

    # 带 enc:v1: 前缀但内容损坏 → 抛 SecretDecryptError
    with pytest.raises(crypto.SecretDecryptError):
        crypto.decrypt_secret("enc:v1:garbage-not-fernert-token")
    # 正常密文仍可解密
    enc = crypto.encrypt_secret("real-key")
    assert crypto.decrypt_secret(enc) == "real-key"


def test_llm_profile_undecryptable_key_degrades_to_empty(tmp_path, monkeypatch):
    """profile 密文解不开 → api_key 视为未配置（而不是把密文当明文下发）。"""
    import sqlite3 as _sqlite3

    from video_to_summary import crypto
    from video_to_summary.web import llm_store

    db_file = tmp_path / "llm.db"
    conn = _sqlite3.connect(str(db_file))
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE llm_profiles (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, purpose TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT 'openai',
            api_key TEXT NOT NULL DEFAULT '', base_url TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE llm_routing (purpose TEXT PRIMARY KEY, profile_id TEXT);
        INSERT INTO meta(key, value) VALUES ('schema_version', '5');
        INSERT INTO llm_profiles (id, name, purpose, api_key, base_url, model, created_at, updated_at)
            VALUES ('p-bad', '坏', 'asr', 'enc:v1:broken-token-xxx', '', '', 1.0, 1.0);
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(store_db, "DATABASE_PATH", db_file)
    store_db.reset()
    crypto.reset()

    slot = llm_store.get_slot("asr")
    assert slot["api_key"] == ""  # 降级为未配置，而非密文字符串


def test_migration_v6_creates_label_tables(tmp_path, monkeypatch):
    """v5 库升级到 v6：labels/job_labels 表建立、版本写回 6、幂等、旧任务不被打标签（未分类）。"""
    import sqlite3 as _sqlite3

    from video_to_summary.web import label_store
    from video_to_summary.web.tasks import create_job

    db_file = tmp_path / "app.db"
    monkeypatch.setattr(store_db, "DATABASE_PATH", db_file)
    monkeypatch.setattr(store_db, "LEGACY_LLM_FILE", tmp_path / "no1.json")
    monkeypatch.setattr(store_db, "LEGACY_TEMPLATE_FILE", tmp_path / "no2.json")
    # 先完整 init 到 v6（获得真实 jobs 全 schema），再把 schema_version 回退到 5
    # 并删掉标签表，模拟一个真实的 v5 库
    store_db.reset()
    store_db.init_db()
    conn = _sqlite3.connect(str(db_file))
    conn.execute("UPDATE meta SET value='5' WHERE key='schema_version'")
    conn.execute("DROP TABLE job_labels")
    conn.execute("DROP TABLE labels")
    conn.commit()
    conn.close()

    store_db.reset()
    store_db.init_db()

    with store_db.get_conn() as conn:
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "labels" in tables and "job_labels" in tables
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"]
        assert version == str(store_db.SCHEMA_VERSION)
        assert conn.execute("SELECT COUNT(*) c FROM labels").fetchone()["c"] == 0

    # 旧任务（迁移后新建的）默认未分类
    job = create_job({"source_type": "url", "url": "https://example.test/x", "title": "旧数据样例"})
    assert label_store.get_job_labels(job.job_id) == []

    # 幂等：再次 init 不重建/不报错
    store_db.reset()
    store_db.init_db()
    with store_db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM labels").fetchone()["c"] == 0

def test_migration_v8_creates_fts_table_and_writes_version(db_path) -> None:
    """v8：jobs_fts 全文索引表创建 + schema_version 无条件写回（新库从 0 全程迁移）。"""
    store_db.init_db()
    with store_db.get_conn() as conn:
        ver = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"]
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert ver == str(store_db.SCHEMA_VERSION)
    assert "jobs_fts" in tables


def test_migration_v8_from_v7_db_upgrades_and_indexes_survive(db_path) -> None:
    """v7 存量库升级：jobs 数据完好，jobs_fts 可用（trigram 或 unicode61 回退）。"""
    from video_to_summary.web import fts_index

    # 先完整 init（到 v8），再回退到 v7 模拟存量库，删掉 fts 表后重新 init
    store_db.init_db()
    with store_db.get_conn() as conn:
        conn.execute("DROP TABLE jobs_fts")
        conn.execute("INSERT INTO jobs (job_id, status, title, source_type, payload, progress, created_at, updated_at)"
                     " VALUES ('j1', 'completed', '存量任务', 'url', '{}', '[]', 1.0, 1.0)")
        conn.execute("UPDATE meta SET value='7' WHERE key='schema_version'")
    store_db.reset()  # 只清初始化标记，DB 文件保留
    store_db.init_db()
    with store_db.get_conn() as conn:
        ver = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"]
        job = conn.execute("SELECT title FROM jobs WHERE job_id='j1'").fetchone()
    assert ver == str(store_db.SCHEMA_VERSION)  # 迁移后无条件写回最新版本号
    assert job["title"] == "存量任务"
    # 升级后索引链路可用（trigram 或回退表均满足 fts_available）
    assert fts_index.fts_available() is True


def test_migration_v9_adds_summary_edited_at(db_path) -> None:
    """v8→v9：jobs 加 `summary_edited_at` 总结编辑标记列（NULL=未编辑），数据完好。"""
    import sqlite3 as _sqlite3

    if _sqlite3.sqlite_version_info < (3, 35, 0):
        # 该用例用 DROP COLUMN 模拟 v8 存量库，需要 SQLite ≥ 3.35；
        # 链接系统旧 SQLite 的环境（非 python.org 官方构建）直接跳过，不阻塞整组迁移测试
        pytest.skip("SQLite < 3.35 不支持 DROP COLUMN，跳过 v8→v9 迁移用例")

    # 先 init 到 v8，插入一行 completed 任务，再回退版本号 + 删列模拟 v8 存量库
    store_db.init_db()
    conn = _sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO jobs (job_id, status, title, source_type, payload, progress, created_at, updated_at)"
        " VALUES ('j9', 'completed', '存量任务', 'url', '{}', '[]', 1.0, 1.0)"
    )
    conn.commit()
    conn.close()
    conn = _sqlite3.connect(str(db_path))
    conn.execute("UPDATE meta SET value='8' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    # SQLite 3.35+ 支持 DROP COLUMN；老版本用重建表兜底——项目要求 3.10+ 自带 3.37+
    conn = _sqlite3.connect(str(db_path))
    conn.execute("ALTER TABLE jobs DROP COLUMN summary_edited_at")
    conn.commit()
    conn.close()

    store_db.reset()
    store_db.init_db()

    with store_db.get_conn() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
        assert "summary_edited_at" in cols
        ver = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"]
        row = c.execute("SELECT title, summary_edited_at FROM jobs WHERE job_id='j9'").fetchone()
    assert ver == str(store_db.SCHEMA_VERSION)
    assert row["title"] == "存量任务"
    assert row["summary_edited_at"] is None  # 存量数据 = 未编辑


def test_init_db_with_newer_db_fails_open_and_keeps_marker(db_path) -> None:
    """数据库 schema 高于本 App（由更新版本创建）：fail-open 继续运行、
    不回写降级 schema_version、db_newer_version 暴露真实版本供 UI 提示。"""
    import sqlite3 as _sqlite3

    # 先建 v9 库，再把 schema_version 抬到未来版本（模拟「更新版 App 的库」）
    store_db.init_db()
    conn = _sqlite3.connect(str(db_path))
    conn.execute("UPDATE meta SET value='10' WHERE key='schema_version'")
    conn.commit()
    conn.close()

    assert store_db.db_newer_version() is None  # 首次 init（v9 正常态）
    store_db.reset()
    store_db.init_db()

    # 版本号保持 10（未被降级回写），db_newer_version 暴露
    with store_db.get_conn() as c:
        ver = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"]
        cols = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
    assert ver == "10"
    assert "summary_edited_at" in cols  # 表结构未被破坏
    assert store_db.db_newer_version() == 10

    # 正常库 → 提示为 None
    conn = _sqlite3.connect(str(db_path))
    conn.execute("UPDATE meta SET value='9' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    store_db.reset()
    store_db.init_db()
    assert store_db.db_newer_version() is None
    with store_db.get_conn() as c:
        ver = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"]
    assert ver == str(store_db.SCHEMA_VERSION)
