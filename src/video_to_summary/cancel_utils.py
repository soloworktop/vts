"""线程 + 轮询取消的公共工具。

用于在同步阻塞调用（LLM HTTP / yt-dlp 下载）中支持中途取消：后台线程
跑阻塞调用，主线程每 0.5s 轮询 cancel_check，收到取消信号时中断调用
（LLM 用 client.close()，yt-dlp 线程作为 daemon 自然消亡）。
"""
import logging
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger("video_to_summary.cancel")


def call_with_cancel(
    func: Callable[[], Any],
    cancel_check: Callable[[], None],
    *,
    on_cancel: Optional[Callable[[], None]] = None,
    cancel_log_msg: str = "operation cancelled by user",
) -> Any:
    """在后台线程执行 *func*，主线程轮询 *cancel_check* 实现中途取消。

    - cancel_check 抛异常 → 调用 on_cancel（如 client.close 中断 HTTP）→ re-raise 该异常
    - func 正常完成 → 返回其返回值
    - func 抛异常 → re-raise 该异常

    cancel_check 抛的异常通常是 JobCancelledError(BaseException)，不会被
    业务层 except Exception 吞掉。
    """
    result_holder: dict = {}

    def _worker():
        try:
            result_holder["result"] = func()
        except Exception as exc:
            result_holder["error"] = exc

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

    while t.is_alive():
        t.join(timeout=0.5)
        if t.is_alive():
            try:
                cancel_check()
            except BaseException:
                if on_cancel is not None:
                    try:
                        on_cancel()
                    except Exception:
                        pass
                t.join(timeout=5.0)
                logger.info(cancel_log_msg)
                raise

    if "error" in result_holder:
        raise result_holder["error"]
    return result_holder.get("result")
