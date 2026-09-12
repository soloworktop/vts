"""cancel_utils 单元测试：线程 + 轮询取消的各条路径（LLM/下载中途取消的唯一实现）。"""

import threading
import time

import pytest

from video_to_summary.cancel_utils import call_with_cancel


class _Cancelled(BaseException):
    """模拟 JobCancelledError（BaseException 子类，不被业务 except Exception 吞掉）。"""


def test_returns_result_when_cancel_never_fires() -> None:
    assert call_with_cancel(lambda: 42, lambda: None) == 42


def test_func_exception_propagates() -> None:
    def boom():
        raise ValueError("upstream broken")

    with pytest.raises(ValueError):
        call_with_cancel(boom, lambda: None)


def test_cancel_reraises_and_calls_on_cancel() -> None:
    """cancel_check 抛取消异常 → on_cancel 被调用（中断 HTTP）→ 取消异常 re-raise。"""
    started = threading.Event()
    release = threading.Event()
    on_cancel_calls: list[int] = []

    def func() -> str:
        started.set()
        release.wait(5)  # 模拟长阻塞 HTTP；on_cancel 后立即放行
        return "late"

    def cancel_check() -> None:
        if started.is_set():
            raise _Cancelled()

    def on_cancel() -> None:
        on_cancel_calls.append(1)
        release.set()

    t0 = time.perf_counter()
    with pytest.raises(_Cancelled):
        call_with_cancel(func, cancel_check, on_cancel=on_cancel, cancel_log_msg="x")
    elapsed = time.perf_counter() - t0

    assert on_cancel_calls == [1]
    # 轮询周期 0.5s：取消应在秒级生效，而不是等阻塞调用跑满 5s
    assert elapsed < 3


def test_poll_interval_allows_fast_completion() -> None:
    """不取消时行为与直接调用一致（结果透传、无额外等待）。"""
    t0 = time.perf_counter()
    assert call_with_cancel(lambda: "ok", lambda: None) == "ok"
    assert time.perf_counter() - t0 < 1
