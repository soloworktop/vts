"""log_export 单元测试：ring buffer / 脱敏规则 / 诊断包组装与 fail-safe。

`fresh_ring` fixture 定义在 `tests/conftest.py`（与 test_web 的日志导出用例共享）。
"""

import logging
import re
from pathlib import Path

import pytest

from video_to_summary import log_export
from video_to_summary.log_export import (
    InMemoryLogHandler,
    attach_ring_buffer,
    build_log_bundle,
    export_filename,
    sanitize_text,
)

RING_LOGGERS = ("", "uvicorn")


def test_attach_ring_buffer_idempotent(fresh_ring):
    h1 = attach_ring_buffer()
    h2 = attach_ring_buffer()
    assert h1 is h2
    root_handlers = [h for h in logging.getLogger().handlers if isinstance(h, InMemoryLogHandler)]
    assert len(root_handlers) == 1
    attached = [h for h in logging.getLogger("uvicorn").handlers if isinstance(h, InMemoryLogHandler)]
    assert len(attached) == 1


def test_attach_ring_buffer_no_duplicate_capture(fresh_ring):
    """同一条 LogRecord 经传播链多次到达 handler 时只收一次。

    回归背景：ring 曾同时挂 uvicorn 与 uvicorn.error，导出里 startup 日志成对重复。
    这里构造最棘手的裸进程场景（无 uvicorn LOGGING_CONFIG，"uvicorn" propagate=True，
    同一记录会先后经过 uvicorn 与 root 的 handler），handler 内按记录对象去重；
    生产环境（uvicorn propagate=False）只会到达一处，天然单次。
    """
    attach_ring_buffer()
    handler = log_export.get_ring_handler()
    loggers = [logging.getLogger(n) for n in ("uvicorn.error", "video_to_summary.test")]
    for lg in loggers:
        lg.setLevel(logging.INFO)
    try:
        logging.getLogger("uvicorn.error").info("Application startup complete.")
        logging.getLogger("video_to_summary.test").info("app line once")
    finally:
        for lg in loggers:
            lg.setLevel(logging.NOTSET)
    joined = "\n".join(handler.buffer)
    assert joined.count("Application startup complete.") == 1
    assert joined.count("app line once") == 1


def test_ring_buffer_captures_traceback(fresh_ring):
    attach_ring_buffer()
    logger = logging.getLogger("test.log_export.traceback")
    try:
        raise RuntimeError("boom-for-evidence")
    except RuntimeError:
        logger.exception("job failed")
    joined = "\n".join(log_export.get_ring_handler().buffer)
    assert "job failed" in joined
    assert "Traceback (most recent call last)" in joined
    assert "boom-for-evidence" in joined


def test_ring_buffer_rotation_drops_oldest():
    handler = InMemoryLogHandler(capacity=5)
    logger = logging.getLogger("test.log_export.rotate")
    logger.propagate = False
    logger.handlers = [handler]
    try:
        for i in range(8):
            logger.error("record-%d", i)
    finally:
        logger.propagate = True
        logger.handlers = []
    assert len(handler.buffer) == 5
    lines = list(handler.buffer)
    assert "record-3" in lines[0]  # 最旧的 0-2 已被轮转淘汰
    assert "record-7" in lines[-1]


def test_sanitize_rules():
    text = "\n".join(
        [
            "Authorization: Bearer abc123def456",
            "GET https://api.test/v1?token=tok123&x=1 HTTP/1.1",
            "url=https://passport.bilibili.com/x?SESSDATA=abcd1234&wts=456&x=2",
            "title=https://www.bilibili.com/video/BV1xx/?spm_id_from=333.5&vd_source=1ec97647767a1e6527541c3a5de40eca",
            "openai key sk-abcdefgh12345678 and sk-proj-ZZZZZZZZZZ",
            "stored enc:v1:AbCdEf123456-_+/==",
            "Set-Cookie: SESSDATA=xxx; Path=/",
            "Cookie: SESSDATA=yyy",
            "config api_key = mysecret123",
            f"audio at {Path.home() / 'Movies' / 'a.mp4'}",
            "monkey=1 不应被误伤",
        ]
    )
    out = sanitize_text(text)
    assert "abc123def456" not in out and "Bearer [REDACTED]" in out
    assert "token=[REDACTED]" in out and "x=1" in out  # 普通参数保留
    assert "SESSDATA=[REDACTED]" in out and "abcd1234" not in out
    assert "wts=[REDACTED]" in out
    assert "vd_source=[REDACTED]" in out and "1ec97647767a1e6527541c3a5de40eca" not in out
    assert "spm_id_from=333.5" in out  # 纯追踪码非身份标识，保留证据价值
    assert "sk-[REDACTED]" in out and "sk-abcdefgh12345678" not in out
    assert "enc:v1:[REDACTED]" in out and "AbCdEf123456" not in out
    assert "Set-Cookie: [REDACTED]" in out
    assert "api_key = [REDACTED]" in out and "mysecret123" not in out
    assert "~/Movies/a.mp4" in out and str(Path.home()) not in out
    assert "monkey=1" in out  # \b 防误伤


def test_build_bundle_sections_sanitized_and_no_leak(fresh_ring, tmp_path, monkeypatch):
    from video_to_summary import db as store_db

    # build_log_bundle 会读任务库：指到临时库，避免触碰真实 data/app.db
    monkeypatch.setattr(store_db, "DATABASE_PATH", tmp_path / "app.db")
    monkeypatch.setattr(store_db, "LEGACY_LLM_FILE", tmp_path / "no1.json")
    monkeypatch.setattr(store_db, "LEGACY_TEMPLATE_FILE", tmp_path / "no2.json")
    store_db.reset()

    attach_ring_buffer()
    logging.getLogger("test.log_export.bundle").error(
        "downloading %s failed with sk-abcdefgh12345678", Path.home() / "v.mp4"
    )

    bundle = build_log_bundle()
    assert "=== video-to-summary 诊断日志 ===" in bundle
    assert "应用版本:" in bundle and "平台:" in bundle and "DB schema:" in bundle
    assert "=== 最近任务" in bundle and "=== 运行日志" in bundle
    # 脱敏必须发生在导出边界：植入的假 Key/主目录不得出现在交付物中
    assert "sk-abcdefgh12345678" not in bundle
    assert "sk-[REDACTED]" in bundle
    assert str(Path.home()) not in bundle
    # 文件名格式（ASCII 安全）
    assert re.fullmatch(r"video-to-summary-logs-\d{8}-\d{6}\.txt", export_filename())


def test_build_bundle_formats_timestamps_and_failed_error(fresh_ring, tmp_path, monkeypatch):
    """任务段证据链：epoch → 可读本地时间；failed 任务补取脱敏后的失败原因。"""

    from video_to_summary import db as store_db
    from video_to_summary.web import tasks as web_tasks

    monkeypatch.setattr(store_db, "DATABASE_PATH", tmp_path / "app.db")
    monkeypatch.setattr(store_db, "LEGACY_LLM_FILE", tmp_path / "no1.json")
    monkeypatch.setattr(store_db, "LEGACY_TEMPLATE_FILE", tmp_path / "no2.json")
    store_db.reset()
    monkeypatch.setattr(
        web_tasks,
        "list_jobs",
        lambda limit=50: [
            {
                "job_id": "abc12345-full-id",
                "status": "failed",
                "title": "失败样例",
                "created_at": 1787844375.345696,
                "source_type": "url",
                "retry_count": 1,
                "error": "transcribe boom with sk-abcdefgh12345678",
            }
        ],
    )

    bundle = build_log_bundle()
    assert "created=2026-" in bundle  # epoch 已格式化为可读本地时间
    assert "1787844375" not in bundle
    assert "transcribe boom with sk-[REDACTED]" in bundle
    assert "sk-abcdefgh12345678" not in bundle


def test_build_bundle_fail_safe_keeps_going(fresh_ring, tmp_path, monkeypatch):
    from video_to_summary import db as store_db
    from video_to_summary.web import tasks as web_tasks

    monkeypatch.setattr(store_db, "DATABASE_PATH", tmp_path / "app.db")
    monkeypatch.setattr(store_db, "LEGACY_LLM_FILE", tmp_path / "no1.json")
    monkeypatch.setattr(store_db, "LEGACY_TEMPLATE_FILE", tmp_path / "no2.json")
    store_db.reset()
    monkeypatch.setattr(web_tasks, "list_jobs", lambda limit=50: (_ for _ in ()).throw(RuntimeError("db gone")))

    attach_ring_buffer()
    logging.getLogger("test.log_export.failsafe").error("still here")
    bundle = build_log_bundle()
    assert "section unavailable" in bundle  # 任务段失败有占位说明
    assert "=== 运行日志" in bundle  # 其余段照常
    assert "still here" in bundle


def test_log_newline_injection_cannot_fake_header(fresh_ring, tmp_path, monkeypatch):
    """异常消息嵌入换行伪造 '=== 标题' 不得生效：日志行带前导空格、字段单行化。"""
    from video_to_summary import db as store_db
    from video_to_summary.web import tasks as web_tasks

    monkeypatch.setattr(store_db, "DATABASE_PATH", tmp_path / "app.db")
    store_db.reset()
    monkeypatch.setattr(web_tasks, "list_jobs", lambda limit=50: [
        {"job_id": "abc", "status": "failed", "title": "恶意\n=== 伪造任务标题 ===",
         "created_at": 1.0, "source_type": "url", "retry_count": 0,
         "error": "boom\n=== 伪造错误 ==="},
    ])

    attach_ring_buffer()
    logging.getLogger("test.log_export.inject").error("pwned\n=== 伪造日志段落 ===")

    bundle = build_log_bundle()
    # 只有本模块生成的 3 个真实段落标题以列 0 的 '===' 开头；
    # 日志/字段里注入的伪造标题都因前导空格/单行化而不满足
    header_lines = [ln for ln in bundle.splitlines() if ln.startswith("===")]
    assert len(header_lines) == 3, header_lines
    for fake in ("=== 伪造日志段落 ===", "=== 伪造任务标题 ===", "=== 伪造错误 ==="):
        assert fake not in header_lines, fake


def test_sanitize_covers_bili_cookie_dict_forms():
    """B 站会话三件套的 dict/冒号形态（无 Cookie: 行首）也必须被脱敏。"""
    from video_to_summary.log_export import sanitize_text

    samples = [
        "session saved: {'SESSDATA': 'abc123def456'}",
        "SESSDATA: abc123def456",
        "cookies bili_jct=xyz789 appended",
        "dedeuserid: 12345",
    ]
    for s in samples:
        out = sanitize_text(s)
        assert "abc123def456" not in out, s
        assert "xyz789" not in out, s
        assert "REDACTED" in out, s
