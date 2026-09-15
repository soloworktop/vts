"""live E2E：真实 CLI 子进程（`python -m video_to_summary.main <url>`）走完整链。

CLI 与 Web 是两条独立消费路径：CLI 不读 DB 槽位，配置纯靠 CLI 旗标 + 环境变量
（`main.py` 自行 `load_dotenv(find_dotenv(usecwd=True))`，与 pytest 同在仓库根
执行 → 找到同一个 `.env`）。本文件验证真实下载/转写/LLM 下的 CLI 退出码、
stdout 契约（`summary -> <path>`）与产物落盘。

门控同 test_live_web：默认整层 skip（`VTS_LIVE_E2E=1` 才运行）；
缺 `VTS_LIVE_TEST_URL` / Key 时 skip 并给出可行动提示。
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from video_to_summary.utils import subprocess_no_window_kwargs

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("VTS_LIVE_E2E"),
        reason="真实边界 E2E 默认跳过；设置 VTS_LIVE_E2E=1 运行（配置读 .env）",
    ),
]


def test_live_cli_full_chain(live_env, live_url, live_settings, live_timeout, tmp_path) -> None:
    """CLI 完整链：真实下载 → 转写/字幕 → LLM 总结 → 退出码 0 + 产物落盘。"""
    if not (live_settings.asr_key or live_settings.summary_key):
        pytest.skip(
            "未配置任何 Key（ASR_API_KEY / SUMMARY_API_KEY，含旧名 LLM_* / OPENAI_API_KEY 别名；"
            "写入 .env 后重跑）"
        )

    out_dir = tmp_path / "cli-output"
    proc = subprocess.run(
        [
            sys.executable, "-m", "video_to_summary.main", live_url,
            "--output-dir", str(out_dir),
            # 用户 .env 若开了 POLISH_TRANSCRIPT，CLI 侧显式关闭：本用例锚定「下载→转写→总结」主链
            "--no-polish-transcript",
        ],
        capture_output=True,
        text=True,
        timeout=live_timeout + 120,
        env=dict(os.environ),
        **subprocess_no_window_kwargs(),
    )
    assert proc.returncode == 0, (
        f"CLI 退出码 {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "Traceback" not in proc.stderr, f"stderr 出现裸 traceback:\n{proc.stderr}"

    # stdout 契约：`summary -> <summary.md 路径>`（main.py 唯一的成功输出）
    assert "summary -> " in proc.stdout, f"stdout 缺 summary 路径输出:\n{proc.stdout}"
    summary_path = Path(proc.stdout.strip().rsplit("summary -> ", 1)[1].strip())
    assert summary_path.is_file(), f"stdout 声称的产物不存在: {summary_path}"

    summary_md = summary_path.read_text(encoding="utf-8")
    assert summary_md.lstrip().startswith("# "), "summary 缺 Markdown 标题骨架"
    assert len(summary_md) > 100, f"summary 内容疑似空壳（{len(summary_md)} 字符）"

    transcript_path = summary_path.with_name(summary_path.name.replace(".summary.md", ".txt"))
    assert transcript_path.is_file(), f"转写产物缺失: {transcript_path}"
    assert transcript_path.read_text(encoding="utf-8").strip()

    srt_path = summary_path.with_name(summary_path.name.replace(".summary.md", ".srt"))
    if srt_path.is_file():
        assert "-->" in srt_path.read_text(encoding="utf-8")
