"""日志导出（诊断包）：内存环形缓冲 + 导出时统一脱敏。

设计要点：
- **内存 ring buffer**：进程内保留最近 N 条格式化日志（含 ``logger.exception``
  的完整 traceback），Web/桌面共用同一进程，导出即快照；不做文件落盘
- **脱敏发生在导出边界**：内存中保留原文（仅进程内可见），导出的文本即对外
  交付物，天然干净；规则覆盖 API Key / Fernet 密文 / Bearer / cookie /
  查询参数 token / 用户主目录伪名化
- **逐段 fail-safe**：bundle 任一段构建失败不中断导出（写占位说明后继续）
"""

import logging
import platform as _platform
import re
import sys
from collections import deque
from datetime import datetime
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
RING_CAPACITY = 2000
REDACTED = "[REDACTED]"
# 只挂 root + "uvicorn"："uvicorn.error" 的记录会向上传播经过 "uvicorn" 的
# handler（随后被其 propagate=False 截停，不会到 root），再单独挂 uvicorn.error
# 会把同一行捕获两次（曾导致导出里 startup 日志成对重复）
RING_LOGGERS = ("", "uvicorn")


# ---------------------------------------------------------------- ring buffer

class InMemoryLogHandler(logging.Handler):
    """把格式化后的日志行存进有界 deque；emit 异常绝不向上抛。

    同一条 LogRecord 在传播链上会先后经过多个 logger（root/uvicorn/...）的
    handler，按记录对象去重，保证任何配置下每个日志事件只收一次。
    """

    def __init__(self, capacity: int = RING_CAPACITY) -> None:
        super().__init__()
        self.buffer: deque[str] = deque(maxlen=capacity)
        self.setFormatter(logging.Formatter(LOG_FORMAT))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if getattr(record, "_vts_ring_seen", False):
                return  # 传播链上重复到达的同一条记录
            record._vts_ring_seen = True
            self.buffer.append(self.format(record))
        except Exception:  # noqa: BLE001 - 日志采集失败不能影响业务
            self.handleError(record)


_ring_handler: InMemoryLogHandler | None = None


def attach_ring_buffer() -> InMemoryLogHandler:
    """把 ring buffer 挂到 root 与 uvicorn logger；重复调用幂等。

    只挂两处：应用日志经 root 捕获；uvicorn 自身 `propagate=False`，其
    `uvicorn.error` 子 logger 的记录向上传播经过 "uvicorn" 的 handler 被
    捕获一次后即被截停——因此无需也不应再挂 `uvicorn.error`（会重复捕获）。
    若 logger 上已存在 ring handler（如 lifespan 每次启动都调用），直接复用，
    避免重复挂载或返回孤儿实例。
    """
    global _ring_handler
    if _ring_handler is not None:
        return _ring_handler
    for logger_name in RING_LOGGERS:
        for existing in logging.getLogger(logger_name).handlers:
            if isinstance(existing, InMemoryLogHandler):
                _ring_handler = existing
                return existing
    handler = InMemoryLogHandler()
    for logger_name in RING_LOGGERS:
        logging.getLogger(logger_name).addHandler(handler)
    _ring_handler = handler
    return handler


def get_ring_handler() -> InMemoryLogHandler | None:
    return _ring_handler


# ---------------------------------------------------------------- sanitizer

def _home_patterns() -> list[str]:
    """当前用户主目录的各种形态（原始 / 展开 / resolve 后），长的在前。"""
    homes: set[str] = set()
    raw = Path.home()
    homes.add(str(raw))
    try:
        homes.add(str(raw.resolve()))
    except OSError:  # pragma: no cover - resolve 失败时保留原始形态
        pass
    import os

    homes.add(os.path.expanduser("~"))
    return sorted((h for h in homes if h and h != "/" and h != "\\"), key=len, reverse=True)


# URL 内嵌凭据（http://user:pass@host）：base_url / proxy 带认证时，httpx/openai
# SDK 的 INFO 访问日志（"HTTP Request: POST http://user:pass@host/v1/..."）会原样
# 回显完整 URL——str(request.url) 保留 userinfo，必须打码
_URL_CRED_RE = re.compile(r"//([^/@\s:]+):([^/@\s]+)@")


def scrub_url_credentials(text: str) -> str:
    """把 URL userinfo 形态的凭据（``user:pass@host``）统一打码为 ``user:***@``。

    脱敏的唯一实现点：诊断导出（sanitize_text）与对外错误文案（web/tasks 的
    _user_facing_error）共用本函数，避免规则漂移。
    """
    return _URL_CRED_RE.sub(r"//\1:***@", text)


def sanitize_text(text: str) -> str:
    """对导出文本统一脱敏：Key/密文/token/cookie/主目录。

    规则只做「删敏」不做「改义」：时间戳、层级、消息结构原样保留，
    保证证据链可读。
    """
    out = text
    # 1. 主目录伪名化（保留相对结构；_home_patterns 已按长度降序，避免前缀截断）
    for home in _home_patterns():
        out = out.replace(home, "~")
    # 2. OpenAI 风格 Key
    out = re.sub(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{8,}", f"sk-{REDACTED}", out)
    # 3. Fernet 密文（crypto.py 的 enc:v1: 前缀）
    out = re.sub(r"enc:v1:[A-Za-z0-9+/=_\-]{8,}", f"enc:v1:{REDACTED}", out)
    # 4. Bearer / 直写 token 头
    out = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{4,}", r"\1" + REDACTED, out)
    # 5. 查询参数中的敏感键（含 B 站 SESSDATA / 签名类参数 / 分享者追踪 ID）；\b 防误伤
    out = re.sub(
        r"(?i)\b((?:token|key|sessdata|access_key|sign(?:ature)?|wts|w_rid|appkey|vd_source)=)[^&\s'\"<>]+",
        r"\1" + REDACTED,
        out,
    )
    # 6. Cookie 头整值（SESSDATA 等都在其中）
    out = re.sub(r"(?i)^(\s*(?:set-)?cookie:\s*).+$", r"\1" + REDACTED, out, flags=re.MULTILINE)
    # 7. 通用敏感赋值（api_key=xxx / SECRET: xxx / SESSDATA: xxx——dict/冒号形态
    #    不带 "Cookie:" 行首时由这里兜底）。值匹配：带引号的串 或 不含 &;'\"<> 的
    #    裸 token——不能放 \S+，否则在 URL 上下文里会吞掉 & 后续参数（如已脱敏的
    #    wts=[REDACTED]&x=2 一并被改写）
    out = re.sub(
        r"(?i)(\b(?:api_?key|secret|password|passphrase|sessdata|bili_jct|dedeuserid)\b['\"]?\s*[:=]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s&;'\"<>]+)",
        r"\1" + REDACTED,
        out,
    )
    # 8. 认证头的非 Bearer 形态（Basic / API-Key 等整值打码；Bearer 行已由规则 4 处理）。
    #    bearer 排除必须用 `.*\bbearer\b` 全行扫描——`(?!bearer\b)` 紧跟 `\s*` 会被
    #    回宣传绕（\s* 吐回空格后前瞻位置上不是 bearer，整行被误打码）
    out = re.sub(
        r"(?im)^(\s*(?:authorization|x-api-key|api-key)\s*:\s*)(?!.*\bbearer\b).+$",
        r"\1" + REDACTED,
        out,
    )
    # 9. token 族下划线形态（access_token= / refresh_token= / id_token=——规则 5 的
    #    \btoken= 因下划线属 word 字符、无词边界而覆盖不到）
    out = re.sub(
        r"(?i)\b((?:access|refresh|id)_token=)[^&\s'\"<>]+",
        r"\1" + REDACTED,
        out,
    )
    # 10. URL userinfo 凭据（http://user:pass@host）
    out = scrub_url_credentials(out)
    return out


# ---------------------------------------------------------------- bundle

def _app_version() -> str:
    try:
        from importlib.metadata import version

        return version("video-to-summary")
    except Exception:  # noqa: BLE001 - 元数据缺失不阻断导出
        return "unknown"


def _schema_version() -> str:
    try:
        from video_to_summary.db import SCHEMA_VERSION

        return str(SCHEMA_VERSION)
    except Exception:  # noqa: BLE001
        return "unknown"


def _section(title: str, render) -> str:
    """段级 fail-safe：渲染异常时写占位说明，不中断整包（标题始终存在）。"""
    try:
        body = render()
    except Exception as exc:  # noqa: BLE001
        body = f"(section unavailable: {type(exc).__name__}: {exc})"
    return f"=== {title} ===\n{body}"


def _fmt_ts(value) -> str:
    """epoch 秒 → 本地时间 ISO 字符串；非数值原样返回（与日志时间线可对齐）。"""
    try:
        return datetime.fromtimestamp(float(value)).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError, OverflowError):
        return str(value or "")


def _single_line(value) -> str:
    """单行化：折叠 CR/LF，防止字段内容注入伪造段落标题或破坏行结构。"""
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()


def _render_jobs() -> str:
    from video_to_summary.web.tasks import list_jobs

    jobs = list_jobs(limit=50)
    lines = [f"共 {len(jobs)} 条（最多 50）"]
    for j in jobs:
        # error 由 list_jobs 直接带出（单查询，无 N+1）；截断防超长错误撑大导出
        error = _single_line(j.get("error") or "")[:200]
        lines.append(
            " | ".join(
                [
                    f"id={str(j.get('job_id', ''))[:8]}",
                    f"type={j.get('source_type', '')}",
                    f"status={j.get('status', '')}",
                    f"title={_single_line(j.get('title'))}",
                    f"created={_fmt_ts(j.get('created_at'))}",
                    f"retry={j.get('retry_count', 0)}",
                    f"error={error}",
                ]
            )
        )
    if not jobs:
        lines.append("(无任务记录)")
    return "\n".join(lines) + "\n"


def _render_logs() -> str:
    handler = get_ring_handler()
    if handler is None:
        return "(ring buffer 未挂载，无运行日志)\n"
    lines = list(handler.buffer)
    if not lines:
        return "(暂无运行日志)\n"
    head = f"共 {len(lines)} 条（容量 {handler.buffer.maxlen}，超出部分已轮转淘汰）"
    # 每条日志记录的每一行物理行前都加前导空格：异常消息里嵌入的换行（攻击者
    # 可控）也会被逐行缩进，无法以列 0 的 '===' 伪造段落标题（标题由本模块生成）
    indented = ["\n".join(" " + ln for ln in line.splitlines()) for line in lines]
    return head + "\n" + "\n".join(indented) + "\n"


def build_log_bundle() -> str:
    """组装脱敏诊断包：环境头 + 最近任务 + 运行日志。"""
    handler = get_ring_handler()
    count = len(handler.buffer) if handler else 0
    header = "\n".join(
        [
            "=== video-to-summary 诊断日志 ===",
            f"导出时间: {datetime.now().isoformat(timespec='seconds')}",
            f"应用版本: {_app_version()}",
            f"平台: {_platform.platform()}",
            f"Python: {sys.version.split()[0]}",
            f"DB schema: {_schema_version()}",
            f"运行日志条数: {count}",
            "说明: 本文件已自动脱敏（API Key/密文/token/cookie/主目录路径）",
        ]
    )
    bundle = "\n\n".join(
        [
            header,
            _section("最近任务（来自本地数据库，已脱敏）", _render_jobs),
            _section(f"运行日志（最近 {RING_CAPACITY} 条，已脱敏）", _render_logs),
        ]
    )
    return sanitize_text(bundle) + "\n"


def export_filename(now: datetime | None = None) -> str:
    """导出文件名（ASCII 安全，浏览器下载用）。"""
    ts = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"video-to-summary-logs-{ts}.txt"


__all__ = [
    "LOG_FORMAT",
    "RING_CAPACITY",
    "REDACTED",
    "InMemoryLogHandler",
    "attach_ring_buffer",
    "get_ring_handler",
    "sanitize_text",
    "scrub_url_credentials",
    "build_log_bundle",
    "export_filename",
]
