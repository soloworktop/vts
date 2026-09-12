#!/usr/bin/env python3
"""基础示例：从视频 URL 生成结构化 Markdown 笔记（BYOK）。

字幕优先：视频自带字幕时 pipeline 直接使用字幕文本，不下载音频、不调用任何转写
接口——因此下面即使不配转写 Key 也能跑通有字幕的视频。

运行前设置环境变量（任意 OpenAI 兼容端点）：
    export LLM_API_KEY=sk-xxx
    export LLM_BASE_URL=https://api.deepseek.com/v1
    export LLM_MODEL=deepseek-chat

用法:
    python examples/basic_url_summary.py "https://www.youtube.com/watch?v=..."
"""

import os
import sys
from pathlib import Path

from video_to_summary import OpenAISummarizer, OpenAIWhisperAPITranscriber, URLAudioSource, run


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    output_dir = Path("output")

    source = URLAudioSource(url, output_dir=output_dir, audio_format="mp3")

    # 仅在「视频没有自带字幕」时才会被调用；api_key 留空则无字幕视频会转写失败，
    # 有字幕的视频完全不受影响（字幕优先在前一步就返回了）
    transcriber = OpenAIWhisperAPITranscriber(
        api_key=os.environ.get("OPENAI_API_KEY", ""),
        base_url=os.environ.get("ASR_BASE_URL") or None,
    )

    # 未配置 LLM 时不构造摘要器：pipeline 推 summarize_skipped 事件后仍产出转写文本
    summarizer = None
    if os.environ.get("LLM_API_KEY"):
        summarizer = OpenAISummarizer(
            api_key=os.environ["LLM_API_KEY"],
            model=os.environ.get("LLM_MODEL", ""),
            base_url=os.environ.get("LLM_BASE_URL") or None,
        )

    summary_path = run(source, transcriber, summarizer, output_dir)
    print(summary_path)


if __name__ == "__main__":
    main()
