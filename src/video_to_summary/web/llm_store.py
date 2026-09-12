"""LLM 配置存储（简化两槽位）：推理模型（summary）+ 语音识别模型（asr）。

设计：
- 没有多 profile、没有路由：每个用途（summary / asr）各一个固定槽位，
  base_url / api_key / model 三元组即全部配置；润色（polish）复用推理模型。
- 复用既有 ``llm_profiles`` 表（每用途一行，id = purpose），旧库（含
  default-summary 等历史 profile）无需迁移即可读取。
- API Key 经 crypto.py（Fernet）加密落库；对外展示一律经 mask_api_key 掩码。
  掩码占位或空 Key 回传时保留既有真实 Key，避免前端往返覆盖。
- 兼容读取：槽位解析优先 id == purpose 的行，否则取该 purpose 下最早创建的
  profile（兼容旧版 default-* 命名与历史路由配置）。
"""

import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv

from .. import crypto, db

logger = logging.getLogger("video_to_summary.web.llm_store")

# 固定两槽位：summary = 推理模型（总结与文本润色），asr = 语音识别模型
PURPOSES = ("summary", "asr")


def _is_masked(value) -> bool:
    """判断是否为掩码占位值（前端展示用），保存时应保留原 Key。"""
    return isinstance(value, str) and "****" in value


def _env() -> dict:
    load_dotenv(dotenv_path=Path(".env"))
    return dict(os.environ)


def _decrypt_key(row) -> str:
    try:
        return crypto.decrypt_secret(row["api_key"])
    except crypto.SecretDecryptError as exc:
        # 密文解不开（密钥轮转/损坏）：标记未配置，避免把密文当明文 Key 使用
        logger.warning("slot %s: API Key 无法解密（%s），视为未配置", row["purpose"], exc)
        return ""


def _slot_row(purpose: str, conn) -> dict | None:
    """定位某用途的槽位 profile：id == purpose 优先，否则该用途最早创建的行。"""
    row = conn.execute(
        "SELECT * FROM llm_profiles WHERE id = ?", (purpose,)
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM llm_profiles WHERE purpose = ? ORDER BY created_at, id LIMIT 1",
            (purpose,),
        ).fetchone()
    return row


def mask_api_key(slot: dict) -> dict:
    if not slot:
        return slot
    masked = dict(slot)
    key = masked.get("api_key")
    if isinstance(key, str) and key:
        # 短 Key（≤8 字符）无法安全保留首尾片段，全掩码——任何真实 Key 片段都不外露
        if len(key) > 8:
            masked["api_key"] = key[:4] + "****" + key[-4:]
        else:
            masked["api_key"] = "****"
    return masked


def _slot_row_to_dict(row) -> dict | None:
    if row is None:
        return None
    return {
        "purpose": row["purpose"],
        "provider": row["provider"],
        "api_key": _decrypt_key(row),
        "base_url": row["base_url"],
        "model": row["model"],
    }


def get_slot(purpose: str) -> dict | None:
    """读取槽位真实配置（含真实 Key，仅服务端内部使用）。"""
    if purpose not in PURPOSES:
        raise ValueError(f"invalid purpose: {purpose}, must be one of {', '.join(PURPOSES)}")
    db.init_db()
    with db.get_conn() as conn:
        return _slot_row_to_dict(_slot_row(purpose, conn))


def save_slot(purpose: str, payload: dict) -> dict:
    """写入槽位。掩码占位或空 Key 回传时保留既有真实 Key（前端往返不覆盖）。"""
    if purpose not in PURPOSES:
        raise ValueError(f"invalid purpose: {purpose}, must be one of {', '.join(PURPOSES)}")
    db.init_db()
    with db.get_conn() as conn:
        row = _slot_row(purpose, conn)
        existing = _slot_row_to_dict(row) or {}
        row_id = row["id"] if row is not None else purpose

        api_key = payload.get("api_key") or ""
        if _is_masked(api_key) or not api_key:
            api_key = existing.get("api_key", "")

        base_url = (
            payload["base_url"]
            if payload.get("base_url") is not None
            else existing.get("base_url", "")
        )
        model = (
            payload["model"]
            if payload.get("model") is not None
            else existing.get("model", "")
        )
        provider = "openai"

        now = time.time()
        conn.execute(
            """INSERT INTO llm_profiles
                   (id, name, purpose, provider, api_key, base_url, model, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    api_key = excluded.api_key,
                    base_url = excluded.base_url,
                    model = excluded.model,
                    updated_at = excluded.updated_at""",
            (row_id, purpose, purpose, provider, crypto.encrypt_secret(api_key), base_url, model, now, now),
        )
    return get_slot(purpose) or {}


def llm_config() -> dict:
    """两个槽位的对外视图（Key 掩码 + configured 布尔，不含真实 Key）。"""
    out: dict = {}
    for purpose in PURPOSES:
        slot = get_slot(purpose) or {}
        masked = mask_api_key(slot)
        masked["configured"] = bool(slot.get("api_key"))
        out[purpose] = {
            "base_url": masked.get("base_url", ""),
            "api_key": masked.get("api_key", ""),
            "model": masked.get("model", ""),
            "configured": masked["configured"],
        }
    return out


def save_llm_config(payload: dict) -> dict:
    """部分更新槽位：payload 只需携带要修改的用途。"""
    for purpose, slot in (payload or {}).items():
        if purpose not in PURPOSES:
            raise ValueError(f"invalid purpose: {purpose}, must be one of {', '.join(PURPOSES)}")
        if not isinstance(slot, dict):
            raise ValueError(f"slot {purpose} must be an object")
        save_slot(purpose, slot)
    return llm_config()


def resolve_llm_kwargs_map(fallbacks: dict[str, dict | None]) -> dict[str, dict]:
    """批量解析多用途 LLM 配置（一次读库）。

    用途 → 槽位：asr 用语音识别槽位；summary 与 polish 共用推理槽位。
    合并优先级：
    - **summary / asr**：槽位已配置（api_key 非空）→ 槽位为准，fallback 只补
      槽位缺失的键（否则 .env 老用户在界面上怎么改都不会生效）；槽位未配置 →
      完全由 fallback 决定（兼容纯 .env 用户，UI 不填不改变行为）。
    - **polish**：无独立槽位，复用推理槽位后由 fallback（``POLISH_MODEL`` /
      ``POLISH_BASE_URL`` 环境变量）覆盖——该 env 覆盖契约保持不变。
    掩码占位值一律忽略，避免把掩码 Key 传给 LLM 客户端。
    """
    db.init_db()
    with db.get_conn() as conn:
        slots = {purpose: _slot_row(purpose, conn) for purpose in PURPOSES}

    def slot_for(purpose: str) -> dict | None:
        # polish 复用推理模型槽位；其余用途各用各的
        return _slot_row_to_dict(slots.get("asr" if purpose == "asr" else "summary"))

    def apply_fallback(resolved: dict, fallback: dict | None, *, override: bool) -> None:
        if not fallback:
            return
        for key in ("api_key", "base_url", "model"):
            val = fallback.get(key)
            if val and not _is_masked(val) and (override or not resolved.get(key)):
                resolved[key] = val

    resolved_all: dict[str, dict] = {}
    for purpose, fallback in fallbacks.items():
        resolved: dict = {}
        profile = slot_for(purpose)
        slot_ready = bool(profile and profile.get("api_key"))
        if slot_ready and profile:
            for key in ("api_key", "base_url", "model"):
                if profile.get(key):
                    resolved[key] = profile[key]
        # 槽位未配置 → env fallback 决定（等价旧"仅 env"行为）；
        # polish 无槽位 → env 覆盖契约保持；槽位已配置 → env 仅补空缺
        apply_fallback(resolved, fallback, override=not slot_ready or purpose == "polish")
        resolved_all[purpose] = resolved
    return resolved_all


def resolve_llm_kwargs(purpose: str, fallback: dict | None = None) -> dict:
    """单一用途解析（语义见 resolve_llm_kwargs_map）。"""
    return resolve_llm_kwargs_map({purpose: fallback})[purpose]


def has_configured_key() -> bool:
    """是否任一槽位填入了真实 API Key（BYOK 首跑引导 / 健康状态用）。

    开源版「未配置 Key」是常态路径而不是故障：前端据此显示引导，而非报错。
    只返回布尔，不泄露任何 Key 片段。
    """
    db.init_db()
    with db.get_conn() as conn:
        for purpose in PURPOSES:
            row = _slot_row(purpose, conn)
            if row is None:
                continue
            try:
                if crypto.decrypt_secret(row["api_key"]):
                    return True
            except crypto.SecretDecryptError:
                continue  # 密文解不开视为未配置，不把密文当 Key 用
    return False


def import_from_env(dotenv_path: str | None = None) -> dict:
    """从 ``.env`` 导入两槽位配置；env 未提供 Key 的槽位保持原样（绝不清空）。"""
    if dotenv_path:
        load_dotenv(dotenv_path=Path(dotenv_path))
    env = _env()

    env_key = env.get("LLM_API_KEY") or env.get("OPENAI_API_KEY") or ""
    if env_key:
        save_slot("summary", {
            "api_key": env_key,
            "base_url": env.get("LLM_BASE_URL") or "",
            "model": env.get("LLM_MODEL") or "",
        })

    asr_key = env.get("OPENAI_API_KEY") or env.get("LLM_API_KEY") or ""
    if asr_key:
        save_slot("asr", {
            "api_key": asr_key,
            "base_url": env.get("ASR_BASE_URL") or "",
            "model": env.get("ASR_MODEL") or "",
        })

    return llm_config()


__all__ = [
    "PURPOSES",
    "get_slot",
    "save_slot",
    "llm_config",
    "save_llm_config",
    "mask_api_key",
    "has_configured_key",
    "resolve_llm_kwargs",
    "resolve_llm_kwargs_map",
    "import_from_env",
]
