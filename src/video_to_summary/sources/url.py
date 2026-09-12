import logging
import re
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from video_to_summary.config import SubtitleConfig, normalize_user_agent
from video_to_summary.constants import SubtitlePreference
from video_to_summary.schemas import AudioMeta
from video_to_summary.sources.base import Source
from video_to_summary.subtitles import SubtitleResult, parse_subtitle_file

logger = logging.getLogger("video_to_summary.sources.url")

# 可作转写来源的字幕扩展名（弹幕等非语音轨除外）
_SUBTITLE_EXTS = {".vtt", ".srt", ".json", ".json3", ".ass", ".ssa"}

# 字幕轨道 ext 拉取优先级（解析器支持 VTT/SRT/JSON3）
_TRACK_EXT_PREFERENCE = ("vtt", "srt", "json3", "json")

# 非语音轨伪字幕（B 站弹幕 / YouTube 直播聊天回放），一律排除
_NON_SPEECH_LANGS = {"danmaku", "live_chat"}

# 单次提取最多尝试的候选字幕数（防止对平台字幕接口的连发请求触发限流）
_SUBTITLE_MAX_ATTEMPTS = 3
# 相邻两次字幕拉取之间的退避间隔（秒）；测试可置 0
_SUBTITLE_FETCH_SLEEP = 1.0

# 允许交给 yt-dlp 的 URL scheme 白名单：本地单用户工具，仅放行公开网络协议，
# 拒绝 file:// 等本地文件读取与其它非预期协议
_ALLOWED_URL_SCHEMES = {"http", "https"}


def _validate_url(url: str) -> None:
    """进入 yt-dlp 前校验 URL：仅 http/https（拒绝 file:// 等本地协议注入）。"""
    if not url or not isinstance(url, str):
        raise ValueError("url is required")
    scheme = urlparse(url).scheme.lower()
    if scheme not in _ALLOWED_URL_SCHEMES:
        raise ValueError(f"unsupported URL scheme: {scheme or '(none)'} (仅支持 http/https)")


def _is_bilibili(url: str) -> bool:
    return "bilibili.com" in (url or "").lower() or "b23.tv" in (url or "").lower()


def _resolve_cookies(url: str, cookies: Optional[Path]) -> Optional[Path]:
    """解析 yt-dlp cookiefile：只认用户显式传入的文件。

    开源版不提供站点扫码登录（能力位 ``bilibili_login`` = false）：登录态一律由
    用户自备——``--cookies cookies.txt``，或经浏览器 cookies（见 _resolve_cookie_opts）。
    显式文件与浏览器 cookies 互斥，显式文件优先。
    """
    return cookies


def _resolve_cookie_opts(url: str, cookies: Optional[Path], cookies_browser: str) -> dict:
    """解析 yt-dlp cookie 配置（``cookiefile`` 与 ``cookiesfrombrowser`` 互斥，同时设置会报错）。

    优先级：显式 cookies 文件 > 浏览器 cookies（yt-dlp cookies-from-browser，直接读
    本机浏览器登录态；YouTube 自动字幕 429 限流即由此解锁，B 站 CC/AI 字幕同理解锁）。
    """
    resolved = _resolve_cookies(url, cookies)
    if resolved:
        return {"cookiefile": str(resolved)}
    browser = (cookies_browser or "").strip().lower()
    if browser:
        return {"cookiesfrombrowser": (browser,)}
    return {}


def _resolve_user_agent(value: Optional[str], http_headers: Optional[dict] = None) -> dict:
    """解析 yt-dlp User-Agent 配置：非空才注入，空/未设置返回 {}（= yt-dlp 默认 UA）。

    与 _resolve_cookie_opts 同风格：返回可直接 ``ydl_opts.update()`` 的字典。
    值统一经 config.normalize_user_agent 清洗（去空白/控制字符、限长，非法值 WARN）。

    为什么必须同时写进 ``http_headers["User-Agent"]``（服务器实测结论，勿再"简化"回去）：
    yt-dlp 2026.08.19 在 B 站这条提取路径上，仅设 ``user_agent`` 不足以替换请求头里的
    UA（真实请求仍 HTTP 412）；CLI ``--user-agent`` 之所以有效，正是因为它除
    ``user_agent`` 外还会写进 ``http_headers``。库参数实测：
    {"user_agent": ...} → 412；{"http_headers": {"User-Agent": ...}} → 成功。因此这里
    两者都设，兼顾只认其一 / 两者皆认的其它提取器与站点。

    http_headers **合并而非覆盖**：若调用方 ydl_opts 已带 ``http_headers``（如设置了
    其它请求头），只把 ``User-Agent`` 键合并进去，绝不整体替换别人设的头。
    """
    ua = normalize_user_agent(value)
    if not ua:
        return {}
    headers = dict(http_headers or {})
    headers["User-Agent"] = ua
    return {"user_agent": ua, "http_headers": headers}


def _skip_reason_hint(url: str, cookies: Optional[Path], cookies_browser: str) -> str:
    """无可用字幕时的原因说明。

    B 站视频的 AI/CC 字幕对匿名访问不可见，是最常见的根因，单独点名并给出
    开源版可行的解法（配置 cookies），而不是指向不存在的扫码登录入口。
    """
    if _is_bilibili(url) and not _resolve_cookie_opts(url, cookies, cookies_browser):
        return "no usable subtitle: B站 AI/CC 字幕通常需登录，请配置 cookies（cookies.txt 或浏览器 cookies）"
    return "no usable subtitle"


def _subtitle_langs(language: str) -> list[str]:
    """把字幕语言偏好转成语言匹配模式（支持 ``X.*`` 前缀通配与精确名）。

    - auto：中文优先（zh.* 含 zh-Hans/zh-Hant），其次英文；并兜底 B 站 AI 字幕（ai-.*）；
    - 显式语言：精确代码 + 同族（en → en.*），并为 zh/en 补上对应 AI 字幕代码。
    """
    lang = (language or "auto").strip().lower()
    if not lang or lang in ("auto", "default"):
        return ["zh.*", "en.*", "ai-.*"]
    langs = [lang, f"{lang}.*"]
    if lang in ("zh", "cn"):
        langs.append("ai-zh")
    elif lang == "en":
        langs.append("ai-en")
    return langs


def _lang_matches(lang: str, patterns: list[str]) -> bool:
    """语言是否命中任一模式：``X.*`` 按 yt-dlp 同款前缀语义，其余精确匹配。"""
    for p in patterns:
        if p.endswith(".*"):
            if lang.startswith(p[:-2]):
                return True
        elif lang == p:
            return True
    return False


def _lang_rank(lang: str, preferred: str) -> int:
    """语言偏好排序：显式语言精确/同族最高，其次中文 > 英文 > 其它。

    AI 字幕（ai-xx）按剥离前缀后的基础语言排序，与人工字幕同级，
    仅在人工/自动分组时由 is_auto 区分。
    """
    lang = (lang or "").lower()
    base = lang[3:] if lang.startswith("ai-") else lang
    if preferred and preferred not in ("auto", "default"):
        if base == preferred or base.startswith(preferred):
            return 0
    if base.startswith("zh"):
        return 1
    if base.startswith("en"):
        return 2
    return 3


def _pick_track(tracks: list) -> Optional[dict]:
    """从一条语言的字幕轨道列表里挑最佳格式：vtt > srt > json3 > json > 其它。

    轨道内容两种形态都接受：``url``（YouTube timedtext 等，需再发请求拉取）与
    ``data``（B 站提取器把 SRT 文本直接内联在轨道里，无 url 字段）。
    """
    def _rank(item: dict) -> int:
        ext = str(item.get("ext") or "").lower().lstrip(".")
        return _TRACK_EXT_PREFERENCE.index(ext) if ext in _TRACK_EXT_PREFERENCE else len(_TRACK_EXT_PREFERENCE)

    def _usable(item: dict) -> bool:
        if not isinstance(item, dict):
            return False
        has_content = isinstance(item.get("url"), str) or isinstance(item.get("data"), (str, bytes))
        return has_content and str(item.get("ext") or "").lower().lstrip(".") in {e.lstrip(".") for e in _SUBTITLE_EXTS}

    usable = [t for t in (tracks or []) if _usable(t)]
    return min(usable, key=_rank) if usable else None


def _absolute_url(url: str) -> Optional[str]:
    """规范化字幕轨道 URL：补全协议相对地址，仅放行 http/https。"""
    url = (url or "").strip()
    if url.startswith("//"):
        url = f"https:{url}"
    if not url.lower().startswith(("http://", "https://")):
        return None
    return url


class URLAudioSource:
    def __init__(
        self,
        url: str,
        *,
        output_dir: Path,
        keep_video: bool = False,
        cookies: Optional[Path] = None,
        cookies_browser: str = "",
        user_agent: Optional[str] = None,
        proxy: Optional[str] = None,
        audio_format: str = "wav",
    ) -> None:
        _validate_url(url)
        self.url = url
        self.output_dir = output_dir
        self.keep_video = keep_video
        self.cookies = cookies
        self.cookies_browser = cookies_browser
        self.user_agent = user_agent
        self.proxy = proxy
        self.audio_format = audio_format
        self.meta: Optional[AudioMeta] = None
        # extract_subtitle 返回 None 时给 pipeline 的跳过原因（事件链展示用）
        self.subtitle_skip_reason: str = ""
        # 下载进度回调（可选）：payload 见 _make_download_progress_hook。
        # 与 transcriber.on_progress 同构——由调用方（web/tasks 桥接事件链；
        # CLI 不设置即零开销）
        self.on_progress: Optional[Callable[[dict], None]] = None

    def resolve(self) -> tuple[Path, AudioMeta]:
        audio_path, meta = download_audio(
            self.url,
            self.output_dir,
            keep_video=self.keep_video,
            cookies=self.cookies,
            cookies_browser=self.cookies_browser,
            user_agent=self.user_agent,
            proxy=self.proxy,
            audio_format=self.audio_format,
            on_progress=self.on_progress,
        )
        self.meta = meta
        return audio_path, meta

    def extract_subtitle(self, config: SubtitleConfig) -> Optional[SubtitleResult]:
        """用视频自带字幕生成转写结果（免下载音频），无可用字幕返回 None。

        流程（两段式，规避平台字幕接口限流）：
        1. yt-dlp 仅提取元信息（不触发任何字幕文件下载），拿到可用语言清单；
        2. 本地按「人工 > 自动 + 语言偏好」排序候选，逐个直接拉取字幕文件——
           每次只请求一个语言。早期实现用 subtitleslangs 通配让 yt-dlp 批量下载
           所有匹配语言，YouTube timedtext 接口对连发请求返回 429，连人工字幕
           也一并失败（匿名访问时 ASR 自动字幕本身就被定点限流，需登录 cookies）。

        全部候选失败返回 None，由 pipeline 回退到「下载音频 + 转写」。
        """
        if config.preference == SubtitlePreference.OFF:
            return None
        output_dir = self.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            import yt_dlp
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("yt-dlp is required for URLAudioSource") from exc

        ydl_opts = {
            "skip_download": True,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            # yt-dlp 门控（common.py::extract_subtitles/extract_automatic_captions）：
            # extract_info 返回的 subtitles / automatic_captions 清单仅在
            # writesubtitles / writeautomaticsub（或 listsubtitles）为真时才拉取，
            # 否则恒为空 dict（表现为所有视频 "no usable subtitle"）。
            # 这里只是要轨道清单：本方法第二段自行 ydl.urlopen 单文件拉取，
            # 且未设 subtitleslangs，不会触发 yt-dlp 的批量字幕文件下载。
            "writesubtitles": True,
            "writeautomaticsub": True,
        }
        ydl_opts.update(_resolve_cookie_opts(self.url, self.cookies, self.cookies_browser))
        # 自定义 User-Agent：user_agent + http_headers["User-Agent"] 同时注入（仅设
        # user_agent 在 B 站提取路径上不生效，实测仍 412）；已有 http_headers 时合并
        ydl_opts.update(_resolve_user_agent(self.user_agent, ydl_opts.get("http_headers")))
        if self.proxy:
            ydl_opts["proxy"] = self.proxy

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # 阶段 1：仅元信息（无字幕下载副作用）
            info = ydl.extract_info(self.url, download=False)

            manual_map = {
                k: v for k, v in (info.get("subtitles") or {}).items()
                if k not in _NON_SPEECH_LANGS
            }
            auto_map = {
                k: v for k, v in (info.get("automatic_captions") or {}).items()
                if k not in _NON_SPEECH_LANGS
            }

            patterns = _subtitle_langs(config.language)
            # 候选清单按「人工优先于自动」分组，组内按语言偏好排序
            candidates = [(lang, False) for lang in manual_map if _lang_matches(lang, patterns)]
            candidates += [(lang, True) for lang in auto_map if _lang_matches(lang, patterns)]
            if config.preference == SubtitlePreference.MANUAL_ONLY:
                candidates = [(lang, auto) for lang, auto in candidates if not auto]
            if not candidates:
                logger.info("no usable subtitle languages (manual=%s auto=%d langs)",
                            sorted(manual_map), len(auto_map))
                self.subtitle_skip_reason = _skip_reason_hint(
                    self.url, self.cookies, self.cookies_browser)
                return None
            preferred = (config.language or "auto").strip().lower()
            candidates.sort(key=lambda c: (1 if c[1] else 0, _lang_rank(c[0], preferred)))

            video_id = info.get("id", "video")
            # 阶段 2：逐候选取内容（内联 data 直接用；url 形态单请求/候选，
            # 失败换下一个，防 429 突发限流）
            last_error: Optional[Exception] = None
            for index, (lang, is_auto) in enumerate(candidates[:_SUBTITLE_MAX_ATTEMPTS]):
                entry = _pick_track((auto_map if is_auto else manual_map).get(lang) or [])
                if not entry:
                    continue
                inline = entry.get("data")
                if isinstance(inline, (str, bytes)) and len(inline) > 0:
                    # B 站等提取器：字幕内容已内联在轨道 data 字段，无需再发请求
                    data = inline.encode("utf-8") if isinstance(inline, str) else inline
                else:
                    url = _absolute_url(entry.get("url", ""))
                    if not url:
                        continue
                    if index and _SUBTITLE_FETCH_SLEEP > 0:
                        time.sleep(_SUBTITLE_FETCH_SLEEP)
                    try:
                        data = ydl.urlopen(url).read()
                    except Exception as exc:  # noqa: BLE001 - 单候选失败（限流/网络）降级下一候选
                        last_error = exc
                        logger.warning("subtitle fetch failed for %s (%s): %s",
                                       lang, "auto" if is_auto else "manual", exc)
                        continue
                if not data.strip():
                    logger.warning("subtitle track %s is empty, trying next candidate", lang)
                    continue
                ext = str(entry.get("ext") or "vtt").lower().lstrip(".")
                if not re.fullmatch(r"[a-z0-9]{1,5}", ext):
                    ext = "vtt"
                safe_lang = re.sub(r"[^A-Za-z0-9_-]", "_", str(lang or "sub"))
                subtitle_path = output_dir / f"{video_id}.{safe_lang}.{ext}"
                subtitle_path.write_bytes(data)

                transcript = parse_subtitle_file(subtitle_path)
                if not transcript.text.strip():
                    logger.warning("subtitle track %s parsed to empty text, trying next candidate", lang)
                    continue

                meta = AudioMeta(
                    source_id=video_id,
                    title=info.get("title"),
                    source_url=self.url,
                    duration=info.get("duration"),
                    uploader=info.get("uploader"),
                    upload_date=info.get("upload_date"),
                    audio_path=subtitle_path,
                )
                self.meta = meta
                return SubtitleResult(
                    transcript=transcript,
                    meta=meta,
                    subtitle_path=subtitle_path,
                    language=lang,
                    automatic=is_auto,
                )

            if last_error is not None:
                logger.warning("subtitle extraction abandoned after %d candidate(s): %s",
                               min(len(candidates), _SUBTITLE_MAX_ATTEMPTS), last_error)
        return None


def _make_download_progress_hook(on_progress: Callable[[dict], None]) -> Callable[[dict], None]:
    """包装 yt-dlp progress_hook → 节流后的进度回调。

    节流双阈值：距上次发出 ≥1 秒 **或** 百分比步进 ≥5% 才回调一次（100% 终态
    必发）。yt-dlp 每秒回调多次，直接透传会把事件链灌爆。percent 在总大小
    未知（某些直播/分片流）时为 None，调用方按字节展示。回调异常只记日志，
    绝不中断下载。
    """
    state = {"last_ts": 0.0, "last_percent": -100.0}

    def _emit(payload: dict) -> None:
        try:
            on_progress(payload)
        except Exception:  # noqa: BLE001 - 进度回调不得影响主流程
            logger.warning("download progress callback failed", exc_info=True)

    def hook(d: dict) -> None:
        status = d.get("status")
        if status not in ("downloading", "finished"):
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        downloaded = d.get("downloaded_bytes") or 0
        # total_bytes_estimate 是估算值，低估实际字节时钳制到 100%（与 finished 收口一致）
        percent = min(round(downloaded / total * 100, 1), 100.0) if total else None
        if status == "finished":
            # 终态必达：100% 不经节流（总大小未知时也补一条 finished 收口）
            _emit({"percent": 100.0, "downloaded_bytes": downloaded, "total_bytes": total,
                   "speed": None, "eta": 0})
            return
        now = time.monotonic()
        if percent is not None and now - state["last_ts"] < 1.0 and percent - state["last_percent"] < 5.0:
            return
        if percent is None and now - state["last_ts"] < 1.0:
            return
        state["last_ts"] = now
        state["last_percent"] = percent if percent is not None else -100.0
        _emit({
            "percent": percent,
            "downloaded_bytes": downloaded,
            "total_bytes": total,
            "speed": d.get("speed"),
            "eta": d.get("eta"),
        })

    return hook


def download_audio(
    url: str,
    output_dir: Path,
    *,
    keep_video: bool = False,
    cookies: Optional[Path] = None,
    cookies_browser: str = "",
    user_agent: Optional[str] = None,
    proxy: Optional[str] = None,
    audio_format: str = "wav",
    on_progress: Optional[Callable[[dict], None]] = None,
) -> tuple[Path, AudioMeta]:
    output_dir.mkdir(parents=True, exist_ok=True)

    fmt = (audio_format or "wav").strip().lower()
    if fmt not in {"wav", "mp3"}:
        raise ValueError(f"unsupported audio_format: {audio_format}")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(output_dir / "%(id)s.%(ext)s"),
        "restrictfilenames": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": fmt,
                "preferredquality": "0",
            }
        ],
    }
    # cookies 解析：显式 cookies 文件优先于浏览器 cookies（互斥，见 _resolve_cookie_opts）
    ydl_opts.update(_resolve_cookie_opts(url, cookies, cookies_browser))
    # 自定义 User-Agent：非空才注入，空 = yt-dlp 默认（见 _resolve_user_agent）。
    # 同时写 user_agent 与 http_headers["User-Agent"]——B 站提取路径实测仅设
    # user_agent 不会替换请求头里的 UA（仍 412）；已有 http_headers 时合并不覆盖。
    ydl_opts.update(_resolve_user_agent(user_agent, ydl_opts.get("http_headers")))
    if proxy:
        ydl_opts["proxy"] = proxy
    # 进度回调只在需要时挂载（CLI 零开销）
    if on_progress is not None:
        ydl_opts["progress_hooks"] = [_make_download_progress_hook(on_progress)]

    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("yt-dlp is required for URLAudioSource") from exc

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    video_id = info.get("id", "video")
    audio_path = output_dir / f"{video_id}.{fmt}"
    if not audio_path.exists():
        raise FileNotFoundError(f"audio file not found: {audio_path}")

    meta = AudioMeta(
        source_id=video_id,
        title=info.get("title"),
        source_url=url,
        duration=info.get("duration"),
        uploader=info.get("uploader"),
        upload_date=info.get("upload_date"),
        audio_path=audio_path,
    )

    if not keep_video:
        _remove_intermediate_video(output_dir, video_id)

    return audio_path, meta


# 中间视频容器白名单：FFmpegExtractAudio 提取音频后残留的源视频文件扩展名。
# 只删白名单内文件——output_dir 同目录还有 pipeline 缓存产物（.txt/.segments.json/
# .srt/.polished.txt/.summary.md/字幕 .vtt 等），泛化 glob 删除会破坏
# 「文件存在即命中缓存」语义（曾把 {id}.txt 一并清掉）。
_INTERMEDIATE_VIDEO_EXTS = {".mkv", ".webm", ".mp4", ".avi", ".mov", ".flv", ".ts", ".3gp", ".ogv"}


def _remove_intermediate_video(output_dir: Path, video_id: str) -> None:
    """删除本次下载残留的中间视频容器文件（仅白名单扩展名，忽略不存在）。"""
    for p in output_dir.glob(f"{video_id}.*"):
        if p.suffix.lower() in _INTERMEDIATE_VIDEO_EXTS:
            try:
                p.unlink()
            except FileNotFoundError:
                pass
