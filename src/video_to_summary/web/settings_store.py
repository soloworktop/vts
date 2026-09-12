"""应用级全局配置（存 SQLite meta 表，供 Web UI 修改）。

设计：**所有任务配置只放全局**，创建任务不支持单任务覆盖（唯一例外是总结模板）。
- 「任务默认配置」：summary_template / audio_format / polish_preset /
  subtitle_preference / subtitle_language / cookies_browser / proxy，
  存为 meta 键 ``default_<name>``；文本优化开关单独存 ``polish_transcript``。
- 优先级：DB 全局设置 > 环境变量 ``POLISH_TRANSCRIPT``（仅文本优化）> 内置默认值。
- CLI 保留自身参数（见 main.py），不读 DB 全局设置。
- LLM 接入信息（base_url / api_key / model）不在这里，走 ``web/llm_store.py``（BYOK）。
"""

import os

from .. import db

# 内置默认值（name -> 默认值）
_JOB_DEFAULTS: dict[str, str] = {
    "summary_template": "通用",
    "audio_format": "mp3",  # Web 默认 mp3（文件更小）；CLI 默认 wav（config.py，ASR 质量优先）——有意差异
    "polish_preset": "default",
    "subtitle_preference": "auto",  # auto | manual_only | off
    "subtitle_language": "auto",
    "cookies_browser": "",  # 空 = 不使用；yt-dlp cookies-from-browser 浏览器名
    "proxy": "",  # 空 = 自动（环境变量/直连）；如 http://127.0.0.1:7897
}
_POLISH_KEY = "polish_transcript"

# 浏览器 cookies 白名单（yt-dlp cookies-from-browser 支持的浏览器；空串 = 关闭）。
# 值会原样传给 yt-dlp，白名单同时挡住任意字符串注入
COOKIES_BROWSERS: set[str] = {
    "chrome", "chromium", "edge", "brave", "vivaldi", "opera", "whale", "firefox", "safari",
}

# 代理 URL 允许的 scheme 前缀（yt-dlp 支持），拦住明显的手滑输入
_PROXY_SCHEMES = ("http://", "https://", "socks4://", "socks5://", "socks5h://")


def _parse_bool(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def get_default_polish() -> bool:
    stored = db.get_setting(_POLISH_KEY)
    if stored is not None:
        return _parse_bool(stored)
    return _parse_bool(os.environ.get("POLISH_TRANSCRIPT", "0"))


def set_default_polish(enabled: bool) -> None:
    db.set_setting(_POLISH_KEY, "1" if enabled else "0")


def _get_default(name: str) -> str:
    stored = db.get_setting(f"default_{name}")
    return stored if stored is not None else str(_JOB_DEFAULTS[name])


def _set_default(name: str, value) -> None:
    db.set_setting(f"default_{name}", str(value))


def job_defaults() -> dict:
    """返回全部任务默认配置（UI 层级键）。"""
    return {
        **{name: _get_default(name) for name in _JOB_DEFAULTS},
        "polish_transcript": get_default_polish(),
    }


def set_job_defaults(values: dict) -> None:
    """持久化传入的任务默认配置（只处理已知键）。

    **先整体校验、再统一落库**：任何键非法（cookies_browser 非白名单 / proxy scheme
    不符）在写入前抛 ValueError（API 层转 400），不产生部分写入。

    ``cookies_browser`` 做白名单校验，空串 = 关闭浏览器 cookies。
    """
    cleaned: list[tuple[str, str]] = []
    for name, value in (values or {}).items():
        if name == "polish_transcript":
            cleaned.append((name, "1" if _parse_bool(value) else "0"))
        elif name == "cookies_browser":
            browser = str(value or "").strip().lower()
            if browser and browser not in COOKIES_BROWSERS:
                raise ValueError(
                    f"unsupported browser for cookies: {value!r} (可选: {', '.join(sorted(COOKIES_BROWSERS))})"
                )
            cleaned.append((name, browser))
        elif name == "proxy":
            proxy = str(value or "").strip()
            if proxy and not proxy.lower().startswith(_PROXY_SCHEMES):
                raise ValueError(
                    f"invalid proxy url: {value!r} (需以 {'/'.join(_PROXY_SCHEMES)} 开头，留空 = 自动)"
                )
            cleaned.append((name, proxy))
        elif name in _JOB_DEFAULTS:
            cleaned.append((name, str(value)))

    for name, value in cleaned:
        if name == "polish_transcript":
            db.set_setting(_POLISH_KEY, value)
        else:
            _set_default(name, value)


def job_defaults_payload() -> dict:
    """把全局默认配置转成任务 payload（web/tasks 消费的键）。

    开源版转写引擎唯一：**字幕优先**，无自带字幕时走 OpenAI 兼容 Whisper API
    （``whisper_api`` 恒为 True）。payload 保留该键以便历史任务快照自解释。
    """
    d = job_defaults()
    return {
        "summary_template": d["summary_template"],
        "audio_format": d["audio_format"],
        "whisper_api": True,
        "polish_transcript": d["polish_transcript"],
        "polish_preset": d["polish_preset"],
        "subtitle_preference": d["subtitle_preference"],
        "subtitle_language": d["subtitle_language"],
        "cookies_browser": d["cookies_browser"],
        "proxy": d["proxy"],
    }


__all__ = [
    "get_default_polish",
    "set_default_polish",
    "job_defaults",
    "set_job_defaults",
    "job_defaults_payload",
]
