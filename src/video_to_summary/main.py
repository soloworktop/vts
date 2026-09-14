import argparse
import functools
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from .config import DEFAULT_ASR_API_MODEL, Settings, SubtitleConfig
from .pipeline import run
from .polishers.llm import LLMTranscriptPolisher
from .sources.url import URLAudioSource
from .summarizers.openai import OpenAISummarizer, SUMMARY_TEMPLATES
from .transcribers.openai_whisper_api import (
    MissingASRCredentialsError,
    OpenAIWhisperAPITranscriber,
)
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
  python -m video_to_summary.main <视频链接> --summary-key sk-xxx --summary-base-url https://api.example.com/v1 --summary-model gpt-4o-mini
  python -m video_to_summary.main <视频链接> --whisper-api --asr-key sk-xxx   # 无字幕时用 Whisper API 转写
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
        help="使用 OpenAI 兼容 Whisper API 转写（无自带字幕时；需 --asr-key）",
    )
    # 两个槽位的 Key / 端点 / 模型旗标：新名为主推（与环境变量 SUMMARY_* / ASR_* 对称），
    # 旧名 --llm-* / --openai-key 以同 dest 隐藏别名保留，老命令行不受影响。
    parser.add_argument("--asr-key", help="转写 API Key（OpenAI 兼容 Whisper API；等价环境变量 ASR_API_KEY）")
    parser.add_argument("--openai-key", dest="asr_key", help=argparse.SUPPRESS)
    parser.add_argument("--asr-model", help=f"Whisper API 模型名（默认 {DEFAULT_ASR_API_MODEL}）")
    parser.add_argument("--asr-base-url", help="Whisper API Base URL（自建/第三方兼容端点）")
    parser.add_argument("--summary-key", help="推理 API Key（总结与文本润色；等价环境变量 SUMMARY_API_KEY）")
    parser.add_argument("--llm-key", dest="summary_key", help=argparse.SUPPRESS)
    parser.add_argument("--summary-base-url", help="推理 API Base URL")
    parser.add_argument("--llm-base-url", dest="summary_base_url", help=argparse.SUPPRESS)
    parser.add_argument("--summary-model", help="推理模型名称")
    parser.add_argument("--llm-model", dest="summary_model", help=argparse.SUPPRESS)
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
    # .env 从**命令执行的当前目录**向上查找（CLI 惯例）。load_dotenv() 无参等价于
    # 从本文件所在位置向上找——pip 安装后在任意目录使用时，用户自己的 .env 会静默
    # 失效；usecwd=True 后开发场景（scripts/*.sh 固定 cd 到仓库根执行）行为不变。
    load_dotenv(find_dotenv(usecwd=True))
    configure_logging()
    args = parse_args(argv)

    settings = Settings.from_mapping(vars(args))

    # 业务错误走 stderr（脚本/管道友好），不污染 stdout；退出码 2 与 argparse 惯例一致。
    # 必须在 Settings 构造之后检查：__post_init__ 会回落读 ASR_API_KEY / OPENAI_API_KEY 等
    # 环境变量，提前检查只看显式参数，会误拒按报错文案设置了环境变量的用户。
    if settings.whisper_api and not (settings.asr_key or settings.summary_key):
        print(
            "error: --whisper-api requires --asr-key / --summary-key (or ASR_API_KEY / SUMMARY_API_KEY env)",
            file=sys.stderr,
            flush=True,
        )
        return 2

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
        api_key=settings.asr_key or settings.summary_key or "",
        model=settings.asr_model or DEFAULT_ASR_API_MODEL,
        base_url=settings.asr_base_url or settings.summary_base_url,
    )

    # BYOK：未提供 Key 时不构造摘要器（pipeline 推 summarize_skipped 事件后
    # 仍产出转写文本），而不是让整个任务失败；仅设 base_url 无 Key 等于没配
    summarizer = None
    if settings.summary_key or settings.asr_key:
        summarizer = OpenAISummarizer(
            api_key=settings.summary_key or settings.asr_key or "",
            model=settings.summary_model or "",
            base_url=settings.summary_base_url,
            template=settings.summary_template,
        )

    polisher = None
    if settings.polish_transcript and (settings.summary_key or settings.asr_key):
        polisher = LLMTranscriptPolisher(
            api_key=settings.summary_key or settings.asr_key or "",
            model=settings.polish_model or settings.summary_model or "",
            base_url=settings.polish_base_url,
            prompt_preset=settings.polish_preset or "default",
        )

    # 字幕优先：不在此处预取元信息/下载音频（那会让 CLI 在有字幕时白下载、
    # 被 412 风控时误失败）。pipeline.run 内部先尝试字幕提取，仅在无字幕时
    # 才调 source.resolve() 下载，meta 由 pipeline 自取并回填 polisher_title。
    # 无 Key + 无字幕视频：转写阶段抛 MissingASRCredentialsError，转成
    # stderr 引导 + 退出码 2，而不是让 openai SDK 的裸 traceback 直接冒出。
    try:
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
    except MissingASRCredentialsError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 2
    print(f"summary -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
