"""llm_store 单元测试（两槽位简化模型）：掩码 Key 往返、.env 导入不覆盖、服务端解析。"""

import pytest

from video_to_summary.web import llm_store


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """将 LLM 配置存储隔离到临时 SQLite，避免读写真实 data/app.db / .env / llm_profiles.json。"""
    from video_to_summary import db as store_db

    monkeypatch.setattr(store_db, "DATABASE_PATH", tmp_path / "app.db")
    monkeypatch.setattr(store_db, "LEGACY_LLM_FILE", tmp_path / "missing_llm_profiles.json")
    monkeypatch.setattr(store_db, "LEGACY_TEMPLATE_FILE", tmp_path / "missing_summary_templates.json")
    store_db.reset()
    return tmp_path / "app.db"


def test_mask_roundtrip_preserves_real_key(store) -> None:
    llm_store.save_slot(
        "summary",
        {"api_key": "REALKEY-1234567890-abcdef", "base_url": "https://x", "model": "m"},
    )
    masked = llm_store.mask_api_key(llm_store.get_slot("summary"))
    assert masked["api_key"] == "REAL****cdef"
    # 前端把掩码值原样 PUT 回来，真实 Key 必须保留
    llm_store.save_slot(
        "summary",
        {"api_key": masked["api_key"], "base_url": "https://x", "model": "m2"},
    )
    assert llm_store.get_slot("summary")["api_key"] == "REALKEY-1234567890-abcdef"


def test_save_slot_empty_key_keeps_existing(store) -> None:
    llm_store.save_slot("summary", {"api_key": "REALKEY", "base_url": "https://x"})
    llm_store.save_slot("summary", {"api_key": "", "base_url": "https://x"})
    assert llm_store.get_slot("summary")["api_key"] == "REALKEY"


def test_save_slot_validates_purpose(store) -> None:
    with pytest.raises(ValueError):
        llm_store.save_slot("nope", {"api_key": "k"})
    with pytest.raises(ValueError):
        llm_store.save_llm_config({"nope": {"api_key": "k"}})


def test_llm_config_masks_and_reports_configured(store) -> None:
    llm_store.save_slot("summary", {"api_key": "REAL-SUMMARY-KEY", "base_url": "https://s", "model": "m1"})
    cfg = llm_store.llm_config()
    assert set(cfg.keys()) == {"summary", "asr"}
    assert cfg["summary"]["configured"] is True
    assert cfg["summary"]["api_key"] == "REAL****-KEY"
    assert cfg["asr"]["configured"] is False
    # 真实 Key 绝不出现在对外视图
    assert "REAL-SUMMARY-KEY" not in str(cfg)


def test_import_from_env_keeps_existing_when_no_env_key(store, monkeypatch) -> None:
    llm_store.save_slot(
        "summary",
        {"api_key": "EXISTING-REAL-KEY", "base_url": "https://keep.example", "model": "keep-model"},
    )
    # env 只提供 base_url、无任何 Key：不得覆盖既有配置
    monkeypatch.setattr(llm_store, "_env", lambda: {"LLM_BASE_URL": "https://env.example"})
    llm_store.import_from_env()
    stored = llm_store.get_slot("summary")
    assert stored["api_key"] == "EXISTING-REAL-KEY"
    assert stored["base_url"] == "https://keep.example"


def test_import_from_env_fills_slots_with_env_key(store, monkeypatch) -> None:
    monkeypatch.setattr(
        llm_store,
        "_env",
        lambda: {
            "LLM_API_KEY": "NEW-KEY",
            "LLM_BASE_URL": "https://new.example",
            "LLM_MODEL": "new-model",
            "ASR_BASE_URL": "https://asr.example",
            "ASR_MODEL": "asr-model",
        },
    )
    result = llm_store.import_from_env()
    assert result["summary"]["configured"] is True
    assert result["asr"]["configured"] is True
    assert llm_store.get_slot("summary")["model"] == "new-model"
    assert llm_store.get_slot("asr")["base_url"] == "https://asr.example"
    assert llm_store.get_slot("asr")["model"] == "asr-model"


def test_resolve_llm_kwargs_ignores_masked_fallback(store) -> None:
    """槽位已配置（有 Key）→ 槽位为准：掩码 Key 永不生效，env 也不再覆盖已配置键。"""
    llm_store.save_slot(
        "summary",
        {"api_key": "REAL-SERVER-KEY", "base_url": "https://profile.example", "model": "profile-model"},
    )
    cfg = llm_store.resolve_llm_kwargs(
        "summary", {"api_key": "MASKED****VALUE", "base_url": "", "model": "override-model"}
    )
    assert cfg["api_key"] == "REAL-SERVER-KEY"
    assert cfg["model"] == "profile-model"
    assert cfg["base_url"] == "https://profile.example"


def test_resolve_llm_kwargs_slot_ready_env_fills_gaps(store) -> None:
    """槽位有 Key 但缺 model → env fallback 只补缺失键（典型：.env 只给了 Key）。"""
    llm_store.save_slot("summary", {"api_key": "REAL-SERVER-KEY", "base_url": "https://profile.example"})
    cfg = llm_store.resolve_llm_kwargs("summary", {"model": "env-model"})
    assert cfg["api_key"] == "REAL-SERVER-KEY"
    assert cfg["model"] == "env-model"


def test_resolve_llm_kwargs_without_slot_uses_real_fallback(store) -> None:
    cfg = llm_store.resolve_llm_kwargs(
        "summary", {"api_key": "REAL-USER-KEY", "base_url": "https://u", "model": "m"}
    )
    assert cfg["api_key"] == "REAL-USER-KEY"


def test_polish_shares_summary_slot(store) -> None:
    """润色不再有独立模型：polish 用途直接复用推理模型槽位。"""
    llm_store.save_slot("summary", {"api_key": "KEY-SUM", "base_url": "https://s", "model": "sum-model"})
    cfg = llm_store.resolve_llm_kwargs("polish", None)
    assert cfg["api_key"] == "KEY-SUM"
    assert cfg["model"] == "sum-model"


def test_short_key_fully_masked_and_preserved(store) -> None:
    """短 Key（≤8 字符）全掩码：不外露任何片段，掩码回传仍保留真实 Key。"""
    llm_store.save_slot("summary", {"api_key": "abc", "base_url": "https://x", "model": "m"})
    masked = llm_store.mask_api_key(llm_store.get_slot("summary"))
    assert masked["api_key"] == "****"
    assert "abc" not in masked["api_key"]
    # 掩码回传 → 既有真实 Key 保留
    llm_store.save_slot("summary", {"api_key": masked["api_key"]})
    assert llm_store.get_slot("summary")["api_key"] == "abc"


def test_slot_fallback_reads_legacy_profile_ids(store) -> None:
    """旧库兼容：历史 profile id（如 default-summary）也能被槽位解析免迁移读取。"""
    from video_to_summary import crypto, db

    db.init_db()
    # 模拟真实旧库：没有 id=purpose 的播种行（播种只在空库发生，旧库有配置即跳过）
    with db.get_conn() as conn:
        conn.execute("DELETE FROM llm_profiles WHERE purpose='summary'")
    enc = crypto.encrypt_secret("LEGACY-KEY-123456")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO llm_profiles"
            " (id, name, purpose, provider, api_key, base_url, model, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            ("default-summary", "默认总结", "summary", "openai",
             enc, "https://old.example", "old-model", 1, 1),
        )

    slot = llm_store.get_slot("summary")
    assert slot["api_key"] == "LEGACY-KEY-123456"
    assert slot["base_url"] == "https://old.example"

    # save_slot 原地更新旧行（沿用旧 id），不另立新槽位行
    llm_store.save_slot("summary", {"api_key": "NEW-KEY", "model": "m"})
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM llm_profiles WHERE purpose='summary'"
        ).fetchall()
    assert [r["id"] for r in rows] == ["default-summary"]
    assert llm_store.get_slot("summary")["api_key"] == "NEW-KEY"
