"""sources/url.py 纯逻辑单测（不触网）：中间视频清理白名单。"""

from pathlib import Path

from video_to_summary.sources.url import _remove_intermediate_video


def _make(d: Path, name: str) -> Path:
    p = d / name
    p.write_text("x", encoding="utf-8")
    return p


def test_remove_intermediate_video_only_touches_whitelist(tmp_path):
    video = _make(tmp_path, "abc123.webm")
    keep = [
        _make(tmp_path, "abc123.wav"),
        _make(tmp_path, "abc123.txt"),
        _make(tmp_path, "abc123.segments.json"),
        _make(tmp_path, "abc123.srt"),
        _make(tmp_path, "abc123.polished.txt"),
        _make(tmp_path, "abc123.summary.md"),
        _make(tmp_path, "abc123.zh-Hans.vtt"),
    ]

    _remove_intermediate_video(tmp_path, "abc123")

    assert not video.exists()
    for p in keep:
        assert p.exists(), f"cached artifact must survive: {p.name}"


def test_remove_intermediate_video_covers_common_containers(tmp_path):
    removed = [_make(tmp_path, f"vid{ext}") for ext in (".mkv", ".mp4", ".flv", ".ts")]
    _remove_intermediate_video(tmp_path, "vid")
    assert all(not p.exists() for p in removed)


def test_remove_intermediate_video_missing_files_no_error(tmp_path):
    # 目录里根本没有该 id 的任何文件：静默通过
    _remove_intermediate_video(tmp_path, "nonexistent")


# ---------------------------------------------------------------- 下载进度节流 hook

from video_to_summary.sources.url import _make_download_progress_hook


def test_download_progress_hook_throttles_small_steps():
    """小步进 + 1s 内连发 → 节流；大步进（≥5%）立即放行。"""
    emitted: list[dict] = []
    hook = _make_download_progress_hook(emitted.append)
    hook({"status": "downloading", "downloaded_bytes": 10, "total_bytes": 100, "speed": 5, "eta": 9})
    hook({"status": "downloading", "downloaded_bytes": 11, "total_bytes": 100, "speed": 5, "eta": 9})
    hook({"status": "downloading", "downloaded_bytes": 12, "total_bytes": 100, "speed": 5, "eta": 9})
    assert len(emitted) == 1  # 10% → 11%/12% 均被节流
    hook({"status": "downloading", "downloaded_bytes": 60, "total_bytes": 100, "speed": 5, "eta": 4})
    assert len(emitted) == 2  # 60%：步进 50% ≥ 5% 必发
    assert emitted[-1]["percent"] == 60.0
    assert emitted[-1]["speed"] == 5 and emitted[-1]["eta"] == 4


def test_download_progress_hook_clamps_percent_to_100():
    """total_bytes_estimate 低估实际字节时 percent 钳制到 100%，不外溢。"""
    emitted: list[dict] = []
    hook = _make_download_progress_hook(emitted.append)
    hook({"status": "downloading", "downloaded_bytes": 1200, "total_bytes": 1000, "speed": 5, "eta": 0})
    hook({"status": "downloading", "downloaded_bytes": 1300, "total_bytes": 1000, "speed": 5, "eta": 0})
    assert all(e["percent"] is None or e["percent"] <= 100.0 for e in emitted)
    assert emitted[-1]["percent"] == 100.0


def test_download_progress_hook_finished_always_emits_100():
    """finished 终态必达 100%，不经节流（即使紧跟在一条刚发出的进度后）。"""
    emitted: list[dict] = []
    hook = _make_download_progress_hook(emitted.append)
    hook({"status": "downloading", "downloaded_bytes": 10, "total_bytes": 100})
    hook({"status": "finished", "downloaded_bytes": 100, "total_bytes": 100})
    assert emitted[-1]["percent"] == 100.0
    assert emitted[-1]["eta"] == 0


def test_download_progress_hook_unknown_total_degrades_to_bytes():
    """总大小未知（total_bytes 与 estimate 均缺）→ percent=None，1s 内只发一条。"""
    emitted: list[dict] = []
    hook = _make_download_progress_hook(emitted.append)
    hook({"status": "downloading", "downloaded_bytes": 2048})
    hook({"status": "downloading", "downloaded_bytes": 4096})
    assert len(emitted) == 1
    assert emitted[0]["percent"] is None
    assert emitted[0]["downloaded_bytes"] == 2048
    # total_bytes 缺失但 total_bytes_estimate 存在 → 正常计算百分比
    hook({"status": "downloading", "downloaded_bytes": 500, "total_bytes_estimate": 1000})
    assert emitted[-1]["percent"] == 50.0


def test_download_progress_hook_swallows_callback_errors():
    """进度回调异常只记日志，绝不中断下载主流程。"""

    def _boom(_payload: dict) -> None:
        raise RuntimeError("callback exploded")

    hook = _make_download_progress_hook(_boom)
    hook({"status": "downloading", "downloaded_bytes": 10, "total_bytes": 100})
    hook({"status": "finished", "downloaded_bytes": 100, "total_bytes": 100})


def test_download_progress_hook_ignores_other_statuses():
    emitted: list[dict] = []
    hook = _make_download_progress_hook(emitted.append)
    hook({"status": "error", "downloaded_bytes": 10, "total_bytes": 100})
    hook({})
    assert emitted == []
