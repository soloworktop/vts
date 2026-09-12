"""总结模板存储：内置模板（代码内 ``SUMMARY_TEMPLATES``）+ 自定义模板（SQLite）。

v4 模型：模板 = 一段提示词 ``{"prompt": "..."}``，不再有章节三层结构。
排版美化（emoji 小标题/加粗/引用块/受限着色）由 summarizers 的排版契约统一保证，
提示词只描述「总结什么、怎么组织」。

读取优先级：DB 自定义 > 内置（同名时 DB 覆盖内置，删除自定义即恢复内置）。
列表顺序：内置按声明序（视频使用场景频率）在前，自定义按名称排后。
"""

import time

from video_to_summary.summarizers.openai import (
    DEFAULT_TEMPLATE_NAME,
    SUMMARY_TEMPLATES,
    TEMPLATE_ALIASES,
)

from .. import db


def list_templates() -> list[str]:
    """模板名列表：内置模板按声明序（视频使用场景频率：通用/摘要 → 学习 → 创作垂类）
    在前，纯自定义模板按名称排序追加在后。被 DB 覆盖的同名内置模板仍占内置槽位
    （只出现一次，读取时按「DB 自定义 > 内置」解析）。兼容别名 "default" 不在列表中。"""
    db.init_db()
    with db.get_conn() as conn:
        rows = conn.execute("SELECT name FROM summary_templates").fetchall()
    custom_names = {row["name"] for row in rows}
    builtins = list(SUMMARY_TEMPLATES)
    extras = sorted(name for name in custom_names if name not in SUMMARY_TEMPLATES)
    return builtins + extras


def get_template(name: str) -> dict:
    """返回 {"prompt": "..."}；未知模板抛 ValueError（调用方应回落通用）。

    兼容别名（如历史名 "default"）解析到对应内置模板；DB 自定义 > 内置。"""
    db.init_db()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT prompt FROM summary_templates WHERE name = ?",
            (name,),
        ).fetchone()
    if row is not None:
        return {"prompt": row["prompt"] or SUMMARY_TEMPLATES[DEFAULT_TEMPLATE_NAME]["prompt"]}
    key = TEMPLATE_ALIASES.get(name, name)
    if key in SUMMARY_TEMPLATES:
        return dict(SUMMARY_TEMPLATES[key])
    raise ValueError(f"Unknown summary template: {name}")


def save_template(name: str, prompt: str) -> dict:
    """新建或覆盖同名自定义模板；内置模板（含兼容别名）只读，覆盖抛 ValueError。"""
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    _ensure_not_builtin(name)
    db.init_db()
    now = time.time()
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO summary_templates
                   (name, prompt, created_at, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                   prompt = excluded.prompt,
                   updated_at = excluded.updated_at""",
            (name, prompt, now, now),
        )
    return {"prompt": prompt}


def delete_template(name: str) -> bool:
    """删除自定义模板；内置模板（含兼容别名）不可删除，抛 ValueError。
    返回是否确实删除了 DB 行。"""
    _ensure_not_builtin(name)
    db.init_db()
    with db.get_conn() as conn:
        cur = conn.execute("DELETE FROM summary_templates WHERE name = ?", (name,))
    return cur.rowcount > 0


def _ensure_not_builtin(name: str) -> None:
    """内置模板只读：写入/删除内置名（含历史别名）一律拒绝。"""
    from video_to_summary.summarizers.openai import SUMMARY_TEMPLATES, TEMPLATE_ALIASES

    key = TEMPLATE_ALIASES.get(name, name)
    if key in SUMMARY_TEMPLATES:
        raise ValueError(f"内置模板「{key}」不可编辑或删除；如需类似模板请另存新名称")


__all__ = ["list_templates", "get_template", "save_template", "delete_template"]
