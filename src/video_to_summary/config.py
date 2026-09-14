import logging
import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .constants import SubtitlePreference

logger = logging.getLogger("video_to_summary.config")

# 默认 LLM 接入点：通用 OpenAI 兼容占位（BYOK）。
# 本产品不内置任何中转/代收通道类服务——每个用户自备任意 OpenAI 兼容端点，
# 在「设置 → LLM 配置」里填 base_url / api_key / model 即可。
DEFAULT_LLM_BASE_URL = "https://api.openai.com/v1"

# OpenAI 兼容 Whisper API 的默认模型名（BYOK：可在 LLM 配置的 asr profile 里覆盖）
DEFAULT_ASR_API_MODEL = "whisper-1"


# 「LLM 文本优化」全局默认开关（环境变量）；Web 可用设置项覆盖，任务可单独覆盖。
# Settings.polish_transcript 用 default_factory 实时读取，避免 import 时固化。如:
#   POLISH_TRANSCRIPT=1 python -m video_to_summary.main <url>
def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


# 环境变量更名（槽位对称三元组）：推理槽 SUMMARY_API_KEY / SUMMARY_BASE_URL / SUMMARY_MODEL，
# 转写槽 ASR_API_KEY / ASR_BASE_URL / ASR_MODEL。旧名 LLM_* / OPENAI_API_KEY 长期保留为
# 兼容别名，兜底链统一为「新名 → 旧专名 → 通用旧名」；命中旧名时打一次 INFO 提示新名
#（进程内每个旧名至多一次，Web 常驻进程不刷屏）。
_LEGACY_ENV_HINTS: dict[str, str] = {
    "LLM_API_KEY": "SUMMARY_API_KEY",
    "LLM_BASE_URL": "SUMMARY_BASE_URL",
    "LLM_MODEL": "SUMMARY_MODEL",
    "OPENAI_API_KEY": "SUMMARY_API_KEY（推理）或 ASR_API_KEY（转写）",
}
_legacy_env_hinted: set[str] = set()


def first_env_value(lookup: Callable[[str], Optional[str]], *names: str) -> Optional[str]:
    """按序返回 lookup 中第一个非空的变量值；命中旧名时提示一次新名。

    Settings.__post_init__（``os.environ.get``）与 ``web/llm_store.import_from_env``
    （.env 合并后的 dict）共用，保证两条路径的别名兜底顺序一致。
    """
    for name in names:
        value = lookup(name)
        if not value:
            continue
        hint = _LEGACY_ENV_HINTS.get(name)
        if hint and name not in _legacy_env_hinted:
            _legacy_env_hinted.add(name)
            logger.info("环境变量 %s 已更名为 %s（旧名继续有效，建议迁移）", name, hint)
        return value
    return None


# VTS_USER_AGENT：非空时作为 yt-dlp 的 user_agent 并写入 http_headers["User-Agent"]
# （写进 ydl_opts，两处都设——B 站提取路径实测仅设 user_agent 不会替换请求头里的 UA，
# 见 sources/url._resolve_user_agent 的注释）；空/未设置 = 用 yt-dlp 默认 UA
# （绝大多数站点用默认 UA 是正常的，绝不改动默认行为）。
# 值只做基本清理（去空白/控制字符、限长），非法形态**不静默丢弃**：记录 WARNING 说明
# 发生了什么，让用户能发现配置没按预期生效（而不是把坏值静默传给 yt-dlp）。
_USER_AGENT_MAX_CHARS = 200
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")


def normalize_user_agent(value: Optional[str]) -> Optional[str]:
    """清洗 User-Agent 值；空/全空白/清洗后为空 → None（= 用 yt-dlp 默认 UA）。

    控制字符（换行等会破坏 HTTP 头）被移除、超长被截断，均打 WARNING；
    返回值可安全写进 ydl_opts["user_agent"]。
    """
    raw = (value or "").strip()
    if not raw:
        return None
    cleaned = _CONTROL_CHARS_RE.sub("", raw).strip()
    if cleaned != raw:
        logger.warning(
            "VTS_USER_AGENT 含控制字符/换行，已移除（%r → %r）；User-Agent 应是一行普通字符串",
            raw,
            cleaned,
        )
    if len(cleaned) > _USER_AGENT_MAX_CHARS:
        logger.warning(
            "VTS_USER_AGENT 超长（%d > %d 字符），已截断为前 %d 字符；若非预期请检查配置",
            len(cleaned),
            _USER_AGENT_MAX_CHARS,
            _USER_AGENT_MAX_CHARS,
        )
        cleaned = cleaned[:_USER_AGENT_MAX_CHARS]
    return cleaned or None

# 空库时自动播种的默认 LLM 配置（api_key/model 留空，base_url 为通用占位）：
# 只有两个槽位——推理模型（总结与文本润色）与语音识别模型，用户仅需填入 API Key 与模型名。
DEFAULT_LLM_PROFILES = [
    {
        "id": "summary",
        "name": "推理模型",
        "purpose": "summary",
        "provider": "openai",
        "base_url": DEFAULT_LLM_BASE_URL,
        "model": "",
    },
    {
        "id": "asr",
        "name": "语音识别模型",
        "purpose": "asr",
        "provider": "openai",
        "base_url": DEFAULT_LLM_BASE_URL,
        "model": "",
    },
]


@dataclass
class SubtitleConfig:
    """视频自带字幕的使用策略（来自全局设置 / CLI 参数）。

    - preference: SubtitlePreference.AUTO（默认）/ MANUAL_ONLY / OFF；
    - language: "auto"（优先中文，其次英文）或具体语言代码（如 zh / en / ja）。
    """

    preference: str = SubtitlePreference.AUTO
    language: str = "auto"


@dataclass
class Settings:
    url: str
    output_dir: Path = Path("output")
    whisper_api: bool = False
    # 转写槽位 Key：环境变量 ASR_API_KEY（兼容别名 OPENAI_API_KEY）
    asr_key: Optional[str] = None
    # 推理槽位 Key（总结与文本润色共用）：环境变量 SUMMARY_API_KEY（兼容别名 LLM_API_KEY）
    summary_key: Optional[str] = None
    summary_base_url: Optional[str] = None
    summary_model: Optional[str] = None
    keep_video: bool = False
    cookies: Optional[Path] = None
    # 直接从本机浏览器读取登录 cookies（yt-dlp cookies-from-browser；空 = 不使用）。
    # 与 cookies 文件互斥，显式 cookies 文件优先（见 sources/url._resolve_cookie_opts）
    cookies_browser: str = ""
    # 自定义 yt-dlp User-Agent（环境变量 VTS_USER_AGENT；空 = 用 yt-dlp 默认，不改默认行为）。
    # 非浏览器 UA（如 Wget/1.21.3）可绕过 B 站边缘 WAF 按 UA×IP 信誉对 /video/ HTML 页的
    # 412 挑战；仅按需设置，不要全局默认（会影响其它站点）。
    user_agent: Optional[str] = None
    proxy: Optional[str] = None
    audio_format: str = "wav"
    # 默认是否启用 LLM 文本优化：显式传入时以显式值为准；否则每次实例化读取全局开关（POLISH_TRANSCRIPT 环境变量）
    polish_transcript: bool = field(default_factory=lambda: _env_bool("POLISH_TRANSCRIPT"))
    polish_model: Optional[str] = None
    polish_base_url: Optional[str] = None
    polish_preset: Optional[str] = None
    asr_model: Optional[str] = None
    asr_base_url: Optional[str] = None
    summary_template: str = "通用"
    # 视频自带字幕优先：preference=off 关闭，auto/manual_only 时可用字幕直接跳过下载+转写
    subtitle_preference: str = SubtitlePreference.AUTO
    subtitle_language: str = "auto"

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "Settings":
        """从 dict（CLI args / Web payload）构造 Settings：只取已知字段并做类型归一。

        统一 main.py 与 web/tasks.py 的构造逻辑，新增字段只需在 dataclass 声明一次。
        """
        known = {f.name for f in fields(cls)}
        data: dict[str, Any] = {}
        for key, value in mapping.items():
            if key not in known or value is None:
                continue
            data[key] = value

        data["url"] = data.get("url") or mapping.get("url") or ""

        # Path 字段归一（payload / argparse 传来的通常是 str）
        for f in fields(cls):
            if f.type is Path or f.type is Optional[Path]:
                if f.name in data and data[f.name] is not None and not isinstance(data[f.name], Path):
                    data[f.name] = Path(data[f.name])
            # bool 字段归一（payload 可能传来 "true"/"false" 等字符串）
            elif f.type is bool and f.name in data and not isinstance(data[f.name], bool):
                data[f.name] = str(data[f.name]).strip().lower() in ("1", "true", "yes", "on")

        return cls(**data)

    def __post_init__(self) -> None:
        # Key / 端点兜底链「新名 → 旧专名 → 通用旧名」（见 first_env_value）。
        # asr_key 只回落通用旧名 OPENAI_API_KEY；跨槽兜底（转写无 Key 时用推理 Key）
        # 在使用点以 `asr_key or summary_key` 表达，与旧结构同构。
        if not self.asr_key:
            self.asr_key = first_env_value(os.environ.get, "ASR_API_KEY", "OPENAI_API_KEY")
        if not self.summary_key:
            self.summary_key = first_env_value(os.environ.get, "SUMMARY_API_KEY", "LLM_API_KEY", "OPENAI_API_KEY")
        if not self.summary_base_url:
            self.summary_base_url = first_env_value(os.environ.get, "SUMMARY_BASE_URL", "LLM_BASE_URL")
        if not self.summary_model:
            self.summary_model = first_env_value(os.environ.get, "SUMMARY_MODEL", "LLM_MODEL")
        if not self.polish_model:
            self.polish_model = os.environ.get("POLISH_MODEL")
        if not self.polish_base_url:
            self.polish_base_url = os.environ.get("POLISH_BASE_URL") or self.summary_base_url
        if not self.polish_preset:
            self.polish_preset = os.environ.get("POLISH_PRESET")
        if not self.asr_model:
            self.asr_model = os.environ.get("ASR_MODEL")
        if not self.asr_base_url:
            self.asr_base_url = os.environ.get("ASR_BASE_URL")
        if not self.summary_template:
            self.summary_template = os.environ.get("SUMMARY_TEMPLATE", "通用")
        # 自定义 User-Agent：环境变量 VTS_USER_AGENT（显式传入的字段值优先，空才读环境变量）
        if not self.user_agent:
            self.user_agent = normalize_user_agent(os.environ.get("VTS_USER_AGENT"))
        # cookies 文件：环境变量 VTS_COOKIES_FILE 等价 CLI --cookies（显式 --cookies 优先）。
        # 文件不存在时**明确 WARN 并说明路径**（不静默忽略，用户会以为已生效），且不把
        # 死路径交给 yt-dlp（浏览器 cookies 设置仍可正常兜底）。
        if not self.cookies:
            cookies_file = (os.environ.get("VTS_COOKIES_FILE") or "").strip()
            if cookies_file:
                cookies_path = Path(cookies_file)
                if cookies_path.is_file():
                    self.cookies = cookies_path
                else:
                    logger.warning(
                        "VTS_COOKIES_FILE=%s 指定的文件不存在，已忽略该配置（任务将不使用 cookies）；"
                        "Docker 示例：把 cookies.txt 挂载到 /data/cookies.txt 并设置 "
                        "VTS_COOKIES_FILE=/data/cookies.txt",
                        cookies_file,
                    )
