"""Windows 子进程控制台弹窗抑制（坑条目见根 AGENTS.md「关键约定与坑」）。

现象：Windows GUI 形态（桌面 App = PyInstaller ``--windowed``）spawn 控制台程序
（ffmpeg/ffprobe/powershell，以及 yt-dlp 内部音频提取的 ffmpeg）时，系统为每个
子进程新开黑色控制台窗口（Win11 默认终端托管下退出后还可能停留显示退出码）。

三层防护，本文件全覆盖：
1. spawn 点显式展开 ``utils.subprocess_no_window_kwargs()``
2. 桌面入口 ``desktop.main()`` 安装 Popen 进程级守卫（覆盖第三方库内部 spawn）
3. spawn 点防漂移扫描：包内 ``subprocess.run/Popen`` 所在文件必须引入该 kwargs
"""

import os
import subprocess
import sys
from pathlib import Path

from video_to_summary.utils import (
    _WINDOWS_CREATE_NO_WINDOW,
    _merge_no_window_flags,
    install_windows_subprocess_guard,
    subprocess_no_window_kwargs,
)

_SRC = Path(__file__).resolve().parents[1] / "src" / "video_to_summary"


def test_kwargs_posix_is_noop(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    assert subprocess_no_window_kwargs() == {}
    assert _merge_no_window_flags(None) == 0


def test_kwargs_windows_carries_create_no_window(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    kwargs = subprocess_no_window_kwargs()
    assert set(kwargs) == {"creationflags"}
    assert kwargs["creationflags"] & _WINDOWS_CREATE_NO_WINDOW


def test_merge_flags_merges_and_idempotent(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    assert _merge_no_window_flags(None) == _WINDOWS_CREATE_NO_WINDOW
    assert _merge_no_window_flags(0) == _WINDOWS_CREATE_NO_WINDOW
    # 已带标志时 OR 合并保持幂等，不得覆盖调用方显式传入的其它创建标志
    explicit = 0x00000008  # DETACHED_PROCESS
    assert _merge_no_window_flags(explicit) == explicit | _WINDOWS_CREATE_NO_WINDOW
    assert _merge_no_window_flags(_WINDOWS_CREATE_NO_WINDOW) == _WINDOWS_CREATE_NO_WINDOW


def test_guard_idempotent_and_posix_passthrough():
    install_windows_subprocess_guard()
    install_windows_subprocess_guard()  # 幂等：二次安装不得叠加包装
    assert getattr(subprocess.Popen, "_vts_no_window_guard", False) is True
    # 非 Windows 平台守卫必须等价直通：普通 spawn 行为不受影响
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert proc.returncode == 0


def test_run_through_guard_still_works():
    """subprocess.run（内部走 Popen）在守卫安装后照常工作。"""
    install_windows_subprocess_guard()
    result = subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.strip() == "ok"


def test_all_spawn_sites_adopt_no_window_kwargs():
    """防漂移：包内任何 subprocess.run/Popen 所在文件必须引入无窗口 kwargs。"""
    offenders = []
    for py in sorted(_SRC.rglob("*.py")):
        text = py.read_text(encoding="utf-8")
        has_spawn = "subprocess.run(" in text or "subprocess.Popen(" in text
        if has_spawn and "subprocess_no_window_kwargs" not in text:
            offenders.append(py.relative_to(_SRC).as_posix())
    assert offenders == []
