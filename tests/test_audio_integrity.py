"""番剧/会员视频「音频完整下载」网络验证。

实测结论：匿名访问会员番剧时，B 站只下发前 60 秒预览音频，并非完整音频；
要完整下载（音频覆盖源视频全长）必须提供大会员 cookies。

- test_bangumi_audio_anonymous_only_preview：匿名 → 仅拿到预览片段（记录该限制）
- test_bangumi_audio_full_with_cookies：提供 VTS_BILI_COOKIES（cookies.txt 路径）时 → 断言完整

需要联网；默认跳过，设置 VTS_NETWORK_TESTS=1 运行：
    VTS_NETWORK_TESTS=1 python -m pytest tests/test_audio_integrity.py -s -v
"""

import os
from pathlib import Path

import pytest

from video_to_summary.downloader import probe_duration_seconds

pytestmark = pytest.mark.skipif(
    not os.environ.get("VTS_NETWORK_TESTS"),
    reason="需要联网；设置 VTS_NETWORK_TESTS=1 运行网络验证",
)

# 目标：B 站番剧集（会员内容，匿名仅免费预览）
BANGUMI_EPISODE = "https://www.bilibili.com/bangumi/play/ep4721970"
DURATION_TOLERANCE = 15.0  # 秒


def _download_audio(tmp_path: Path, cookies: str | None = None):
    from video_to_summary.sources.url import URLAudioSource

    src = URLAudioSource(
        BANGUMI_EPISODE,
        output_dir=tmp_path,
        audio_format="mp3",
        cookies=Path(cookies) if cookies else None,
    )
    audio_path, meta = src.resolve()
    assert audio_path.exists() and audio_path.stat().st_size > 0, "音频文件缺失或为空"
    return audio_path, meta


@pytest.mark.network
def test_bangumi_audio_anonymous_only_preview(tmp_path) -> None:
    """匿名下载：断言只拿到预览片段（音频时长明显小于源视频），记录会员限制。"""
    audio_path, meta = _download_audio(tmp_path)
    duration = probe_duration_seconds(audio_path)
    assert duration is not None, "ffprobe 无法读取音频"
    print(f"\n匿名: 音频 {duration:.1f}s, 源视频 {meta.duration}s")
    assert meta.duration and duration < meta.duration - DURATION_TOLERANCE, (
        "预期匿名仅预览；若时长已接近源时长，说明该集已开放（限制可能变化）"
    )
    print(f"→ 会员限制：仅预览 {duration:.1f}s / 全长 {meta.duration}s（未完整下载）")


@pytest.mark.network
@pytest.mark.skipif(
    not os.environ.get("VTS_BILI_COOKIES"),
    reason="未提供 VTS_BILI_COOKIES（大会员 cookies.txt 路径），跳过完整下载验证",
)
def test_bangumi_audio_full_with_cookies(tmp_path) -> None:
    """提供大会员 cookies：断言音频完整（时长 ≈ 源视频全长）。"""
    cookies = os.environ["VTS_BILI_COOKIES"]
    audio_path, meta = _download_audio(tmp_path, cookies)
    duration = probe_duration_seconds(audio_path)
    assert duration is not None
    print(f"\n带 cookies: 音频 {duration:.1f}s, 源视频 {meta.duration}s")
    assert abs(duration - (meta.duration or 0)) < DURATION_TOLERANCE, (
        f"音频 {duration:.1f}s 与源 {meta.duration}s 差异过大，可能未完整下载"
    )
    print(f"→ 完整下载 ✓（音频覆盖源全长 {duration:.1f}s）")
