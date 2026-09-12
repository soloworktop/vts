"""故障注入 E2E：模拟多类真实故障 → 导出诊断包 → 断言证据链完整。

这是「日志导出」功能的核心验收：技术员只拿到导出的 .txt，就必须能
还原每次故障的 when/what/why。覆盖：
1. 转写阶段异常（异常消息内嵌假 API Key → 验证异常内嵌敏感信息的脱敏）
2. 下载阶段异常（URL 源网络失败）
3. 摘要阶段异常（LLM API 401）
4. 用户取消运行中任务
5. 无 Key 优雅降级（completed，非故障但状态应在案）
"""

import pathlib
import tempfile

import pytest
import requests

pytestmark = pytest.mark.e2e

FAKE_KEY = "sk-abcdefgh12345678"


class BoomSource:
    """resolve 直接抛网络异常的 URL 源假体（模拟下载阶段失败）。"""

    meta = None

    def resolve(self):
        raise ConnectionError("E2E 故障注入：视频下载失败（网络超时）")


def _create(base_url, payload):
    res = requests.post(f"{base_url}/api/v1/jobs", json=payload, timeout=5)
    assert res.status_code == 200
    return res.json()["job_id"]


def test_fault_injection_log_coverage(ui_page, e2e_server, wait_job, monkeypatch, tmp_path):
    from video_to_summary.web import tasks as web_tasks

    from fakes import FakeUrlSource

    base = e2e_server.base_url
    media = tmp_path / "media" / "fault.mp3"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"\xff\xfb" + b"0" * 64)

    # ---- 故障 1：转写阶段异常（消息内嵌假 API Key）----
    e2e_server.transcriber.fail = RuntimeError(f"E2E 故障注入：转写超时 key={FAKE_KEY}")
    j1 = _create(base, {"source_type": "local", "audio_path": str(media), "title": "故障-转写失败"})
    detail = wait_job(j1, {"failed"})
    assert "转写超时" in (detail["error"] or "")

    # ---- 故障 2：下载阶段异常 ----
    e2e_server.transcriber.fail = None
    monkeypatch.setattr(web_tasks, "_build_source", lambda settings, job: BoomSource())
    j2 = _create(base, {"source_type": "url", "url": "https://example.test/boom", "title": "故障-下载失败"})
    detail = wait_job(j2, {"failed"})
    assert "视频下载失败" in (detail["error"] or "")

    # ---- 故障 3：摘要阶段异常（LLM API 401）----
    # BYOK：认证类故障对用户不是"联系维护人员"，而是"去设置检查自己的 Key/base_url/模型名"
    monkeypatch.setattr(web_tasks, "_build_source", lambda settings, job: FakeUrlSource(media))
    e2e_server.summarizer.fail = RuntimeError("E2E 故障注入：LLM API 401 Unauthorized")
    j3 = _create(base, {"source_type": "local", "audio_path": str(media), "title": "故障-摘要失败"})
    detail = wait_job(j3, {"failed"})
    assert "认证失败" in (detail["error"] or "")
    assert "设置" in (detail["error"] or "")

    # ---- 故障 4：用户取消运行中任务 ----
    e2e_server.summarizer.fail = None
    e2e_server.transcriber.delay = 3.0
    j4 = _create(base, {"source_type": "local", "audio_path": str(media), "title": "故障-手动取消"})
    wait_job(j4, {"running"})
    assert requests.post(f"{base}/api/v1/jobs/{j4}/cancel", timeout=5).status_code == 200
    wait_job(j4, {"cancelled"})
    e2e_server.transcriber.delay = 0

    # ---- 场景 5：无 Key 优雅降级（completed，非故障但在案）----
    monkeypatch.setattr(web_tasks, "_build_summarizer", lambda settings, cfg: None)
    j5 = _create(base, {"source_type": "local", "audio_path": str(media), "title": "场景-无Key降级"})
    wait_job(j5, {"completed"})

    # ================= 导出并断言证据链 =================
    res = requests.get(f"{base}/api/v1/logs/export", timeout=5)
    assert res.status_code == 200
    bundle = res.text

    # 任务段：5 条全部可见，状态计数准确
    for jid in (j1, j2, j3, j4, j5):
        assert f"id={jid[:8]} | type=" in bundle
    assert bundle.count("status=failed") >= 3
    assert "status=cancelled" in bundle and "status=completed" in bundle

    # 失败原因逐项可还原
    assert "E2E 故障注入：转写超时" in bundle
    assert "E2E 故障注入：视频下载失败" in bundle
    assert "E2E 故障注入：LLM API 401" in bundle
    assert "cancelled by user" in bundle

    # 异常消息内嵌的假 Key 必须无泄漏；脱敏标记存在（说明此处曾有凭证被隐藏）。
    # 注：规则叠加后最终形态是 "key=[REDACTED]"（查询参数规则会吞掉内层
    # sk- 标记），安全性质（无泄漏 + 有标记）才是本断言的核心。
    assert FAKE_KEY not in bundle
    assert "[REDACTED]" in bundle

    # 运行日志段：每个失败任务都有完整 traceback（logger.exception）
    tracebacks = bundle.count("Traceback (most recent call last)")
    assert tracebacks >= 3, f"预期 ≥3 个失败任务的 traceback，实际 {tracebacks}"

    # 时间线可对齐：任务 created 为可读本地时间
    assert "created=20" in bundle


def test_fault_log_level_coverage(e2e_server, wait_job):
    """运行日志级别覆盖：失败任务产生 ERROR，阶段/启动产生 INFO。

    pytest 会把 root logger 压到 WARNING；生产环境 configure_logging 为 INFO
    （用户真实导出含 INFO 行可证）。此处显式镜像生产级别再断言。
    """
    import logging as _logging

    from video_to_summary.log_export import get_ring_handler

    e2e_server.transcriber.fail = RuntimeError("级别覆盖检查")
    media = pathlib.Path(tempfile.mkdtemp()) / "lv.mp3"
    media.write_bytes(b"\xff\xfb00")

    root = _logging.getLogger()
    original_level = root.level
    root.setLevel(_logging.INFO)
    try:
        jid = _create(e2e_server.base_url, {"source_type": "local", "audio_path": str(media), "title": "级别覆盖"})
        wait_job(jid, {"failed"})
    finally:
        root.setLevel(original_level)

    handler = get_ring_handler()
    assert handler is not None
    levels = {line.split(" ")[2] for line in handler.buffer if line.count(" ") >= 2}
    assert "ERROR" in levels
    assert "INFO" in levels  # 启动/阶段 INFO 同样在案
