import argparse
import functools
import sys
from pathlib import Path

from dotenv import load_dotenv

from .config import DEFAULT_ASR_API_MODEL, Settings, SubtitleConfig
from .pipeline import run
from .polishers.llm import LLMTranscriptPolisher
from .sources.url import URLAudioSource
from .summarizers.openai import OpenAISummarizer, SUMMARY_TEMPLATES
from .transcribers.openai_whisper_api import OpenAIWhisperAPITranscriber
from .utils import configure_logging


@functools.lru_cache(maxsize=1)
def _version_string() -> str:
    """--version 展示值：version.get_version()（开发态 git describe / 发布 tag），失败回落 "dev"。

    进程内缓存一次：parse_args 每次启动（含 --help/业务错误路径）都会构建
    parser，开发态 get_version() 会 spawn git 子进程，不应让全部路径买单。
    """
    try:
        from .version import get_version

        return get_version() or "dev"
    except Exception:  # noqa: BLE001 - 版本展示永不阻断 CLI 主流程
        return "dev"


class _HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    """默认值展示 + epilog 示例保留换行（多继承组合，argparse 惯用法）。"""


_EPILOG = """示例:
  python -m video_to_summary.main <视频链接>                        # 字幕优先（自带字幕时零 API 成本）
  python -m video_to_summary.main <视频链接> --summary-template 学术笔记
  python -m video_to_summary.main <视频链接> --llm-key sk-xxx --llm-base-url https://api.example.com/v1 --llm-model gpt-4o-mini
  python -m video_to_summary.main <视频链接> --whisper-api --openai-key sk-xxx   # 无字幕时用 Whisper API 转写
"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="vts",
        description="VTS：输入视频 URL，字幕优先取文稿（自带字幕零 API 成本），再生成结构化 Markdown 总结笔记",
        formatter_class=_HelpFormatter,
        epilog=_EPILOG,
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s " + _version_string(),
        help="显示版本号后退出",
    )
    parser.add_argument("url", help="视频 URL")
    parser.add_argument("--output-dir", default="output", help="输出目录")
    parser.add_argument(
        "--whisper-api",
        action="store_true",
        help="使用 OpenAI 兼容 Whisper API 转写（无自带字幕时；需 --openai-key）",
    )
    parser.add_argument("--openai-key", help="OpenAI API Key（Whisper API 转写用）")
    parser.add_argument("--asr-model", help=f"Whisper API 模型名（默认 {DEFAULT_ASR_API_MODEL}）")
    parser.add_argument("--asr-base-url", help="Whisper API Base URL（自建/第三方兼容端点）")
    parser.add_argument("--llm-key", help="LLM API Key")
    parser.add_argument("--llm-base-url", help="LLM Base URL")
    parser.add_argument("--llm-model", help="LLM 模型名称")
    parser.add_argument("--keep-video", action="store_true", help="保留视频文件")
    parser.add_argument("--cookies", type=Path, help="cookies.txt 路径")
    parser.add_argument("--proxy", help="HTTP/HTTPS 代理")
    parser.add_argument("--audio-format", default="wav", choices=["wav", "mp3"], help="音频格式")
    parser.add_argument(
        "--polish-transcript",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="LLM 文本优化（默认取全局开关 POLISH_TRANSCRIPT；可用 --no-polish-transcript 关闭）",
    )
    parser.add_argument("--polish-model", help="转写优化模型名称")
    parser.add_argument("--polish-base-url", help="转写优化 Base URL")
    parser.add_argument(
        "--polish-preset",
        choices=["default", "light"],
        help="转写优化 prompt 预设（default 保守校对 / light 轻量）",
    )
    parser.add_argument(
        "--summary-template",
        default="通用",
        choices=list(SUMMARY_TEMPLATES.keys()),
        help="总结模板名称",
    )
    parser.add_argument(
        "--subtitle-preference",
        choices=["auto", "manual_only", "off"],
        default=None,
        help="视频自带字幕优先：auto（有字幕就用，人工优先，默认）/ manual_only（仅人工字幕）/ off（关闭，始终下载转写）",
    )
    parser.add_argument(
        "--subtitle-language",
        default=None,
        help="字幕语言偏好：auto（中文优先，默认）或语言代码（如 zh / en / ja）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    configure_logging()
    args = parse_args(argv)

    # 业务错误走 stderr（脚本/管道友好），不污染 stdout；退出码 2 与 argparse 惯例一致
    if args.whisper_api and not args.openai_key:
        print(
            "error: --whisper-api requires --openai-key / --llm-key (or OPENAI_API_KEY env)",
            file=sys.stderr,
            flush=True,
        )
        return 2

    settings = Settings.from_mapping(vars(args))

    source = URLAudioSource(
        settings.url,
        output_dir=settings.output_dir,
        keep_video=settings.keep_video,
        cookies=settings.cookies,
        user_agent=settings.user_agent,
        proxy=settings.proxy,
        audio_format=settings.audio_format,
    )

    # 字幕优先：视频自带字幕时 pipeline 直接用字幕文本，完全跳过这里构造的转写器
    # （因此没有 Key 也能出 .txt/.srt）；无字幕时才真正调用 Whisper API
    transcriber = OpenAIWhisperAPITranscriber(
        api_key=settings.openai_key or settings.llm_key or "",
        model=settings.asr_model or DEFAULT_ASR_API_MODEL,
        base_url=settings.asr_base_url or settings.llm_base_url,
    )

    # BYOK：未提供 Key 时不构造摘要器（pipeline 推 summarize_skipped 事件后
    # 仍产出转写文本），而不是让整个任务失败；仅设 base_url 无 Key 等于没配
    summarizer = None
    if settings.llm_key or settings.openai_key:
        summarizer = OpenAISummarizer(
            api_key=settings.llm_key or settings.openai_key or "",
            model=settings.llm_model or "",
            base_url=settings.llm_base_url,
            template=settings.summary_template,
        )

    polisher = None
    if settings.polish_transcript and (settings.llm_key or settings.openai_key):
        polisher = LLMTranscriptPolisher(
            api_key=settings.llm_key or settings.openai_key or "",
            model=settings.polish_model or settings.llm_model or "",
            base_url=settings.polish_base_url,
            prompt_preset=settings.polish_preset or "default",
        )

    # 字幕优先：不在此处预取元信息/下载音频（那会让 CLI 在有字幕时白下载、
    # 被 412 风控时误失败）。pipeline.run 内部先尝试字幕提取，仅在无字幕时
    # 才调 source.resolve() 下载，meta 由 pipeline 自取并回填 polisher_title
    output = run(
        source,
        transcriber,
        summarizer,
        settings.output_dir,
        polisher=polisher,
        subtitle_config=SubtitleConfig(
            preference=settings.subtitle_preference,
            language=settings.subtitle_language,
        ),
    )
    print(f"summary -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
