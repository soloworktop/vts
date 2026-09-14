#!/usr/bin/env python3
"""自定义接入示例：BYOK（任意 OpenAI 兼容端点）+ 转写优化 + 自定义总结模板。

演示三件事：
1. 转写端点与摘要端点可以是**不同**的服务商（各配各的 base_url / model）；
2. 文本优化（`LLMTranscriptPolisher`，CLI 用 `--polish-transcript` 开启）；
3. 用自定义提示词替换内置模板。

用法:
    export ASR_API_KEY=sk-xxx         # 转写端点
    export SUMMARY_API_KEY=sk-xxx     # 摘要/优化端点
    python examples/custom_backend.py /path/to/audio.mp3
"""

import os
from pathlib import Path

from video_to_summary import (
    LLMTranscriptPolisher,
    LocalAudioSource,
    OpenAISummarizer,
    OpenAIWhisperAPITranscriber,
    run,
)

CUSTOM_TEMPLATE = {
    "prompt": (
        "把这份音视频文稿整理成一份投资研究备忘录：先给出一句话结论，"
        "再按「关键数据 / 逻辑链条 / 风险点 / 待验证问题」四段展开，"
        "所有数字与时间点保留原文表述。"
    )
}


def main() -> None:
    audio_path = Path(os.environ.get("DEMO_AUDIO", "output/demo.mp3"))
    output_dir = Path("output")

    source = LocalAudioSource(audio_path, title=audio_path.stem)

    transcriber = OpenAIWhisperAPITranscriber(
        api_key=os.environ.get("ASR_API_KEY", ""),
        model=os.environ.get("ASR_MODEL") or "whisper-1",
        base_url=os.environ.get("ASR_BASE_URL") or None,
    )

    polisher = None
    summarizer = None
    if os.environ.get("SUMMARY_API_KEY"):
        polisher = LLMTranscriptPolisher(
            api_key=os.environ["SUMMARY_API_KEY"],
            model=os.environ.get("SUMMARY_MODEL", ""),
            base_url=os.environ.get("SUMMARY_BASE_URL") or None,
            prompt_preset="light",
        )
        summarizer = OpenAISummarizer(
            api_key=os.environ["SUMMARY_API_KEY"],
            model=os.environ.get("SUMMARY_MODEL", ""),
            base_url=os.environ.get("SUMMARY_BASE_URL") or None,
            template=CUSTOM_TEMPLATE,
        )

    summary_path = run(
        source,
        transcriber,
        summarizer,
        output_dir,
        polisher=polisher,
        polisher_title=audio_path.stem,
    )
    print(summary_path)


if __name__ == "__main__":
    main()
