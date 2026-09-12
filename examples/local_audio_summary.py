#!/usr/bin/env python3
"""本地音频/视频文件示例：直接对本地文件做转写和总结（BYOK）。

用法:
    python examples/local_audio_summary.py /path/to/audio.mp3

运行前设置环境变量：
    export OPENAI_API_KEY=sk-xxx     # 转写（OpenAI 兼容 /audio/transcriptions）
    export LLM_API_KEY=sk-xxx        # 摘要（可留空：则只出转写文本）
    export LLM_BASE_URL=https://api.deepseek.com/v1
    export LLM_MODEL=deepseek-chat
"""

import os
import sys
from pathlib import Path

from video_to_summary import (
    LocalAudioSource,
    OpenAISummarizer,
    OpenAIWhisperAPITranscriber,
    run,
)


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("用法: python examples/local_audio_summary.py <音视频文件路径>")
    audio_path = Path(sys.argv[1])
    output_dir = Path("output")

    source = LocalAudioSource(audio_path, title=audio_path.stem)

    transcriber = OpenAIWhisperAPITranscriber(
        api_key=os.environ.get("OPENAI_API_KEY", ""),
        model=os.environ.get("ASR_MODEL") or "whisper-1",
        base_url=os.environ.get("ASR_BASE_URL") or None,
    )

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
