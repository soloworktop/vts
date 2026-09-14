#!/usr/bin/env python3
"""集成示例：把 VTS 当作库嵌入你自己的脚本 / Agent skill。

`generate_video_summary()` 是一个可直接复用的最小封装：输入视频 URL，返回
Markdown 笔记路径；未配置 LLM Key 时不报错，而是仍产出转写原文。
"""

import os
import sys
from pathlib import Path

from video_to_summary import OpenAISummarizer, OpenAIWhisperAPITranscriber, URLAudioSource, run


def generate_video_summary(video_url: str, output_dir: Path = Path("output")) -> Path:
    """输入视频 URL，输出 Markdown 笔记路径（BYOK）。

    字幕优先：视频自带字幕时零 API 成本；没有字幕才用转写端点。
    """
    source = URLAudioSource(video_url, output_dir=output_dir, audio_format="mp3")
    transcriber = OpenAIWhisperAPITranscriber(
        api_key=os.environ.get("ASR_API_KEY", ""),
        base_url=os.environ.get("ASR_BASE_URL") or None,
    )
    summarizer = None
    if os.environ.get("SUMMARY_API_KEY"):
        summarizer = OpenAISummarizer(
            api_key=os.environ["SUMMARY_API_KEY"],
            model=os.environ.get("SUMMARY_MODEL", ""),
            base_url=os.environ.get("SUMMARY_BASE_URL") or None,
        )
    return run(source, transcriber, summarizer, output_dir)


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    summary_path = generate_video_summary(url)
    print(summary_path)
    print(summary_path.read_text(encoding="utf-8")[:500])


if __name__ == "__main__":
    main()
