"""live E2E：真实 uvicorn + 真实下载/转写/LLM（零 fakes），配置读 `.env`。

与离线 e2e（fakes 替换三个网络边界）互补，本文件验证真实边界的端到端装配：
yt-dlp 下载/字幕提取 → Whisper 兼容 ASR → OpenAI 兼容 LLM 总结 → 产物落盘 →
文件回读/导出/日志脱敏。消费的是用户自己的 Key（BYOK），因此：

- 默认整层 skip，`VTS_LIVE_E2E=1` 才运行（CI 无 secrets，不跑本层）；
- 缺 `VTS_LIVE_TEST_URL` 或对应 Key 时逐用例 skip 并给出可行动提示；
- 单次全跑约 2 次下载 + 3-4 次 ASR + 3 次 LLM 调用（建议配短视频控成本）。

运行：`VTS_LIVE_E2E=1 python -m pytest -m live -v` 或 `bash scripts/e2e.sh live`。
"""

import json
import os
from pathlib import Path

import pytest
import requests

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("VTS_LIVE_E2E"),
        reason="真实边界 E2E 默认跳过；设置 VTS_LIVE_E2E=1 运行（配置读 .env）",
    ),
]

# 摘要链全部兜底变量：删干净才能证明「无可用 Key 降级」/「仅 DB 槽位生效」
SUMMARY_KEY_ENVS = ("SUMMARY_API_KEY", "LLM_API_KEY", "OPENAI_API_KEY")
SUMMARY_CFG_ENVS = ("SUMMARY_BASE_URL", "LLM_BASE_URL", "SUMMARY_MODEL", "LLM_MODEL")

TERMINAL_OK = {"completed"}


def _events(detail: dict) -> list[str]:
    return [item["event"] for item in detail.get("progress", [])]


def _event_positions(detail: dict) -> dict[str, int]:
    return {event: idx for idx, event in enumerate(_events(detail))}


def _create_job(base_url: str, payload: dict) -> str:
    res = requests.post(f"{base_url}/api/v1/jobs", json=payload, timeout=15)
    assert res.status_code == 200, f"创建任务失败: {res.status_code} {res.text}"
    return res.json()["job_id"]


def _read_artifact(job_dir: Path, pattern: str) -> Path | None:
    return next(iter(sorted(job_dir.glob(pattern))), None)


def require_live_key(live_settings) -> None:
    """转写/摘要任一槽位有 Key 即可（生产代码两处都做 `asr_key or summary_key` 跨槽兜底）。"""
    if not (live_settings.asr_key or live_settings.summary_key):
        pytest.skip(
            "未配置任何 Key（ASR_API_KEY / SUMMARY_API_KEY，含旧名 LLM_* / OPENAI_API_KEY 别名；"
            "写入 .env 后重跑）"
        )


# ---------------------------------------------------------------- 真实全链

def test_live_url_job_full_chain(live_server, live_url, live_settings, wait_live_job) -> None:
    """URL 任务全链：字幕路径或下载+转写路径 → 真实 LLM 总结 → 产物齐全可回读/导出。"""
    require_live_key(live_settings)
    job_id = _create_job(live_server.base_url, {"source_type": "url", "url": live_url})

    detail = wait_live_job(job_id, TERMINAL_OK)
    pos = _event_positions(detail)

    # 两条合法链（铁律 5 字幕两段式）：字幕优先（跳过下载与转写）或 下载音频 + ASR；
    # 都收敛到 summarize → completed
    if "subtitle_done" in pos:
        assert pos["subtitle_start"] < pos["subtitle_done"]
        assert "download_start" not in pos and "transcribe_start" not in pos
        source_done = pos["subtitle_done"]
    else:
        assert pos["download_start"] < pos["download_done"] < pos["transcribe_start"] < pos["transcribe_done"]
        source_done = pos["transcribe_done"]
    assert source_done < pos["summarize_start"] < pos["summarize_done"] < pos["completed"]

    # 真实标题回填：初始派生为原始 URL，真实 meta 到达后被覆盖（标题回填守卫语义）
    assert detail["title"] and detail["title"] != live_url

    # 产物落盘：summary.md / txt 必有；srt 取决于端点是否回分段
    # （ASR_RESPONSE_FORMAT=json / 字幕无时间轴时无 .srt，属文档化降级，不作硬断言）
    job_dir = live_server.output_dir / job_id
    summary_path = _read_artifact(job_dir, "*.summary.md")
    transcript_path = _read_artifact(job_dir, "*.txt")
    assert summary_path and transcript_path, f"产物缺失: {sorted(p.name for p in job_dir.iterdir())}"
    summary_md = summary_path.read_text(encoding="utf-8")
    assert summary_md.lstrip().startswith("# "), "summary 缺 Markdown 标题骨架"
    assert len(summary_md) > 100, f"summary 内容疑似空壳（{len(summary_md)} 字符）"
    assert transcript_path.read_text(encoding="utf-8").strip()

    srt_path = _read_artifact(job_dir, "*.srt")
    if srt_path:
        assert "-->" in srt_path.read_text(encoding="utf-8")

    # /result 路径字典、/file 回读、/export 附件（format=md 原文件直出）
    assert detail["result_paths"]["summary"] == str(summary_path)
    file_res = requests.get(
        f"{live_server.base_url}/api/v1/jobs/{job_id}/file",
        params={"path": detail["result_paths"]["summary"]},
        timeout=15,
    )
    assert file_res.status_code == 200
    assert file_res.json()["content"] == summary_md

    export_res = requests.get(
        f"{live_server.base_url}/api/v1/jobs/{job_id}/export",
        params={"path": detail["result_paths"]["summary"], "format": "md"},
        timeout=30,
    )
    assert export_res.status_code == 200
    assert "attachment" in export_res.headers.get("content-disposition", "")
    assert export_res.content == summary_path.read_bytes()


# ---------------------------------------------------------------- 真实 ASR（本地文件）

def test_live_local_audio_real_asr(live_server, live_media, live_settings, wait_live_job) -> None:
    """本地真实媒体 → 真实 Whisper API：非缓存转写 + 音频时长指标 + 文本/分段产出。"""
    require_live_key(live_settings)
    job_id = _create_job(live_server.base_url, {
        "source_type": "local",
        "audio_path": str(live_media.audio_path),
        "title": "live本地转写",
    })

    detail = wait_live_job(job_id, TERMINAL_OK)
    pos = _event_positions(detail)
    assert pos["transcribe_start"] < pos["transcribe_done"] < pos["summarize_start"]

    # transcribe_done 指标：真实送 ASR（非缓存命中）+ ffprobe 探到的真实音频时长
    td_payload = detail["progress"][pos["transcribe_done"]]["payload"]
    assert td_payload.get("cached") is False
    assert (td_payload.get("audio_seconds") or 0) > 0, "送 ASR 的音频时长探测失败（ffprobe）"

    job_dir = live_server.output_dir / job_id
    transcript_path = _read_artifact(job_dir, "*.txt")
    assert transcript_path and transcript_path.read_text(encoding="utf-8").strip()

    srt_path = _read_artifact(job_dir, "*.srt")
    if srt_path:
        srt = srt_path.read_text(encoding="utf-8")
        assert "-->" in srt, "SRT 缺时间轴"
        assert srt.strip()[0].isdigit(), "SRT 缺序号块"


# ---------------------------------------------------------------- 真实字幕路径 + 无 Key 降级

def test_live_no_key_subtitle_degradation(live_server, live_url, wait_live_job, monkeypatch) -> None:
    """完全无 Key + 视频自带字幕 → 字幕路径仍出 .txt/.srt + summarize_skipped（铁律 5：不许静默缺产物）。

    无 Key 降级只发生在字幕路径（本地文件/无字幕视频在转写阶段就因缺凭证失败；
    且生产的跨槽兜底 `summary_key or asr_key` 意味着只要配了任一 Key 就会尝试总结）。
    测试 URL 无可用字幕（danmaku 不算）时本场景不适用，skip 并说明。
    """
    for var in SUMMARY_KEY_ENVS + SUMMARY_CFG_ENVS + ("ASR_API_KEY", "ASR_BASE_URL", "ASR_MODEL"):
        monkeypatch.delenv(var, raising=False)

    job_id = _create_job(live_server.base_url, {"source_type": "url", "url": live_url})
    detail = wait_live_job(job_id, {"completed", "failed"})
    if detail["status"] == "failed":
        pytest.skip(
            f"该 VTS_LIVE_TEST_URL 无可用字幕（无 Key 时转写缺凭证失败），场景不适用：{detail.get('error')}"
        )

    events = _events(detail)
    assert "subtitle_done" in events, "无 Key 任务必须走字幕路径完成"
    assert "summarize_skipped" in events, "必须显式推送 summarize_skipped，不允许静默缺产物"
    assert "summarize_start" not in events

    job_dir = live_server.output_dir / job_id
    summary_path = _read_artifact(job_dir, "*.summary.md")
    assert summary_path, "降级也必须产出 summary 骨架文件"
    assert "未生成摘要" in summary_path.read_text(encoding="utf-8")
    transcript_path = _read_artifact(job_dir, "*.txt")
    assert transcript_path and transcript_path.read_text(encoding="utf-8").strip()
    srt_path = _read_artifact(job_dir, "*.srt")
    if srt_path:
        assert "-->" in srt_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 真实故障语义

def test_live_llm_endpoint_unreachable_fails_gracefully(
    live_server, live_media, live_settings, wait_live_job, monkeypatch
) -> None:
    """摘要端点不可达（连接拒绝，确定性、不依赖具体供应商的 401 行为）→ failed + 干净的错误文案。"""
    require_live_key(live_settings)
    # ASR 回落链会借道 summary_base_url：先把 ASR 钉在真实端点，再单独毒化 summary
    asr_base = live_settings.asr_base_url or live_settings.summary_base_url or "https://api.openai.com/v1"
    monkeypatch.setenv("ASR_BASE_URL", asr_base)
    monkeypatch.setenv("SUMMARY_BASE_URL", "http://127.0.0.1:9/v1")

    raw_key = live_settings.summary_key or live_settings.asr_key or ""
    job_id = _create_job(live_server.base_url, {
        "source_type": "local",
        "audio_path": str(live_media.audio_path),
        "title": "live端点不可达",
    })
    detail = wait_live_job(job_id, {"failed"})
    error = detail.get("error") or ""
    assert error.strip(), "失败任务必须带可读错误"
    assert raw_key not in error, "错误文案泄漏 API Key"
    assert "Traceback" not in error, "错误文案裸崩（未走 _user_facing_error 映射）"


# ---------------------------------------------------------------- DB 槽位优先（真实 Key + Fernet）

def test_live_llm_slot_via_api_with_real_key(
    live_server, live_media, live_settings, wait_live_job, monkeypatch
) -> None:
    """PUT /api/v1/llm 写入真实 Key（Fernet 加密入库）→ 删摘要链 env → 摘要只能来自 DB 槽位。"""
    if not live_settings.summary_key:
        pytest.skip("未配置 SUMMARY_API_KEY（或兼容别名），无法验证 DB 槽位优先")

    put_res = requests.put(f"{live_server.base_url}/api/v1/llm", json={"summary": {
        "api_key": live_settings.summary_key,
        "base_url": live_settings.summary_base_url or "",
        "model": live_settings.summary_model or "",
    }}, timeout=15)
    assert put_res.status_code == 200

    # 槽位视图：configured + 掩码，绝不含真实 Key（铁律 6，用真值回归）
    view = requests.get(f"{live_server.base_url}/api/v1/llm", timeout=10).json()
    assert view["summary"]["configured"] is True
    assert live_settings.summary_key not in json.dumps(view), "槽位视图泄漏真实 Key"

    # 删光摘要链 env：任务再想总结就只能解密 DB 槽位
    for var in SUMMARY_KEY_ENVS + SUMMARY_CFG_ENVS:
        monkeypatch.delenv(var, raising=False)

    job_id = _create_job(live_server.base_url, {
        "source_type": "local",
        "audio_path": str(live_media.audio_path),
        "title": "live槽位优先",
    })
    detail = wait_live_job(job_id, TERMINAL_OK)
    assert "summarize_start" in _events(detail), "env 删空后摘要未发生 → DB 槽位未生效"


# ---------------------------------------------------------------- 日志脱敏（真值回归）

def test_live_log_export_no_real_key_leak(live_server, live_media, live_settings, wait_live_job) -> None:
    """真实任务跑完后导出诊断日志：.env 里的真实 Key 绝不能出现在导出包（用真值做脱敏回归）。"""
    real_keys = {
        k for k in (
            live_settings.asr_key,
            live_settings.summary_key,
            os.environ.get("OPENAI_API_KEY"),
        ) if k
    }
    if not real_keys:
        pytest.skip("未配置任何 Key，无真值可断言")

    job_id = _create_job(live_server.base_url, {
        "source_type": "local",
        "audio_path": str(live_media.audio_path),
        "title": "live日志脱敏",
    })
    detail = wait_live_job(job_id, TERMINAL_OK)

    bundle = requests.get(f"{live_server.base_url}/api/v1/logs/export", timeout=30).text
    # 「最近任务」段的 job id 截断为前 8 字符（log_export 脱敏约定），按前缀断言
    assert job_id[:8] in bundle, "本次任务的记录未进入诊断日志导出（断言对象缺失）"
    for key in real_keys:
        assert key not in bundle, f"真实 Key 泄漏进诊断日志导出（{key[:4]}****）"
