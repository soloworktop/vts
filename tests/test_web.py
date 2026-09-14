import os
from pathlib import Path

import json
import pytest
from fastapi.testclient import TestClient

from video_to_summary.web.app import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolate_db(tmp_path, monkeypatch):
    """每个测试独立使用临时 SQLite、清空内存任务缓存，并禁用真实后台任务调度。"""
    from video_to_summary import db as store_db
    from video_to_summary.web import app as web_app
    from video_to_summary.web import tasks as web_tasks

    monkeypatch.setattr(store_db, "DATABASE_PATH", tmp_path / "app.db")
    monkeypatch.setattr(store_db, "LEGACY_LLM_FILE", tmp_path / "missing_llm_profiles.json")
    monkeypatch.setattr(store_db, "LEGACY_TEMPLATE_FILE", tmp_path / "missing_summary_templates.json")
    store_db.reset()
    web_tasks._jobs.clear()
    web_tasks._cancel_flags.clear()
    # POST /api/v1/jobs 不再真实调度后台任务（避免下载/网络副作用污染测试）。
    # 注意 patch app 模块内已绑定的 enqueue_job 引用，以及 tasks 模块内的（retry_job 直接调用）。
    monkeypatch.setattr(web_app, "enqueue_job", lambda job: None)
    monkeypatch.setattr(web_tasks, "enqueue_job", lambda job: None)
    yield


def test_index_page_exists() -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert "VTS 控制台" in res.text


def test_create_job_returns_job_id() -> None:
    payload = {
        "source_type": "url",
        "url": "https://example.test/video",
        "audio_format": "wav",
    }
    res = client.post("/api/v1/jobs", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["job_id"]
    assert data["status"] == "pending"


def test_get_job_not_found() -> None:
    res = client.get("/api/v1/jobs/not-exist")
    assert res.status_code == 404


def test_static_files_are_served() -> None:
    res = client.get("/static/index.html")
    assert res.status_code == 200


def test_static_dir_unset_uses_bundled_static(monkeypatch) -> None:
    """未设置 VTS_STATIC_DIR → 首页与 /static/* 走包内 web/static/（默认行为不回归）。"""
    monkeypatch.delenv("VTS_STATIC_DIR", raising=False)
    res = client.get("/")
    assert res.status_code == 200
    # 包内 static 的构建产物首页与「前端未构建」占位页都含该标识；两者都是「包内」语义
    assert "VTS 控制台" in res.text
    # 包内 static 中恒被跟踪的 user-guide.html 可正常访问
    assert client.get("/static/user-guide.html").status_code == 200


def test_static_dir_override_serves_custom_frontend(tmp_path, monkeypatch) -> None:
    """VTS_STATIC_DIR 指向含 index.html 的目录 → 首页与 /static/* 均由该目录提供。"""
    frontend = tmp_path / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text(
        "<!DOCTYPE html><html><head><title>custom-frontend-marker</title></head>"
        "<body><div id='root'></div></body></html>",
        encoding="utf-8",
    )
    (frontend / "assets" / "x.js").write_text("console.log('custom x.js');", encoding="utf-8")
    monkeypatch.setenv("VTS_STATIC_DIR", str(frontend))

    # 首页读覆盖目录的 index.html
    res = client.get("/")
    assert res.status_code == 200
    assert "custom-frontend-marker" in res.text
    assert "VTS 控制台" not in res.text  # 不再是包内首页

    # /static/* 也来自覆盖目录；no-cache 语义不变
    asset = client.get("/static/assets/x.js")
    assert asset.status_code == 200
    assert asset.text == "console.log('custom x.js');"
    assert asset.headers["cache-control"] == "no-cache"
    assert client.get("/static/index.html").text == (frontend / "index.html").read_text(encoding="utf-8")
    # 覆盖目录里没有的文件 → 404（静态不回落到包内）
    assert client.get("/static/user-guide.html").status_code == 404

    # 覆盖点不影响 /api/v1 与 /guide（_guide_html_path 仍指向包内手册）
    assert client.get("/api/v1/health").status_code == 200
    guide = client.get("/guide")
    assert guide.status_code == 200
    assert "VTS 使用指引" in guide.text


def test_static_dir_invalid_falls_back_to_bundled(tmp_path, monkeypatch) -> None:
    """VTS_STATIC_DIR 无效（不存在/不是目录/缺 index.html）→ 回落包内 static，进程不崩。"""
    from video_to_summary.web import app as web_app

    # 复位兜底 warning 标志，使本用例不依赖其它用例的执行顺序
    monkeypatch.setattr(web_app, "_static_dir_fallback_warned", False)

    cases = [tmp_path / "no-such-dir"]  # 不存在
    file_only = tmp_path / "a-file"
    file_only.write_text("not a dir", encoding="utf-8")
    cases.append(file_only)  # 指向文件而非目录
    no_index = tmp_path / "no-index"
    no_index.mkdir()
    (no_index / "other.txt").write_text("x", encoding="utf-8")
    cases.append(no_index)  # 目录存在但缺 index.html

    for bad in cases:
        monkeypatch.setenv("VTS_STATIC_DIR", str(bad))
        res = client.get("/")
        assert res.status_code == 200, f"VTS_STATIC_DIR={bad} 不应导致首页崩溃"
        assert "VTS 控制台" in res.text  # 回落包内语义（构建产物首页或占位页）
        assert client.get("/static/user-guide.html").status_code == 200
        assert client.get("/api/v1/health").status_code == 200


def test_static_dir_invalid_logs_warning_once(tmp_path, monkeypatch, caplog) -> None:
    """无效 VTS_STATIC_DIR 只打一次 warning（回落包内），多个请求不刷屏。"""
    import logging

    from video_to_summary.web import app as web_app

    monkeypatch.setattr(web_app, "_static_dir_fallback_warned", False)
    monkeypatch.setenv("VTS_STATIC_DIR", str(tmp_path / "missing"))

    with caplog.at_level(logging.WARNING, logger="video_to_summary.web"):
        client.get("/")
        client.get("/static/user-guide.html")
        client.get("/api/v1/health")

    warned = [r for r in caplog.records if "VTS_STATIC_DIR" in r.getMessage()]
    assert len(warned) == 1, f"应只打一次回落 warning，实际 {len(warned)} 条"


def test_guide_page_serves_bundled_placeholder() -> None:
    """开源版自带 web/static/user-guide.html 占位页：/guide 恒可用，不留死链。"""
    res = client.get("/guide")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert "VTS 使用指引" in res.text


def test_guide_page_prefers_bundled_path(tmp_path, monkeypatch) -> None:
    """打包产物：包内 static/user-guide.html 存在时优先（构建脚本自 docs/ 拷入）。"""
    from video_to_summary.web import app as web_app

    bundled = tmp_path / "user-guide.html"
    bundled.write_text("<html><title>bundle-guide-marker</title></html>", encoding="utf-8")
    monkeypatch.setattr(web_app, "_guide_html_path", lambda: bundled)
    res = client.get("/guide")
    assert res.status_code == 200
    assert "bundle-guide-marker" in res.text


def test_guide_page_missing_returns_404(monkeypatch) -> None:
    """手册文件与回退路径都缺失 → 404（不 crash）。"""
    from video_to_summary.web import app as web_app

    monkeypatch.setattr(web_app, "_guide_html_path", lambda: None)
    res = client.get("/guide")
    assert res.status_code == 404


def test_fs_browse_lists_dirs_and_media(tmp_path) -> None:
    # 构造：一个子目录 + 一个音频 + 一个非媒体文件 + 一个隐藏项
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "video.mp4").write_bytes(b"x")
    (tmp_path / "music.mp3").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    (tmp_path / ".DS_Store").write_bytes(b"x")

    res = client.get("/api/v1/fs/browse", params={"path": str(tmp_path)})
    assert res.status_code == 200
    data = res.json()
    assert data["error"] is None
    assert data["path"] == str(tmp_path.resolve())
    # 目录在前
    assert data["dirs"][0]["name"] == "sub"
    assert data["dirs"][0]["path"] == str(tmp_path / "sub")
    file_names = {f["name"] for f in data["files"]}
    assert file_names == {"music.mp3"}  # 仅媒体文件；非媒体/隐藏项被过滤
    # 上一级路径指向父目录
    assert data["parent"] == str(tmp_path.parent)

    # 进入子目录能看到其下媒体文件
    sub = client.get("/api/v1/fs/browse", params={"path": str(tmp_path / "sub")}).json()
    assert sub["files"][0]["name"] == "video.mp4"
    assert sub["parent"] == str(tmp_path.resolve())


def test_fs_pick_returns_selected_path(monkeypatch) -> None:
    # mock 原生对话框：避免真弹窗阻塞测试；验证接口装配与路径透传
    from video_to_summary.web import app as web_app

    monkeypatch.setattr(web_app, "_pick_file_native", lambda: {"path": "/fake/demo/video.mp4"})
    res = client.post("/api/v1/fs/pick")
    assert res.status_code == 200
    assert res.json() == {"path": "/fake/demo/video.mp4"}


def test_fs_pick_cancel_and_unavailable(monkeypatch) -> None:
    from video_to_summary.web import app as web_app

    # 用户取消 → 返回 path:null
    monkeypatch.setattr(web_app, "_pick_file_native", lambda: {"path": None})
    assert client.post("/api/v1/fs/pick").json() == {"path": None}
    # 无 GUI 会话 → 返回 error，前端据此回退面板
    monkeypatch.setattr(web_app, "_pick_file_native", lambda: {"error": "osascript unavailable (非 macOS 环境?)"})
    assert "error" in client.post("/api/v1/fs/pick").json()


def test_fs_pick_osascript_parses_path_and_cancel(monkeypatch) -> None:
    """直接测 _pick_file_native 的 osascript 分支：路径解析 / 取消 / 超时 / 缺失。"""
    import subprocess
    import sys

    from video_to_summary.web import app as web_app

    class FakeProc:
        def __init__(self, code, stdout="", stderr=""):
            self.returncode = code
            self.stdout = stdout
            self.stderr = stderr

    # 固定平台分发到 osascript（套件需可在任意平台跑）
    monkeypatch.setattr(sys, "platform", "darwin")
    # 选择成功 → 解析 POSIX 路径
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: FakeProc(0, "/Users/fan/Desktop/video.mp4\n"),
    )
    assert web_app._pick_file_native() == {"path": "/Users/fan/Desktop/video.mp4"}
    # 用户取消（退出码非 0）→ path:null
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeProc(1, ""))
    assert web_app._pick_file_native() == {"path": None}
    # 非 macOS（osascript 缺失）
    def _raise_fnf(*a, **k):
        raise FileNotFoundError()
    monkeypatch.setattr(subprocess, "run", _raise_fnf)
    assert "error" in web_app._pick_file_native()
    # 超时
    def _raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired("osascript", 600)
    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    assert "timed out" in web_app._pick_file_native()["error"]


def test_fs_pick_windows_powershell_parses_path_cancel_and_error(monkeypatch) -> None:
    """Windows 分支：-EncodedCommand 脚本构造 / 路径与 BOM / 取消 / 报错 / 超时 / 缺失。"""
    import base64
    import subprocess
    import sys

    from video_to_summary.web import app as web_app

    class FakeProc:
        def __init__(self, code, stdout="", stderr=""):
            self.returncode = code
            self.stdout = stdout
            self.stderr = stderr

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return FakeProc(0, "C:\\Users\\fan\\视频\\demo.mp4\r\n")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run", fake_run)
    result = web_app._pick_file_native()
    assert result == {"path": "C:\\Users\\fan\\视频\\demo.mp4"}  # strip + 中文路径不丢字

    # 命令形态：powershell -STA -EncodedCommand <b64>；脚本可解码回源文本
    cmd = captured["cmd"]
    assert cmd[0] == "powershell" and "-STA" in cmd and "-EncodedCommand" in cmd
    script = base64.b64decode(cmd[cmd.index("-EncodedCommand") + 1]).decode("utf-16-le")
    assert "System.Windows.Forms.OpenFileDialog" in script
    assert "[System.Windows.Forms.DialogResult]::OK" in script
    assert "OutputEncoding" in script  # 强制 UTF-8 输出
    assert "*.mp4" in script and "*.flac" in script  # 过滤器来自 _MEDIA_EXTS
    assert captured["kwargs"]["encoding"] == "utf-8"

    # 带前导 BOM 的 stdout 也要能解析
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeProc(0, "\ufeffC:\\a\\b.wav\n"))
    assert web_app._pick_file_native() == {"path": "C:\\a\\b.wav"}
    # 用户取消：退出码 0 且无输出 → path:null
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeProc(0, ""))
    assert web_app._pick_file_native() == {"path": None}
    # PowerShell 报错：退出码非 0 → 透传 stderr
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeProc(1, stderr="boom\n"))
    assert "boom" in web_app._pick_file_native()["error"]
    # powershell 缺失
    def _raise_fnf(*a, **k):
        raise FileNotFoundError()
    monkeypatch.setattr(subprocess, "run", _raise_fnf)
    assert "powershell" in web_app._pick_file_native()["error"]
    # 超时
    def _raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired("powershell", 600)
    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    assert "timed out" in web_app._pick_file_native()["error"]


def test_fs_pick_unsupported_platform_returns_error(monkeypatch) -> None:
    """Linux 等不支持平台：不弹框，直接返回 error（前端回退目录浏览面板）。"""
    import sys

    from video_to_summary.web import app as web_app

    monkeypatch.setattr(sys, "platform", "linux")
    result = web_app._pick_file_native()
    assert "error" in result
    assert "unsupported" in result["error"]


def test_logs_export_endpoint_redacts_and_attaches(fresh_ring) -> None:
    """/api/v1/logs/export：附件下载 + 植入的假 Key 不得出现在交付物中。

    fresh_ring 必须先清共享 buffer——全量跑时 e2e 用例（先于 test_web 收集）
    会填满 2000 条容量并轮转掉本用例的样本日志。
    """
    import logging as _logging

    from video_to_summary import log_export

    log_export.attach_ring_buffer()
    _logging.getLogger("test.web.export").error("boom with sk-abcdef12345678")

    res = client.get("/api/v1/logs/export")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/plain")
    assert "attachment" in res.headers["content-disposition"]
    assert 'filename="video-to-summary-logs-' in res.headers["content-disposition"]
    body = res.text
    assert "=== video-to-summary 诊断日志 ===" in body
    assert "sk-abcdef12345678" not in body  # 脱敏边界
    assert "sk-[REDACTED]" in body


def test_fs_browse_rejects_relative_and_missing(tmp_path) -> None:
    # 相对路径拒绝
    res = client.get("/api/v1/fs/browse", params={"path": "relative/path"})
    assert res.json()["error"] == "path must be absolute"
    # 不存在的目录
    res = client.get("/api/v1/fs/browse", params={"path": str(tmp_path / "nope")})
    assert res.json()["error"] == "not a directory"
    # 空 path 回落到主目录（无 error）
    res = client.get("/api/v1/fs/browse")
    assert res.json()["error"] is None
    assert res.json()["path"]


def test_list_jobs_returns_created_jobs() -> None:
    payload = {"source_type": "url", "url": "https://example.test/video", "title": "My Video"}
    created = client.post("/api/v1/jobs", json=payload).json()
    res = client.get("/api/v1/jobs")
    assert res.status_code == 200
    jobs = res.json()["jobs"]
    matched = [j for j in jobs if j["job_id"] == created["job_id"]]
    assert matched and matched[0]["title"] == "My Video"
    assert matched[0]["created_at"] > 0


def test_health_reports_llm_configured(tmp_path, monkeypatch) -> None:
    # DB 已由 autouse fixture 隔离到临时 SQLite；此处仅切换 cwd 模拟独立运行环境
    monkeypatch.chdir(tmp_path)
    res = client.get("/api/v1/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert isinstance(data["llm_configured"], bool)
    # 无迁移记录时 config_import 恒为空对象（前端据此跳过提示）
    assert data["config_import"] == {}
    # 版本号：发布构建 = git tag；测试环境（git 树）至少解析出非空值
    assert isinstance(data["version"], str) and data["version"]
    # 隔离库 schema 版本与 App 一致 → db_newer_version 为 None（无升级提示）
    assert data["db_newer_version"] is None


def test_health_reports_legacy_import() -> None:
    from video_to_summary import db as store_db

    store_db.set_setting(
        "legacy_imported_llm_profiles",
        json.dumps({"count": 2, "at": 1700000000.0}),
    )
    res = client.get("/api/v1/health")
    assert res.status_code == 200
    imported = res.json()["config_import"]
    assert imported["llm_profiles"]["count"] == 2
    assert imported["llm_profiles"]["at"] == 1700000000.0


def test_job_file_accepts_full_result_path(tmp_path, monkeypatch) -> None:
    from video_to_summary.web import tasks as web_tasks

    monkeypatch.chdir(tmp_path)
    job = web_tasks.create_job({"source_type": "url", "url": "https://example.test/video"})
    job.mark_completed({"summary": "x"})

    out_dir = Path("output") / job.job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "video123.summary.md").write_text("# ok", encoding="utf-8")

    # 前端实际行为：传完整 result_paths（output/<job_id>/xxx）
    res = client.get(f"/api/v1/jobs/{job.job_id}/file?path=output/{job.job_id}/video123.summary.md")
    assert res.status_code == 200
    assert res.json()["content"] == "# ok"

    # 纯文件名（相对 job 目录）同样可用
    res2 = client.get(f"/api/v1/jobs/{job.job_id}/file?path=video123.summary.md")
    assert res2.status_code == 200

    # 跨 job 读取仍被拒绝
    res3 = client.get(f"/api/v1/jobs/{job.job_id}/file?path=../other/video123.summary.md")
    assert res3.status_code == 400


def test_polish_preset_reaches_polisher() -> None:
    import types

    from video_to_summary.web import tasks as web_tasks

    settings = types.SimpleNamespace(polish_transcript=True, polish_preset="light")
    polisher = web_tasks._build_polisher(settings, {"api_key": "k", "model": "m", "base_url": None})
    assert polisher.prompt_preset == "light"


def test_run_job_completes_and_emits_events(tmp_path, monkeypatch) -> None:
    """覆盖 Web 任务全链路：Settings 构造（asr_model 等字段）、source.meta 访问、事件回调。"""
    from video_to_summary.web import tasks as web_tasks

    monkeypatch.chdir(tmp_path)

    def fake_run(source, transcriber, summarizer, output_dir, *, polisher=None, polisher_title="", on_event=None, cancel_check=None, subtitle_config=None):
        source.resolve()
        out = Path(output_dir) / f"{source.meta.source_id}.summary.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("# ok", encoding="utf-8")
        if on_event:
            on_event("download_start", {})
            on_event("download_done", {"title": "My Title", "duration": 60})
            on_event("transcribe_start", {})
            on_event("transcribe_done", {"cached": False})
        return out

    monkeypatch.setattr(web_tasks, "run", fake_run)

    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")
    job = web_tasks.create_job({"source_type": "local", "audio_path": str(audio), "title": "My Title"})
    web_tasks.run_job(job)

    assert job.status == "completed", job.error
    events = [e["event"] for e in job.progress.to_list()]
    assert "download_done" in events
    assert "transcribe_done" in events
    assert job.result_paths["summary"].endswith("a.summary.md")


def test_terminal_state_and_event_persist_atomically(tmp_path, monkeypatch) -> None:
    """回归：终态状态与终态事件必须**同一次落库**，不存在「状态终态但缺终态事件」的快照。

    修复前 run_job 先经 mark_completed 落库 status=completed（事件链只到 finalized），
    再单独 push COMPLETED 事件并第二次落库——两次落库之间轮询会读到「状态已终态、
    事件链缺 completed」。本用例捕获每次 Job.save 时的 (status, 事件名集合)，
    断言任何一次落库都不缺对应终态事件；修复前该用例失败、修复后通过。
    """
    from video_to_summary import constants, db
    from video_to_summary.web import tasks as web_tasks

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "app.db")
    db.reset()

    def fake_run(source, transcriber, summarizer, output_dir, *, polisher=None, polisher_title="", on_event=None, cancel_check=None, subtitle_config=None):
        source.resolve()
        out = Path(output_dir) / f"{source.meta.source_id}.summary.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("# ok", encoding="utf-8")
        if on_event:
            on_event("transcribe_done", {"cached": False})
        return out

    monkeypatch.setattr(web_tasks, "run", fake_run)

    snapshots: list[tuple[str, frozenset]] = []
    original_save = web_tasks.Job.save

    def spy_save(self):
        snapshots.append((self.status, frozenset(e["event"] for e in self.progress.to_list())))
        return original_save(self)

    monkeypatch.setattr(web_tasks.Job, "save", spy_save)

    def assert_no_terminal_snapshot_missing_event() -> None:
        required = {
            constants.JobStatus.COMPLETED: constants.JobEvent.COMPLETED,
            constants.JobStatus.FAILED: constants.JobEvent.ERROR,
            constants.JobStatus.CANCELLED: constants.JobEvent.CANCELLED,
        }
        for status, events in snapshots:
            if status in required:
                assert required[status] in events, (
                    f"落库快照 status={status} 缺终态事件 {required[status]!r}"
                    f"（事件={sorted(events)}）——终态与终态事件必须同一次落库"
                )

    # ① 完成路径（修复前：mark_completed 首次落库快照缺 completed）
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")
    job = web_tasks.create_job({"source_type": "local", "audio_path": str(audio), "title": "My Title"})
    web_tasks.run_job(job)
    assert job.status == "completed", job.error
    assert_no_terminal_snapshot_missing_event()

    # ② 失败路径（mark_failed 本就原子，顺带锁定契约）
    snapshots.clear()

    def failing_run(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(web_tasks, "run", failing_run)
    audio2 = tmp_path / "b.wav"
    audio2.write_bytes(b"x")
    job2 = web_tasks.create_job({"source_type": "local", "audio_path": str(audio2), "title": "b"})
    web_tasks.run_job(job2)
    assert job2.status == "failed"
    assert_no_terminal_snapshot_missing_event()

    # ③ 取消路径（pending 直接终止，mark_cancelled 原子）
    snapshots.clear()
    job3 = web_tasks.create_job({"source_type": "url", "url": "https://example.test/v"})
    web_tasks.cancel_job(job3.job_id)
    assert job3.status == "cancelled"
    assert_no_terminal_snapshot_missing_event()

    db.reset()


def test_completed_job_runs_plugin_side_effects(tmp_path, monkeypatch) -> None:
    """任务完成 → 广播 JobSideEffect（无注册者 = 空操作）；失败任务不广播。

    插件可在任务完成时执行自定义副作用（挂载点语义见 web/hooks.py）。
    """
    from video_to_summary.web import hooks
    from video_to_summary.web import tasks as web_tasks

    captured: list[dict] = []

    class _SpyEffect:
        def on_job_completed(self, job_id, metrics):
            captured.append({"job_id": job_id, **metrics})

    monkeypatch.setattr(hooks.HOOKS, "side_effects", [_SpyEffect()])

    def fake_run(source, transcriber, summarizer, output_dir, *, polisher=None, polisher_title="", on_event=None, cancel_check=None, subtitle_config=None):
        source.resolve()
        out = Path(output_dir) / f"{source.meta.source_id}.summary.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("# ok", encoding="utf-8")
        if on_event:
            on_event("transcribe_done", {"cached": False, "audio_seconds": 12.5})
            on_event("summarize_done", {"input_chars": 100, "output_chars": 40})
        return out

    monkeypatch.setattr(web_tasks, "run", fake_run)
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")
    created = client.post("/api/v1/jobs", json={"source_type": "local", "audio_path": str(audio), "title": "My Title"}).json()
    job = web_tasks.get_job(created["job_id"])
    web_tasks.run_job(job)
    assert job.status == "completed", job.error

    assert len(captured) == 1
    call = captured[0]
    assert call["job_id"] == created["job_id"]
    assert call["asr_audio_seconds"] == 12.5
    assert call["transcript_chars"] == 100
    assert call["summary_chars"] == 40

    # 失败任务不广播副作用
    captured.clear()

    def failing_run(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(web_tasks, "run", failing_run)
    audio2 = tmp_path / "b.wav"
    audio2.write_bytes(b"x")
    created2 = client.post("/api/v1/jobs", json={"source_type": "local", "audio_path": str(audio2), "title": "b"}).json()
    job2 = web_tasks.get_job(created2["job_id"])
    web_tasks.run_job(job2)
    assert job2.status == "failed"
    assert captured == []


def test_side_effect_failure_never_breaks_job_terminal_state(tmp_path, monkeypatch) -> None:
    """副作用抛异常绝不影响任务终态（hooks 层吞掉并记日志）。"""
    from video_to_summary.web import hooks
    from video_to_summary.web import tasks as web_tasks

    class _BrokenEffect:
        def on_job_completed(self, job_id, metrics):
            raise RuntimeError("审计服务不可达")

    monkeypatch.setattr(hooks.HOOKS, "side_effects", [_BrokenEffect()])

    def fake_run(source, transcriber, summarizer, output_dir, **kwargs):
        source.resolve()
        out = Path(output_dir) / f"{source.meta.source_id}.summary.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("# ok", encoding="utf-8")
        return out

    monkeypatch.setattr(web_tasks, "run", fake_run)
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")
    created = client.post("/api/v1/jobs", json={"source_type": "local", "audio_path": str(audio)}).json()
    job = web_tasks.get_job(created["job_id"])
    web_tasks.run_job(job)
    assert job.status == "completed"


def test_auth_required_when_token_configured(monkeypatch) -> None:
    monkeypatch.setenv("VIDEO_TO_SUMMARY_TOKEN", "test-secret-token")
    # 未携带 Token -> 401
    res = client.get("/api/v1/health")
    assert res.status_code == 401
    # 正确 Bearer Token -> 200
    res2 = client.get("/api/v1/health", headers={"Authorization": "Bearer test-secret-token"})
    assert res2.status_code == 200
    # X-Auth-Token 同样可用
    res3 = client.get("/api/v1/health", headers={"X-Auth-Token": "test-secret-token"})
    assert res3.status_code == 200
    # 页面与静态资源不要求鉴权
    assert client.get("/").status_code == 200


def test_cancel_pending_job() -> None:
    created = client.post("/api/v1/jobs", json={"source_type": "url", "url": "https://x"}).json()
    job_id = created["job_id"]
    res = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert res.status_code == 200
    detail = client.get(f"/api/v1/jobs/{job_id}").json()
    assert detail["status"] == "cancelled"
    assert "cancelled by user" in (detail["error"] or "")


def test_cancel_running_job_marks_cancelled(tmp_path, monkeypatch) -> None:
    from video_to_summary.web import tasks as web_tasks

    def fake_run(source, transcriber, summarizer, output_dir, *, polisher=None, polisher_title="", on_event=None, cancel_check=None, subtitle_config=None):
        if cancel_check:
            cancel_check()
        out = Path(output_dir) / f"{source.meta.source_id}.summary.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("# ok", encoding="utf-8")
        return out

    monkeypatch.setattr(web_tasks, "run", fake_run)

    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")
    job = web_tasks.create_job({"source_type": "local", "audio_path": str(audio), "title": "T"})
    job.mark_running()
    web_tasks._cancel_flags.add(job.job_id)
    try:
        web_tasks.run_job(job)
    finally:
        web_tasks._cancel_flags.discard(job.job_id)
    assert job.status == "cancelled", job.error
    assert "cancelled by user" in (job.error or "")


def test_cancel_running_job_pushes_cancel_requested() -> None:
    from video_to_summary.web import tasks as web_tasks

    job = web_tasks.create_job({"source_type": "url", "url": "https://x"})
    job.mark_running()
    assert web_tasks.cancel_job(job.job_id) is True
    assert job.job_id in web_tasks._cancel_flags
    events = [e["event"] for e in job.progress.to_list()]
    assert "cancel_requested" in events


def test_cancelled_pending_job_does_not_revive() -> None:
    """取消复活守卫：pending 在信号量排队期间被取消后，调度器手中旧引用不得再执行。"""
    from video_to_summary.web import tasks as web_tasks

    job = web_tasks.create_job({"source_type": "url", "url": "https://x"})
    assert web_tasks.cancel_job(job.job_id) is True
    cancelled_events = sum(1 for e in job.progress.to_list() if e["event"] == "cancelled")
    assert cancelled_events == 1  # 终态事件只推一次（mark_cancelled 统一负责）

    # 模拟已在排队的 _run_job_task 拿到信号量：旧引用状态已是 cancelled → 必须跳过
    web_tasks.run_job(job)
    assert job.status == "cancelled"
    assert job.result_paths is None
    assert [e["event"] for e in job.progress.to_list()].count("started") == 0


def test_retry_cancelled_job() -> None:
    created = client.post("/api/v1/jobs", json={"source_type": "url", "url": "https://x"}).json()
    job_id = created["job_id"]
    from video_to_summary.web import tasks as web_tasks

    job = web_tasks.get_job(job_id)
    job.mark_cancelled("stopped")
    res = client.post(f"/api/v1/jobs/{job_id}/retry")
    assert res.status_code == 200
    detail = client.get(f"/api/v1/jobs/{job_id}").json()
    assert detail["status"] == "pending"


def test_progress_label_for_cancelled_job() -> None:
    from video_to_summary.web import tasks as web_tasks

    job = web_tasks.create_job({"source_type": "url", "url": "https://x"})
    job.mark_cancelled("stopped")
    assert web_tasks._progress_label(job.status, json.dumps(job.progress.to_list(), ensure_ascii=False)) == "已停止"


def test_cancel_missing_job_returns_404() -> None:
    res = client.post("/api/v1/jobs/not-exist/cancel")
    assert res.status_code == 404


def test_cancel_terminal_job_returns_409() -> None:
    """终态任务（completed/failed/cancelled）调用 cancel 应返回 409，而非误报成功。"""
    from video_to_summary.web import tasks as web_tasks

    job = web_tasks.create_job({"source_type": "url", "url": "https://x"})
    job.mark_completed(result_paths={"transcript": "/fake/x.txt"})
    job.save()
    web_tasks._jobs[job.job_id] = job
    res = client.post(f"/api/v1/jobs/{job.job_id}/cancel")
    assert res.status_code == 409
    assert "terminal" in res.json()["detail"]


def test_job_payload_accepts_extra_config_fields() -> None:
    # 额外配置字段（对应 Settings 字段）由 from_mapping 消费，不应被请求模型拒绝；
    # summary_template 传历史名 "default" 走兼容别名，归一化为「通用」
    payload = {
        "source_type": "url",
        "url": "https://example.test/video",
        "audio_format": "wav",
        "whisper_api": True,
        "summary_template": "default",
    }
    res = client.post("/api/v1/jobs", json=payload)
    assert res.status_code == 200
    job_id = res.json()["job_id"]
    detail = client.get(f"/api/v1/jobs/{job_id}").json()
    assert detail["job_id"] == job_id


def test_template_save_get_and_delete_roundtrip() -> None:
    # 单段提示词模型：保存 / 读取 / 删除 / 覆盖内置名回退
    payload = {"name": "my-custom", "prompt": "用投资备忘录风格总结，重点写持仓变化"}
    res = client.put("/api/v1/templates/my-custom", json=payload)
    assert res.status_code == 200
    got = client.get("/api/v1/templates/my-custom").json()["template"]
    assert got == {"prompt": "用投资备忘录风格总结，重点写持仓变化"}
    # 缺少 prompt -> 400
    assert client.put("/api/v1/templates/bad", json={"hints": {}}).status_code == 400
    # 删除自定义 -> 从列表消失
    assert client.delete("/api/v1/templates/my-custom").json()["deleted"] is True
    assert "my-custom" not in client.get("/api/v1/templates").json()["templates"]
    # 删除后再次读取 -> 404
    assert client.get("/api/v1/templates/my-custom").status_code == 404
    # 内置模板只读：编辑/删除一律 400（历史别名 "default" 同样受保护），内容保持内置版
    assert client.put("/api/v1/templates/通用", json={"name": "通用", "prompt": "自定义覆盖"}).status_code == 400
    assert "不可编辑" in client.put("/api/v1/templates/通用", json={"name": "通用", "prompt": "x"}).json()["detail"]
    assert client.delete("/api/v1/templates/通用").status_code == 400
    assert client.put("/api/v1/templates/default", json={"name": "default", "prompt": "x"}).status_code == 400
    assert client.delete("/api/v1/templates/default").status_code == 400
    assert client.get("/api/v1/templates/通用").json()["template"]["prompt"].startswith("用通用笔记风格总结")


# LLM 配置（BYOK）走两槽位（/api/v1/llm）：
# 存取层语义由 test_llm_store.py 覆盖；此处覆盖 HTTP 层（掩码往返 / 部分更新 / 非法用途）。
# 本仓无任何授权/激活概念。


def test_llm_config_http_roundtrip_masks_key() -> None:
    """PUT 两槽位 → GET 掩码回读；掩码回传不覆盖真实 Key；部分更新不触碰另一槽位。"""
    from video_to_summary.web import llm_store

    res = client.put(
        "/api/v1/llm",
        json={
            "summary": {"base_url": "https://s.example/v1", "api_key": "sk-real-summary-123456", "model": "sum-model"},
            "asr": {"base_url": "https://a.example/v1", "api_key": "sk-real-asr-654321", "model": "asr-model"},
        },
    )
    assert res.status_code == 200
    cfg = res.json()
    assert cfg["summary"]["configured"] is True
    assert cfg["asr"]["configured"] is True
    assert "****" in cfg["summary"]["api_key"]
    assert "sk-real-summary-123456" not in res.text  # 明文绝不回传

    # 前端往返：掩码 Key 原样回传 → 既有真实 Key 保留；只带一个槽位 → 另一槽位不动
    res2 = client.put(
        "/api/v1/llm",
        json={"summary": {"base_url": "https://s2.example/v1", "api_key": cfg["summary"]["api_key"], "model": "sum-model-2"}},
    )
    assert res2.status_code == 200
    assert llm_store.get_slot("summary")["api_key"] == "sk-real-summary-123456"
    assert llm_store.get_slot("summary")["base_url"] == "https://s2.example/v1"
    assert llm_store.get_slot("asr")["model"] == "asr-model"


def test_llm_config_put_invalid_purpose_returns_400() -> None:
    res = client.put("/api/v1/llm", json={"nope": {"api_key": "k"}})
    assert res.status_code == 400
    assert "invalid purpose" in res.json()["detail"]


def test_llm_import_endpoint_reads_dotenv(monkeypatch, tmp_path) -> None:
    """POST /llm/import 读取 cwd 的 .env 填充两槽位；响应不含明文 Key。"""
    monkeypatch.chdir(tmp_path)
    # load_dotenv 会把 .env 键写进 os.environ（进程级、测试结束不会自动还原），
    # 用快照替换 environ 守护，避免污染同进程的后续用例
    environ_snapshot = dict(os.environ)
    monkeypatch.setattr(os, "environ", environ_snapshot)
    (tmp_path / ".env").write_text(
        "SUMMARY_API_KEY=sk-env-summary\nSUMMARY_BASE_URL=https://env.example/v1\nSUMMARY_MODEL=env-model\n",
        encoding="utf-8",
    )
    res = client.post("/api/v1/llm/import")
    assert res.status_code == 200
    cfg = res.json()
    assert cfg["summary"]["configured"] is True
    assert cfg["summary"]["model"] == "env-model"
    assert "sk-env-summary" not in res.text


def test_job_file_rejects_not_completed_and_bad_path() -> None:
    created = client.post("/api/v1/jobs", json={"source_type": "url", "url": "https://x"}).json()
    job_id = created["job_id"]
    # 未完成 -> 400
    assert client.get(f"/api/v1/jobs/{job_id}/file?path=a.md").status_code == 400
    # 不存在的任务 -> 404
    assert client.get("/api/v1/jobs/nope/file?path=a.md").status_code == 404


def test_job_result_requires_completed() -> None:
    created = client.post("/api/v1/jobs", json={"source_type": "url", "url": "https://x"}).json()
    res = client.get(f"/api/v1/jobs/{created['job_id']}/result")
    assert res.status_code == 400


def test_cancel_check_raises_for_cancelled_running_job() -> None:
    from video_to_summary.web import tasks as web_tasks

    job = web_tasks.create_job({"source_type": "url", "url": "https://x"})
    job.mark_running()
    check = web_tasks._make_cancel_check(job.job_id, __import__("time").monotonic(), 0)
    web_tasks._cancel_flags.add(job.job_id)
    try:
        with pytest.raises(web_tasks.JobCancelledError):
            check()
    finally:
        web_tasks._cancel_flags.discard(job.job_id)


def test_cancel_check_raises_on_timeout() -> None:
    import time

    from video_to_summary.web import tasks as web_tasks

    job = web_tasks.create_job({"source_type": "url", "url": "https://x"})
    job.mark_running()
    check = web_tasks._make_cancel_check(job.job_id, time.monotonic() - 100, 10)
    with pytest.raises(web_tasks.JobCancelledError) as exc_info:
        check()
    assert "timeout" in str(exc_info.value)


def test_retry_failed_job() -> None:
    from video_to_summary.web import tasks as web_tasks

    job = web_tasks.create_job({"source_type": "url", "url": "https://x", "title": "T"})
    job.mark_failed("boom")
    res = client.post(f"/api/v1/jobs/{job.job_id}/retry")
    assert res.status_code == 200
    data = res.json()
    assert data["job_id"] == job.job_id
    assert data["status"] == "pending"
    detail = client.get(f"/api/v1/jobs/{job.job_id}").json()
    assert detail["status"] == "pending"
    assert detail["error"] is None
    assert detail["progress"] == []


def test_retry_running_job_rejected() -> None:
    from video_to_summary.web import tasks as web_tasks

    job = web_tasks.create_job({"source_type": "url", "url": "https://x"})
    job.mark_running()
    res = client.post(f"/api/v1/jobs/{job.job_id}/retry")
    assert res.status_code == 400
    assert "not retryable" in res.json()["detail"]


def test_retry_missing_job_returns_404() -> None:
    res = client.post("/api/v1/jobs/not-exist/retry")
    assert res.status_code == 404


def test_list_jobs_returns_enriched_fields() -> None:
    from video_to_summary.web import tasks as web_tasks

    job = web_tasks.create_job(
        {"source_type": "url", "url": "https://www.bilibili.com/video/BV1x", "title": "我的视频"}
    )
    data = client.get("/api/v1/jobs").json()["jobs"]
    entry = next(j for j in data if j["job_id"] == job.job_id)
    assert entry["source"] == "bilibili"
    assert entry["title"] == "我的视频"
    assert entry["retry_count"] == 0
    assert entry["progress"] == "等待中"


def test_source_inference_for_local_and_youtube() -> None:
    from video_to_summary.web import tasks as web_tasks

    local = web_tasks.create_job({"source_type": "local", "audio_path": "/fake/a.wav"})
    assert local.source == "local"
    yt = web_tasks.create_job({"source_type": "url", "url": "https://youtu.be/abc123"})
    assert yt.source == "youtube"


def test_retry_records_count_and_time() -> None:
    from video_to_summary.web import tasks as web_tasks

    job = web_tasks.create_job({"source_type": "url", "url": "https://x"})
    job.mark_failed("boom")
    res = client.post(f"/api/v1/jobs/{job.job_id}/retry")
    assert res.status_code == 200
    detail = client.get(f"/api/v1/jobs/{job.job_id}").json()
    assert detail["retry_count"] == 1
    assert detail["retried_at"] is not None


def test_retry_uses_latest_global_config() -> None:
    """重试应使用当前全局默认配置，而非创建任务时的快照。"""
    from video_to_summary.web import tasks as web_tasks

    # 创建任务时全局 audio_format=wav（通过 API 创建才会合并全局默认）
    client.put("/api/v1/settings", json={"audio_format": "wav"})
    created = client.post("/api/v1/jobs", json={"source_type": "url", "url": "https://x", "title": "T"}).json()
    job = web_tasks.get_job(created["job_id"])
    assert job.payload["audio_format"] == "wav"
    job.mark_failed("boom")

    # 修改全局配置为 mp3
    client.put("/api/v1/settings", json={"audio_format": "mp3"})
    res = client.post(f"/api/v1/jobs/{job.job_id}/retry")
    assert res.status_code == 200

    # 重试后 payload 应反映最新配置
    refreshed = web_tasks.get_job(job.job_id)
    assert refreshed.payload["audio_format"] == "mp3"
    # 源信息保留
    assert refreshed.payload["url"] == "https://x"
    assert refreshed.payload["source_type"] == "url"
    assert refreshed.payload["title"] == "T"


def test_retry_clears_output_dir(tmp_path, monkeypatch) -> None:
    """重试应清空输出目录，确保 pipeline 从头执行不命中旧缓存。"""
    from video_to_summary.web import tasks as web_tasks

    monkeypatch.chdir(tmp_path)
    job = web_tasks.create_job({"source_type": "url", "url": "https://x"})
    job.mark_failed("boom")

    # 在输出目录放假文件模拟旧产物
    job_dir = tmp_path / "output" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    fake_file = job_dir / "old.txt"
    fake_file.write_text("stale", encoding="utf-8")
    assert fake_file.exists()

    res = client.post(f"/api/v1/jobs/{job.job_id}/retry")
    assert res.status_code == 200
    # 目录被清空，假文件不存在
    assert not fake_file.exists()


def test_download_done_updates_real_title(tmp_path, monkeypatch) -> None:
    from video_to_summary.web import tasks as web_tasks

    monkeypatch.chdir(tmp_path)

    def fake_run(source, transcriber, summarizer, output_dir, *, polisher=None, polisher_title="", on_event=None, cancel_check=None, subtitle_config=None):
        source.resolve()
        out = Path(output_dir) / f"{source.meta.source_id}.summary.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("# ok", encoding="utf-8")
        if on_event:
            on_event("download_start", {})
            on_event("download_done", {"title": "真实视频标题", "duration": 60})
        return out

    monkeypatch.setattr(web_tasks, "run", fake_run)
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")
    # URL 当标题（自动派生）→ 被真实视频标题回写
    job = web_tasks.create_job({"source_type": "local", "audio_path": str(audio), "title": "https://example.test/video"})
    web_tasks.run_job(job)
    assert job.title == "真实视频标题"
    # 已持久化到 DB
    assert web_tasks.get_job(job.job_id).title == "真实视频标题"


def test_real_title_writeback_preserves_user_title(tmp_path, monkeypatch) -> None:
    """用户显式输入的标题不被源信息回写覆盖；自动派生标题（URL/文件名）才会。"""
    from video_to_summary.web import tasks as web_tasks

    monkeypatch.chdir(tmp_path)

    def fake_run(source, transcriber, summarizer, output_dir, *, polisher=None, polisher_title="", on_event=None, cancel_check=None, subtitle_config=None):
        if on_event:
            on_event("download_done", {"title": "真实视频标题", "duration": 60})
        out = Path(output_dir) / "x.summary.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("# ok", encoding="utf-8")
        return out

    monkeypatch.setattr(web_tasks, "run", fake_run)
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")

    # 用户显式输入的标题 → 保留
    job = web_tasks.create_job({"source_type": "local", "audio_path": str(audio), "title": "我的自定义标题"})
    web_tasks.run_job(job)
    assert job.title == "我的自定义标题"

    # 源文件名自动派生的标题 → 回写
    job2 = web_tasks.create_job({"source_type": "local", "audio_path": str(audio)})
    web_tasks.run_job(job2)
    assert job2.title == "真实视频标题"


def test_subtitle_done_updates_real_title(tmp_path, monkeypatch) -> None:
    """字幕优先路径（无 download_done）同样回写真实视频标题。"""
    from video_to_summary.web import tasks as web_tasks

    monkeypatch.chdir(tmp_path)

    def fake_run(source, transcriber, summarizer, output_dir, *, polisher=None, polisher_title="", on_event=None, cancel_check=None, subtitle_config=None):
        if on_event:
            on_event("subtitle_start", {})
            on_event("subtitle_done", {"title": "字幕路径真实标题", "duration": 90})
        out = Path(output_dir) / "x.summary.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("# ok", encoding="utf-8")
        return out

    monkeypatch.setattr(web_tasks, "run", fake_run)
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")
    job = web_tasks.create_job({"source_type": "url", "url": "https://example.test/v", "title": "https://example.test/v"})
    web_tasks.run_job(job)
    assert job.title == "字幕路径真实标题"


def test_backfill_titles_from_outputs(tmp_path, monkeypatch) -> None:
    """启动回填：URL 标题的 completed 任务从产物 summary h1 修复；用户标题不动。"""
    from video_to_summary.web import tasks as web_tasks

    monkeypatch.chdir(tmp_path)
    url_job = web_tasks.create_job({"source_type": "url", "url": "https://example.test/a", "title": "https://example.test/a"})
    url_job.status = "completed"
    url_job.save()
    user_job = web_tasks.create_job({"source_type": "url", "url": "https://example.test/b", "title": "用户起的标题"})
    user_job.status = "completed"
    user_job.save()

    out = Path(web_tasks.output_base()) / url_job.job_id
    out.mkdir(parents=True)
    (out / "vid.summary.md").write_text("# 真实视频标题A\n\n正文", encoding="utf-8")
    out2 = Path(web_tasks.output_base()) / user_job.job_id
    out2.mkdir(parents=True)
    (out2 / "vid2.summary.md").write_text("# 真实视频标题B\n\n正文", encoding="utf-8")

    updated = web_tasks.backfill_titles_from_outputs()

    assert updated == 1
    assert web_tasks.get_job(url_job.job_id).title == "真实视频标题A"
    # 用户显式标题不被回填覆盖
    assert web_tasks.get_job(user_job.job_id).title == "用户起的标题"


def test_completed_run_backfills_title_from_summary_in_session(tmp_path, monkeypatch) -> None:
    """会话内标题闭环：事件未携带标题时（元数据缺失），run 成功后从产物 summary
    H1 兜底回填任务标题，无需等下次启动；用户显式标题不被覆盖。"""
    from video_to_summary.web import tasks as web_tasks

    monkeypatch.chdir(tmp_path)

    def fake_run(source, transcriber, summarizer, output_dir, *, polisher=None, polisher_title="", on_event=None, cancel_check=None, subtitle_config=None):
        # 不推任何带 title 的事件：模拟元数据缺失，标题只能来自产物 H1
        out = Path(output_dir) / "vid.summary.md"
        out.write_text("# 总结得出的标题\n\n正文", encoding="utf-8")
        return out

    monkeypatch.setattr(web_tasks, "run", fake_run)
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")

    # 自动派生标题（URL）→ 完成后被产物 H1 回写并持久化
    job = web_tasks.create_job({"source_type": "url", "url": "https://example.test/v", "title": "https://example.test/v"})
    web_tasks.run_job(job)
    assert job.status == "completed"
    assert job.title == "总结得出的标题"
    assert web_tasks.get_job(job.job_id).title == "总结得出的标题"

    # 用户显式标题 → 保留
    job2 = web_tasks.create_job({"source_type": "url", "url": "https://example.test/w", "title": "我的标题"})
    web_tasks.run_job(job2)
    assert job2.title == "我的标题"


def test_settings_get_put_roundtrip() -> None:
    data = client.get("/api/v1/settings").json()
    assert data["polish_transcript"] is False
    assert set(data) == {
        "summary_template", "audio_format", "polish_preset", "polish_transcript",
        "subtitle_preference", "subtitle_language", "cookies_browser", "proxy",
    }
    res = client.put("/api/v1/settings", json={"polish_transcript": True, "audio_format": "wav"})
    assert res.status_code == 200
    assert res.json()["polish_transcript"] is True
    assert res.json()["audio_format"] == "wav"
    assert client.put("/api/v1/settings", json={"polish_transcript": False}).json()["polish_transcript"] is False


def test_settings_cookies_browser_roundtrip_and_validation() -> None:
    """cookies_browser：大小写归一持久化、payload 透传、非法值 400 且不落库、空串关闭。"""
    from video_to_summary.web.settings_store import job_defaults_payload

    # 合法值（大小写/空白归一）→ 持久化并进入任务 payload
    res = client.put("/api/v1/settings", json={"cookies_browser": " Chrome "})
    assert res.status_code == 200
    assert res.json()["cookies_browser"] == "chrome"
    assert job_defaults_payload()["cookies_browser"] == "chrome"

    # 非法值 → 400，且原值不被覆盖
    res = client.put("/api/v1/settings", json={"cookies_browser": "netscape"})
    assert res.status_code == 400
    assert "netscape" in res.json()["detail"]
    assert client.get("/api/v1/settings").json()["cookies_browser"] == "chrome"

    # 空串 = 关闭
    assert client.put("/api/v1/settings", json={"cookies_browser": ""}).json()["cookies_browser"] == ""


def test_settings_proxy_roundtrip_and_validation() -> None:
    """proxy：持久化进任务 payload；非法 scheme 400 且不落库；空串 = 自动。"""
    from video_to_summary.web.settings_store import job_defaults_payload

    res = client.put("/api/v1/settings", json={"proxy": " http://127.0.0.1:7897 "})
    assert res.status_code == 200
    assert res.json()["proxy"] == "http://127.0.0.1:7897"
    assert job_defaults_payload()["proxy"] == "http://127.0.0.1:7897"

    res = client.put("/api/v1/settings", json={"proxy": "ftp://x"})
    assert res.status_code == 400
    assert client.get("/api/v1/settings").json()["proxy"] == "http://127.0.0.1:7897"

    assert client.put("/api/v1/settings", json={"proxy": "socks5://127.0.0.1:1080"}).json()["proxy"] == "socks5://127.0.0.1:1080"
    assert client.put("/api/v1/settings", json={"proxy": ""}).json()["proxy"] == ""


def test_cookies_test_endpoint_counts_without_values(monkeypatch) -> None:
    """预检端点：返回域名计数与登录态提示，绝不回传 cookie 值。"""
    class FakeCookie:
        def __init__(self, domain: str, name: str) -> None:
            self.domain = domain
            self.name = name

    class FakeJar:
        def __init__(self, cookies) -> None:
            self._cookies = cookies

        def __iter__(self):
            return iter(self._cookies)

    class FakeYDL:
        def __init__(self, opts):
            self.cookiejar = FakeJar(
                [
                    FakeCookie(".youtube.com", "VISITOR_INFO1_LIVE"),
                    FakeCookie(".youtube.com", "LOGIN_INFO"),
                    FakeCookie(".youtube.com", "SID"),
                    FakeCookie(".bilibili.com", "bili_jct"),
                    FakeCookie(".bilibili.com", "SESSDATA"),
                ]
            )

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    res = client.post("/api/v1/cookies/test", json={"browser": "chrome"})
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["total"] == 5
    assert data["youtube_login_hint"] is True
    assert data["bilibili_login_hint"] is True
    domains = dict(data["domains"])
    assert domains == {"youtube.com": 3, "bilibili.com": 2}
    # 响应体不含 cookie 值（只有序列化后的计数结构）
    assert "VISITOR_INFO1_LIVE" not in res.text and "bili_jct=" not in res.text
    assert "SESSDATA=" not in res.text


def test_cookies_test_endpoint_bilibili_not_logged_in(monkeypatch) -> None:
    """B 站域名有 cookie 但无 SESSDATA → bilibili_login_hint=False。"""
    class FakeCookie:
        def __init__(self, domain: str, name: str) -> None:
            self.domain = domain
            self.name = name

    class FakeJar:
        def __init__(self, cookies) -> None:
            self._cookies = cookies

        def __iter__(self):
            return iter(self._cookies)

    class FakeYDL:
        def __init__(self, opts):
            self.cookiejar = FakeJar([FakeCookie(".bilibili.com", "bili_jct")])

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    res = client.post("/api/v1/cookies/test", json={"browser": "chrome"})
    assert res.status_code == 200
    data = res.json()
    assert data["bilibili_login_hint"] is False


def test_cookies_test_endpoint_invalid_browser() -> None:
    res = client.post("/api/v1/cookies/test", json={"browser": "netscape"})
    assert res.status_code == 400
    assert "unsupported browser" in res.json()["detail"]


def test_cookies_test_endpoint_read_error_returns_ok_false(monkeypatch) -> None:
    """钥匙串拒绝/浏览器未装等读取失败 → ok=false + 可读错误，不 500。"""

    class BoomYDL:
        def __init__(self, opts):
            raise RuntimeError("keychain access denied")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("yt_dlp.YoutubeDL", BoomYDL)
    res = client.post("/api/v1/cookies/test", json={"browser": "chrome"})
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is False
    assert "keychain" in data["error"]


def test_create_job_applies_global_defaults() -> None:
    client.put("/api/v1/settings", json={"audio_format": "wav", "summary_template": "通用"})
    from video_to_summary.web import tasks as web_tasks

    created = client.post("/api/v1/jobs", json={"source_type": "url", "url": "https://x"}).json()
    job = web_tasks.get_job(created["job_id"])
    assert job.payload["audio_format"] == "wav"
    assert job.payload["whisper_api"] is True  # 开源版转写引擎唯一：Whisper API
    assert job.payload["summary_template"] == "通用"
    assert job.payload["polish_transcript"] is False  # 默认关闭优化
    assert job.payload["subtitle_preference"] == "auto"
    assert job.payload["subtitle_language"] == "auto"
    assert job.payload["source_type"] == "url"


def test_create_job_with_summary_template_overrides_global() -> None:
    """单任务总结模板：显式选择覆盖全局默认写入 payload，历史列表返回该字段。"""
    from video_to_summary.web import tasks as web_tasks

    client.put("/api/v1/settings", json={"summary_template": "通用"})
    created = client.post(
        "/api/v1/jobs", json={"source_type": "url", "url": "https://x", "summary_template": "精简笔记"}
    ).json()
    job = web_tasks.get_job(created["job_id"])
    assert job.payload["summary_template"] == "精简笔记"
    row = next(r for r in client.get("/api/v1/jobs").json()["jobs"] if r["job_id"] == created["job_id"])
    assert row["summary_template"] == "精简笔记"

    # 未显式选择 → 跟随全局默认
    other = client.post("/api/v1/jobs", json={"source_type": "url", "url": "https://y"}).json()
    assert web_tasks.get_job(other["job_id"]).payload["summary_template"] == "通用"


def test_create_job_normalizes_legacy_default_alias() -> None:
    """历史名 "default" 仍被接受（兼容别名），落库时归一化为「通用」。"""
    from video_to_summary.web import tasks as web_tasks

    created = client.post(
        "/api/v1/jobs", json={"source_type": "url", "url": "https://x", "summary_template": "default"}
    ).json()
    job = web_tasks.get_job(created["job_id"])
    assert job.payload["summary_template"] == "通用"


def test_create_job_with_unknown_template_returns_400() -> None:
    res = client.post(
        "/api/v1/jobs", json={"source_type": "url", "url": "https://x", "summary_template": "不存在模板"}
    )
    assert res.status_code == 400
    assert "unknown summary template" in res.json()["detail"]


def test_retry_preserves_summary_template() -> None:
    """重试保留单任务选择的总结模板（「重新生成」语义 = 同配置重跑），其余配置仍刷新。"""
    from video_to_summary.web import tasks as web_tasks

    client.put("/api/v1/settings", json={"audio_format": "wav", "summary_template": "通用"})
    created = client.post(
        "/api/v1/jobs", json={"source_type": "url", "url": "https://x", "summary_template": "会议纪要"}
    ).json()
    job = web_tasks.get_job(created["job_id"])
    assert job.payload["summary_template"] == "会议纪要"
    job.mark_failed("boom")

    client.put("/api/v1/settings", json={"audio_format": "mp3", "summary_template": "详细笔记"})
    res = client.post(f"/api/v1/jobs/{job.job_id}/retry")
    assert res.status_code == 200
    refreshed = web_tasks.get_job(job.job_id)
    assert refreshed.payload["summary_template"] == "会议纪要"  # 模板保留创建时的选择
    assert refreshed.payload["audio_format"] == "mp3"  # 其余配置取当前全局默认


def test_retry_with_new_template_replaces_template() -> None:
    """重试可携带新模板：显式传入 summary_template 时替换 payload 模板。"""
    from video_to_summary.web import tasks as web_tasks

    created = client.post(
        "/api/v1/jobs", json={"source_type": "url", "url": "https://x", "summary_template": "精简笔记"}
    ).json()
    job = web_tasks.get_job(created["job_id"])
    job.mark_failed("boom")

    res = client.post(
        f"/api/v1/jobs/{job.job_id}/retry", json={"summary_template": "详细笔记"}
    )
    assert res.status_code == 200
    refreshed = web_tasks.get_job(job.job_id)
    assert refreshed.payload["summary_template"] == "详细笔记"


def test_retry_with_unknown_template_returns_400() -> None:
    created = client.post("/api/v1/jobs", json={"source_type": "url", "url": "https://x"}).json()
    from video_to_summary.web import tasks as web_tasks

    job = web_tasks.get_job(created["job_id"])
    job.mark_failed("boom")
    res = client.post(
        f"/api/v1/jobs/{job.job_id}/retry", json={"summary_template": "不存在模板"}
    )
    assert res.status_code == 400
    assert "unknown summary template" in res.json()["detail"]


def test_list_jobs_returns_source_url_and_path() -> None:
    """历史任务列表应返回源链接/路径，供前端展示。"""
    from video_to_summary.web import tasks as web_tasks

    url_job = web_tasks.create_job({"source_type": "url", "url": "https://example.test/v1", "title": "U"})
    local_job = web_tasks.create_job({"source_type": "local", "audio_path": "/fake/foo.wav", "title": "L"})
    data = client.get("/api/v1/jobs").json()["jobs"]
    by_id = {j["job_id"]: j for j in data}
    assert by_id[url_job.job_id]["source_url"] == "https://example.test/v1"
    assert by_id[url_job.job_id]["source_type"] == "url"
    assert by_id[local_job.job_id]["source_path"] == "/fake/foo.wav"
    assert by_id[local_job.job_id]["source_type"] == "local"


def test_delete_completed_job_removes_db_and_files(tmp_path, monkeypatch) -> None:
    """删除终态任务：DB 记录与输出目录均被清理。"""
    from video_to_summary.web import tasks as web_tasks

    monkeypatch.chdir(tmp_path)
    job = web_tasks.create_job({"source_type": "url", "url": "https://x", "title": "T"})
    job.mark_completed({"summary": "x"})

    # 制造输出目录与产物文件
    out_dir = Path("output") / job.job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "x.summary.md").write_text("# ok", encoding="utf-8")
    assert out_dir.exists()

    res = client.delete(f"/api/v1/jobs/{job.job_id}")
    assert res.status_code == 200
    assert res.json()["deleted"] is True
    # DB 记录已删
    assert web_tasks.get_job(job.job_id) is None
    # 输出目录已清理
    assert not out_dir.exists()


def test_delete_running_job_rejected() -> None:
    """运行中/等待中任务不可删除，需先 cancel。"""
    from video_to_summary.web import tasks as web_tasks

    # pending
    created = client.post("/api/v1/jobs", json={"source_type": "url", "url": "https://x"}).json()
    res = client.delete(f"/api/v1/jobs/{created['job_id']}")
    assert res.status_code == 400
    assert "cancel" in res.json()["detail"]

    # running
    job = web_tasks.create_job({"source_type": "url", "url": "https://y"})
    job.mark_running()
    res2 = client.delete(f"/api/v1/jobs/{job.job_id}")
    assert res2.status_code == 400


def test_delete_missing_job_returns_404() -> None:
    assert client.delete("/api/v1/jobs/not-exist").status_code == 404


def test_delete_cancelled_job_succeeds(tmp_path, monkeypatch) -> None:
    """cancelled 终态可删除。"""
    from video_to_summary.web import tasks as web_tasks

    monkeypatch.chdir(tmp_path)
    job = web_tasks.create_job({"source_type": "url", "url": "https://x"})
    job.mark_cancelled("stopped")
    res = client.delete(f"/api/v1/jobs/{job.job_id}")
    assert res.status_code == 200
    assert web_tasks.get_job(job.job_id) is None


# ============ 历史任务标签（labels） ============

def test_create_job_with_labels_appears_in_list_and_detail() -> None:
    res = client.post(
        "/api/v1/jobs",
        json={"source_type": "url", "url": "https://example.test/v1", "title": "带标签", "labels": ["投资", "A股"]},
    )
    job_id = res.json()["job_id"]
    rows = client.get("/api/v1/jobs").json()["jobs"]
    row = next(j for j in rows if j["job_id"] == job_id)
    assert row["labels"] == ["A股", "投资"]
    detail = client.get(f"/api/v1/jobs/{job_id}").json()
    assert set(detail["labels"]) == {"投资", "A股"}


def test_create_job_with_invalid_labels_returns_400_without_orphan() -> None:
    # 创建时标签非法（保留名/超量/超长）→ 400 而非 500，且不落任何任务/标签行：
    # 校验先于 job.save()（否则 save 后失败会留下从未入队、列表可见的孤儿任务）
    before_jobs = client.get("/api/v1/jobs").json()["jobs"]
    before_labels = client.get("/api/v1/labels").json()
    res = client.post(
        "/api/v1/jobs",
        json={"source_type": "url", "url": "https://e/r", "title": "r", "labels": ["未分类"]},
    )
    assert res.status_code == 400
    assert "保留名" in res.json()["detail"]
    res = client.post(
        "/api/v1/jobs",
        json={"source_type": "url", "url": "https://e/r", "title": "r", "labels": [str(i) for i in range(9)]},
    )
    assert res.status_code == 400
    res = client.post(
        "/api/v1/jobs",
        json={"source_type": "url", "url": "https://e/r", "title": "r", "labels": ["x" * 25]},
    )
    assert res.status_code == 400
    # 混合（合法+非法）整体拒绝：合法标签也不得落库
    res = client.post(
        "/api/v1/jobs",
        json={"source_type": "url", "url": "https://e/r", "title": "r", "labels": ["合法", "未分类"]},
    )
    assert res.status_code == 400
    assert client.get("/api/v1/jobs").json()["jobs"] == before_jobs
    assert client.get("/api/v1/labels").json() == before_labels


def test_put_job_labels_replace_clear_and_auto_create() -> None:
    job_id = client.post("/api/v1/jobs", json={"source_type": "url", "url": "https://example.test/x", "title": "t"}).json()["job_id"]
    # 设置（自动建缺失标签）
    res = client.put(f"/api/v1/jobs/{job_id}/labels", json={"labels": ["新标签", "新标签"]})
    assert res.status_code == 200
    assert res.json()["labels"] == ["新标签"]  # 去重
    # 清空
    client.put(f"/api/v1/jobs/{job_id}/labels", json={"labels": []})
    assert client.get(f"/api/v1/jobs/{job_id}").json()["labels"] == []


def test_put_job_labels_validation_errors() -> None:
    job_id = client.post("/api/v1/jobs", json={"source_type": "url", "url": "https://example.test/x", "title": "t"}).json()["job_id"]
    # 保留名 / 超长 / 超量
    assert client.put(f"/api/v1/jobs/{job_id}/labels", json={"labels": ["未分类"]}).status_code == 400
    assert client.put(f"/api/v1/jobs/{job_id}/labels", json={"labels": ["x" * 25]}).status_code == 400
    assert client.put(f"/api/v1/jobs/{job_id}/labels", json={"labels": [str(i) for i in range(9)]}).status_code == 400
    # 不存在的任务
    assert client.put("/api/v1/jobs/nope/labels", json={"labels": ["a"]}).status_code == 404


def test_label_crud_rename_delete_merge() -> None:
    l1 = client.post("/api/v1/labels", json={"name": "旧名"}).json()
    l2 = client.post("/api/v1/labels", json={"name": "新名"}).json()
    # 重名 400
    assert client.post("/api/v1/labels", json={"name": "旧名"}).status_code == 400
    # 保留名 400
    assert client.post("/api/v1/labels", json={"name": "未分类"}).status_code == 400
    # 重命名
    assert client.put(f"/api/v1/labels/{l1['id']}", json={"name": "改"}).status_code == 200
    # 重命名撞名 400
    assert client.put(f"/api/v1/labels/{l1['id']}", json={"name": "新名"}).status_code == 400
    # 不存在的 id
    assert client.put("/api/v1/labels/99999", json={"name": "x"}).status_code == 404
    assert client.delete("/api/v1/labels/99999").status_code == 404
    # 合并
    assert client.post("/api/v1/labels/merge", json={"source_id": l1["id"], "target_id": l2["id"]}).status_code == 200
    assert client.post("/api/v1/labels/merge", json={"source_id": l2["id"], "target_id": l2["id"]}).status_code == 400
    data = client.get("/api/v1/labels").json()
    assert not any(l["name"] == "改" for l in data["labels"])


def test_label_counts_and_uncategorized() -> None:
    client.post("/api/v1/jobs", json={"source_type": "url", "url": "https://e/x", "title": "a", "labels": ["A"]})
    client.post("/api/v1/jobs", json={"source_type": "url", "url": "https://e/y", "title": "b", "labels": ["A"]})
    client.post("/api/v1/jobs", json={"source_type": "url", "url": "https://e/z", "title": "c"})
    data = client.get("/api/v1/labels").json()
    assert data["labels"] == [{"id": data["labels"][0]["id"], "name": "A", "count": 2}]
    assert data["uncategorized_count"] == 1


def test_jobs_filter_by_label_and_unlabeled() -> None:
    client.post("/api/v1/jobs", json={"source_type": "url", "url": "https://e/a", "title": "a", "labels": ["系列"]})
    client.post("/api/v1/jobs", json={"source_type": "url", "url": "https://e/b", "title": "b"})
    filtered = client.get("/api/v1/jobs", params={"label": "系列"}).json()["jobs"]
    assert len(filtered) == 1 and filtered[0]["title"] == "a"
    unlabeled = client.get("/api/v1/jobs", params={"unlabeled": 1}).json()["jobs"]
    assert len(unlabeled) == 1 and unlabeled[0]["title"] == "b"


def test_retry_preserves_labels() -> None:
    """关键回归：重试用全局默认重建 payload，标签存关联表必须保留。"""
    job_id = client.post(
        "/api/v1/jobs", json={"source_type": "url", "url": "https://e/r", "title": "r", "labels": ["保留"]}
    ).json()["job_id"]
    # 置为终态后可重试
    import sqlite3 as _sqlite3
    from video_to_summary import db as store_db
    from pathlib import Path as _Path
    with store_db.get_conn() as conn:
        conn.execute("UPDATE jobs SET status='failed' WHERE job_id=?", (job_id,))
    client.post(f"/api/v1/jobs/{job_id}/retry")
    assert client.get(f"/api/v1/jobs/{job_id}").json()["labels"] == ["保留"]


def test_delete_label_unbinds_jobs() -> None:
    job_id = client.post("/api/v1/jobs", json={"source_type": "url", "url": "https://e/d", "title": "d", "labels": ["临"]}).json()["job_id"]
    label_id = next(l["id"] for l in client.get("/api/v1/labels").json()["labels"] if l["name"] == "临")
    assert client.delete(f"/api/v1/labels/{label_id}").status_code == 200
    assert client.get(f"/api/v1/jobs/{job_id}").json()["labels"] == []


def test_auth_token_non_ascii_header_returns_401(monkeypatch) -> None:
    """非 ASCII 鉴权头不得触发 500：bytes 比较后按 401 拒绝。"""
    from video_to_summary.web.app import _auth_header_ok

    # 直测比较函数（TestClient/httpx 无法发送非 ASCII 头，客户端侧就拒绝）
    assert _auth_header_ok("Bearer héllo-世界", "", "tok-123") is False
    assert _auth_header_ok("", "早餐", "tok-123") is False
    assert _auth_header_ok("Bearer tok-123", "", "tok-123") is True

    # 走完整中间件：ASCII 错误 token → 401；正确 token → 通过
    monkeypatch.setenv("VIDEO_TO_SUMMARY_TOKEN", "tok-123")
    try:
        res = client.get("/api/v1/health", headers={"Authorization": "Bearer wrong"})
        assert res.status_code == 401
        res = client.get("/api/v1/health", headers={"Authorization": "Bearer tok-123"})
        assert res.status_code == 200
    finally:
        monkeypatch.delenv("VIDEO_TO_SUMMARY_TOKEN", raising=False)


def test_run_job_bridges_download_progress_events(tmp_path, monkeypatch) -> None:
    """URL 源的 on_progress 属性被 Web 层桥接为 download_progress 事件链节点。"""
    import types
    from pathlib import Path as _Path

    from video_to_summary.web import tasks as web_tasks

    # 与同文件 run_job 用例一致：隔离 output_base（CWD 相对 output/）避免污染仓库根
    monkeypatch.chdir(tmp_path)

    class _ProgressSource:
        def __init__(self) -> None:
            self.meta = None
            self.on_progress = None

        def resolve(self):
            # 模拟 yt-dlp progress_hook 经源侧节流后的回调
            self.on_progress({"percent": 42.0, "downloaded_bytes": 420, "total_bytes": 1000, "speed": 100, "eta": 6})
            meta = types.SimpleNamespace(source_id="a", title="T")
            self.meta = meta
            return _Path("a.wav"), meta

    src = _ProgressSource()
    monkeypatch.setattr(web_tasks, "_build_source", lambda settings, job: src)

    def fake_run(source, transcriber, summarizer, output_dir, *, polisher=None, polisher_title="", on_event=None, cancel_check=None, subtitle_config=None):
        source.resolve()
        out = _Path(output_dir) / "a.summary.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("# ok", encoding="utf-8")
        if on_event:
            on_event("download_start", {})
            on_event("download_done", {"title": "T", "duration": 60})
        return out

    monkeypatch.setattr(web_tasks, "run", fake_run)

    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")
    job = web_tasks.create_job({"source_type": "url", "url": "https://example.com/v", "title": "T"})
    web_tasks.run_job(job)

    assert job.status == "completed", job.error
    progress_events = [e for e in job.progress.to_list() if e["event"] == "download_progress"]
    assert len(progress_events) == 1
    assert progress_events[0]["payload"]["percent"] == 42.0


# ---------------------------------------------------------------- 历史分页

def test_list_jobs_pagination_stable_no_dup_no_gap() -> None:
    """limit/offset 翻页：同秒创建（job_id 定序兜底）也不重不漏，total 与条件一致。"""
    from video_to_summary.web import tasks as web_tasks

    for i in range(5):
        web_tasks.create_job({"source_type": "local", "audio_path": f"/fake/{i}.mp3", "title": f"job{i}"})

    total = web_tasks.count_jobs()
    assert total == 5
    pages = [
        web_tasks.list_jobs(limit=2, offset=0),
        web_tasks.list_jobs(limit=2, offset=2),
        web_tasks.list_jobs(limit=2, offset=4),
    ]
    ids = [e["job_id"] for page in pages for e in page]
    assert len(ids) == 5
    assert len(set(ids)) == 5  # 不重不漏
    # 末页空页语义正常
    assert web_tasks.list_jobs(limit=2, offset=5) == []


def test_list_jobs_pagination_respects_label_filter_total() -> None:
    from video_to_summary.web import tasks as web_tasks
    from video_to_summary.web.label_store import set_job_labels

    job_a = web_tasks.create_job({"source_type": "local", "audio_path": "/fake/a.mp3", "title": "A"})
    job_b = web_tasks.create_job({"source_type": "local", "audio_path": "/fake/b.mp3", "title": "B"})
    set_job_labels(job_a.job_id, ["分页标签"])

    assert web_tasks.count_jobs(label="分页标签") == 1
    assert web_tasks.count_jobs(unlabeled=True) == 1
    filtered = web_tasks.list_jobs(limit=50, label="分页标签")
    assert [e["job_id"] for e in filtered] == [job_a.job_id]
    # offset 越界返回空
    assert web_tasks.list_jobs(limit=50, label="分页标签", offset=1) == []
    # 标签筛选互斥语义：未打标签的 job_b 不出现在筛选结果中
    assert all(e["job_id"] != job_b.job_id for e in filtered)


def test_list_jobs_api_pagination_params(web_client) -> None:
    """/api/v1/jobs 响应带 total；limit 越界/非法被 422 拒绝。"""
    from video_to_summary.web import tasks as web_tasks

    for i in range(3):
        web_tasks.create_job({"source_type": "local", "audio_path": f"/fake/{i}.mp3", "title": f"j{i}"})

    res = web_client.get("/api/v1/jobs?limit=2&offset=0")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 3
    assert len(data["jobs"]) == 2
    # 无参数调用兼容（默认 limit=50）
    res_default = web_client.get("/api/v1/jobs")
    assert res_default.status_code == 200
    assert res_default.json()["total"] == 3
    # 参数校验
    assert web_client.get("/api/v1/jobs?limit=0").status_code == 422
    assert web_client.get("/api/v1/jobs?limit=999").status_code == 422
    assert web_client.get("/api/v1/jobs?offset=-1").status_code == 422


# ---------------------------------------------------------------- 全文检索（?q=）

def test_list_jobs_api_fulltext_search_q(web_client, tmp_path) -> None:
    """?q= 命中转写正文：返回 match_snippet（<mark>）与 match_kinds；标题命中兜底。"""
    from video_to_summary.web import tasks as web_tasks

    out = tmp_path / "output" / "j1"
    out.mkdir(parents=True)
    (out / "j1.txt").write_text("会议里讨论了跨模态检索方案", encoding="utf-8")
    (out / "j1.summary.md").write_text("# 标题\n跨模态检索要点", encoding="utf-8")
    job = web_tasks.create_job({"source_type": "local", "audio_path": "/fake/a.mp3", "title": "周会记录"})
    job.mark_completed({
        "summary": str(out / "j1.summary.md"),
        "transcript": str(out / "j1.txt"),
    })

    # 正文关键词命中（trigram MATCH 或 LIKE 降级，两条路径都应命中）
    res = web_client.get("/api/v1/jobs?q=跨模态检索")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 1
    item = data["jobs"][0]
    assert item["job_id"] == job.job_id
    assert "<mark>" in item["match_snippet"]
    assert set(item["match_kinds"]) <= {"transcript", "summary"}

    # 标题命中（元数据层兜底）
    res_title = web_client.get("/api/v1/jobs?q=周会记录")
    assert res_title.json()["total"] == 1

    # 无命中 → 空结果 + total 0
    res_none = web_client.get("/api/v1/jobs?q=不存在的词")
    assert res_none.json() == {"jobs": [], "total": 0}

    # 无 q 参数时不带检索字段（响应形状向后兼容）
    res_plain = web_client.get("/api/v1/jobs")
    assert "match_snippet" not in res_plain.json()["jobs"][0]


def test_list_jobs_api_fulltext_search_with_label_filter(web_client, tmp_path) -> None:
    """q 与标签筛选叠加：命中集再按标签收窄。"""
    from video_to_summary.web.label_store import set_job_labels
    from video_to_summary.web import tasks as web_tasks

    out = tmp_path / "output" / "j1"
    out.mkdir(parents=True)
    (out / "j1.txt").write_text("视频讲了知识图谱", encoding="utf-8")
    job_a = web_tasks.create_job({"source_type": "local", "audio_path": "/fake/a.mp3", "title": "A"})
    job_b = web_tasks.create_job({"source_type": "local", "audio_path": "/fake/b.mp3", "title": "B"})
    for job, out_dir in ((job_a, out), (job_b, out)):
        job.mark_completed({"transcript": str(out / "j1.txt")})
    set_job_labels(job_a.job_id, ["检索标签"])

    res = web_client.get("/api/v1/jobs?q=知识图谱")
    assert res.json()["total"] == 2
    res_filtered = web_client.get("/api/v1/jobs?q=知识图谱&label=检索标签")
    data = res_filtered.json()
    assert data["total"] == 1
    assert data["jobs"][0]["job_id"] == job_a.job_id


# ---------------------------------------------------------------- 历史数据导出/导入 API

def test_history_export_import_api_roundtrip(web_client, tmp_path, monkeypatch) -> None:
    """导出端点返回合法 zip；导入端点还原任务并可检索；重复导入全跳过。"""
    import io as _io
    import zipfile as _zipfile

    from video_to_summary import db as store_db
    from video_to_summary.web import tasks as web_tasks
    from video_to_summary.web import app as web_app

    out_dir = tmp_path / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(web_tasks, "output_base", lambda: str(out_dir))
    monkeypatch.setattr(web_app, "output_base", lambda: str(out_dir))

    job = web_tasks.create_job({"source_type": "local", "audio_path": "/fake/a.mp3", "title": "导出任务"})
    summary_path = out_dir / job.job_id / "x.summary.md"
    transcript_path = out_dir / job.job_id / "x.txt"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text("# 导出任务\n总结", encoding="utf-8")
    transcript_path.write_text("转写内容", encoding="utf-8")
    job.mark_completed({"summary": str(summary_path), "transcript": str(transcript_path)})

    # 导出
    res = web_client.get("/api/v1/jobs/export")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/zip")
    data = res.content
    with _zipfile.ZipFile(_io.BytesIO(data)) as zf:
        assert "manifest.json" in zf.namelist()
        assert "jobs.json" in zf.namelist()

    # 模拟新机器：清空任务侧表后导入
    store_db.init_db()
    with store_db.get_conn() as conn:
        for table in ("job_labels", "labels", "summary_templates", "jobs"):
            conn.execute(f"DELETE FROM {table}")

    res_import = web_client.post(
        "/api/v1/jobs/import",
        content=data,
        headers={"Content-Type": "application/zip"},
    )
    assert res_import.status_code == 200, res_import.text
    counters = res_import.json()
    assert counters["imported"] == 1
    assert counters["artifacts_restored"] == 2

    # 导入的任务可见且可检索（FTS 索引被复用入口重建）
    res_list = web_client.get("/api/v1/jobs?q=导出任务")
    assert res_list.json()["total"] == 1

    # 重复导入 → 全部跳过
    res_again = web_client.post(
        "/api/v1/jobs/import",
        content=data,
        headers={"Content-Type": "application/zip"},
    )
    assert res_again.json()["skipped_existing"] == 1


def test_history_import_api_rejects_bad_bundle(web_client, monkeypatch) -> None:
    """坏包 → 400 带用户可读文案；空 body → 400；超限 → 413。"""
    res_bad = web_client.post("/api/v1/jobs/import", content=b"not a zip", headers={"Content-Type": "application/zip"})
    assert res_bad.status_code == 400
    assert "zip" in res_bad.json()["detail"]

    res_empty = web_client.post("/api/v1/jobs/import", content=b"")
    assert res_empty.status_code == 400

    monkeypatch.setenv("VTS_IMPORT_MAX_MB", "0")
    res_large = web_client.post("/api/v1/jobs/import", content=b"x", headers={"Content-Type": "application/zip"})
    assert res_large.status_code == 413


# ---------------- 产物导出（原「下载」）与总结编辑 ----------------

def _completed_job_with_products(tmp_path, monkeypatch, *, markdown=True):
    """构造 completed 任务 + 真实产物文件（summary/transcript），返回 (web_tasks, job)。"""
    from video_to_summary.web import tasks as web_tasks

    monkeypatch.chdir(tmp_path)
    job = web_tasks.create_job({"source_type": "url", "url": "https://example.test/video", "title": "测试视频任务"})
    summary = "# 总结标题\n\n**要点**与 <span style=\"color:#d1544a\">警示</span>。"
    if not markdown:
        summary = "纯文本总结内容"
    transcript = "这是转写原文第一段。\n第二段内容。"
    out_dir = Path("output") / job.job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "video123.summary.md").write_text(summary, encoding="utf-8")
    (out_dir / "video123.txt").write_text(transcript, encoding="utf-8")
    job.mark_completed({
        "summary": str((out_dir / "video123.summary.md").resolve()),
        "transcript": str((out_dir / "video123.txt").resolve()),
    })
    return web_tasks, job


def test_export_summary_md_attachment(tmp_path, monkeypatch) -> None:
    _, job = _completed_job_with_products(tmp_path, monkeypatch)
    res = client.get(f"/api/v1/jobs/{job.job_id}/export?path=video123.summary.md&format=md")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/markdown")
    disposition = res.headers["content-disposition"]
    assert "attachment" in disposition
    # CJK 文件名走 RFC 5987 filename*；ASCII 兜底名防不支持场景
    assert "filename*=UTF-8''" in disposition
    assert res.content.decode("utf-8").startswith("# 总结标题")


def test_export_rejects_escape_bad_format_not_completed(tmp_path, monkeypatch) -> None:
    _, job = _completed_job_with_products(tmp_path, monkeypatch)
    # 路径逃逸
    assert client.get(f"/api/v1/jobs/{job.job_id}/export?path=../other/x.md").status_code == 400
    # 未知格式
    assert client.get(f"/api/v1/jobs/{job.job_id}/export?path=video123.summary.md&format=docx").status_code == 400
    # 文件不存在
    assert client.get(f"/api/v1/jobs/{job.job_id}/export?path=nope.md&format=md").status_code == 404
    # 未完成任务
    pending = client.post("/api/v1/jobs", json={"source_type": "url", "url": "https://x"}).json()
    assert client.get(f"/api/v1/jobs/{pending['job_id']}/export?path=a.md&format=md").status_code == 400
    # 任务不存在
    assert client.get("/api/v1/jobs/nope/export?path=a.md").status_code == 404


def test_summary_edit_updates_file_and_marker(tmp_path, monkeypatch) -> None:
    web_tasks, job = _completed_job_with_products(tmp_path, monkeypatch)
    res = client.put(
        f"/api/v1/jobs/{job.job_id}/summary",
        json={"content": "# 编辑后的总结\n\n独一无二的编辑标记词串。"},
    )
    assert res.status_code == 200
    edited_at = res.json()["summary_edited_at"]
    assert isinstance(edited_at, float) and edited_at > 0

    # 文件被覆盖写回
    summary_path = Path(job.result_paths["summary"])
    assert summary_path.read_text(encoding="utf-8").startswith("# 编辑后的总结")

    # 详情返回编辑标记
    detail = client.get(f"/api/v1/jobs/{job.job_id}").json()
    assert detail["summary_edited_at"] == edited_at

    # 编辑稿入全文检索（fts5 缺失的降级环境跳过该断言）
    from video_to_summary.web import fts_index

    if fts_index.fts_available():
        hits = client.get("/api/v1/jobs", params={"q": "独一无二的编辑标记词串"}).json()
        assert job.job_id in {j["job_id"] for j in hits["jobs"]}


def test_summary_edit_validation(tmp_path, monkeypatch) -> None:
    _, job = _completed_job_with_products(tmp_path, monkeypatch)
    # 空内容
    assert client.put(f"/api/v1/jobs/{job.job_id}/summary", json={"content": "   "}).status_code == 400
    # 非 JSON
    assert client.put(f"/api/v1/jobs/{job.job_id}/summary", content=b"xxx").status_code in (400, 422)
    # 任务不存在
    assert client.put("/api/v1/jobs/nope/summary", json={"content": "x"}).status_code == 404
    # 未完成（无 summary 产物）
    pending = client.post("/api/v1/jobs", json={"source_type": "url", "url": "https://x"}).json()
    assert client.put(f"/api/v1/jobs/{pending['job_id']}/summary", json={"content": "x"}).status_code == 400


def test_retry_clears_summary_edited_at(tmp_path, monkeypatch) -> None:
    web_tasks, job = _completed_job_with_products(tmp_path, monkeypatch)
    client.put(f"/api/v1/jobs/{job.job_id}/summary", json={"content": "# 编辑稿"})
    assert web_tasks.get_job(job.job_id).summary_edited_at is not None

    # 重试（completed → 重新生成）：输出目录清空重建，编辑标记同步清零
    monkeypatch.setattr(web_tasks, "enqueue_job", lambda j: None)
    retried = web_tasks.retry_job(job.job_id)
    assert retried.summary_edited_at is None
    assert web_tasks.get_job(job.job_id).summary_edited_at is None


def test_export_filenames_use_format_extension(tmp_path, monkeypatch) -> None:
    """导出文件名 = `<标题>.<格式扩展名>`（保留产物自身后缀，不带源文件名 stem）。"""
    import urllib.parse as _up

    _, job = _completed_job_with_products(tmp_path, monkeypatch)

    res_md = client.get(f"/api/v1/jobs/{job.job_id}/export?path=video123.summary.md&format=md")
    dispo_md = res_md.headers["content-disposition"]
    name_md = _up.unquote(dispo_md.split("filename*=UTF-8''")[1].split(";")[0])
    assert name_md == f"{job.title}.md"

    # 转写纯文本导出 → .txt
    res_txt = client.get(f"/api/v1/jobs/{job.job_id}/export?path=video123.txt&format=md")
    dispo_txt = res_txt.headers["content-disposition"]
    name_txt = _up.unquote(dispo_txt.split("filename*=UTF-8''")[1].split(";")[0])
    assert name_txt == f"{job.title}.txt"


def test_export_HEAD_returns_filename_header(tmp_path, monkeypatch) -> None:
    """HEAD /export：返回 Content-Disposition 文件名头（前端导出后 toast 用），
    且剥掉 body——FastAPI 默认只注册 GET，曾致 HEAD 405。"""
    _, job = _completed_job_with_products(tmp_path, monkeypatch)
    res = client.request("HEAD", f"/api/v1/jobs/{job.job_id}/export?path=video123.summary.md&format=md")
    assert res.status_code == 200
    dispo = res.headers["content-disposition"]
    assert "attachment" in dispo
    assert "filename*=UTF-8''" in dispo
    assert res.content == b""


def test_export_pdf_format_is_rejected_with_actionable_detail(tmp_path, monkeypatch) -> None:
    """本版不提供 PDF 导出：请求 format=pdf 明确 400 + 可行动文案（改用 format=md）。"""
    _, job = _completed_job_with_products(tmp_path, monkeypatch)
    url = f"/api/v1/jobs/{job.job_id}/export?path=video123.summary.md&format=pdf"
    res = client.get(url)
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "本版不提供 PDF 导出" in detail
    assert "format=md" in detail
    assert "pdf_export" not in detail  # 不再携带能力位提示
    # HEAD 同语义（无 body，只看状态码）
    assert client.request("HEAD", url).status_code == 400


# ---------------------------------------------------------------- 站点风控错误（412/429）对外文案


def test_user_facing_error_412_guides_actionable_solutions() -> None:
    """B 站 412 风控：错误文案引导自定义 UA / cookies / 代理，且不暴露上游内部报文。"""
    from video_to_summary.web.tasks import _user_facing_error

    raw = (
        "ERROR: [bilibili] BV1xx: HTTP Error 412: Precondition Failed "
        "(caused by <class 'urllib.error.HTTPError'>)"
    )
    msg = _user_facing_error(RuntimeError(raw))
    assert "HTTP 412" in msg or "412" in msg
    assert "VTS_USER_AGENT=Wget/1.21.3" in msg
    assert "VTS_COOKIES_FILE" in msg
    assert "代理" in msg
    assert raw not in msg  # 不把上游内部报文原样暴露给普通用户


def test_user_facing_error_429_guides_actionable_solutions() -> None:
    """429（YouTube 自动字幕匿名限流等）：同样给出 UA / cookies / 代理解法。"""
    from video_to_summary.web.tasks import _user_facing_error

    msg = _user_facing_error(RuntimeError("ERROR: [youtube] abc: HTTP Error 429: Too Many Requests"))
    assert "VTS_USER_AGENT" in msg
    assert "VTS_COOKIES_FILE" in msg
    assert "代理" in msg


def test_user_facing_error_too_many_requests_without_code() -> None:
    """无状态码的 Too Many Requests 同样命中限流引导（yt-dlp 偶发改写报文）。"""
    from video_to_summary.web.tasks import _user_facing_error

    msg = _user_facing_error(RuntimeError("Too Many Requests"))
    assert "VTS_USER_AGENT" in msg


def test_user_facing_error_other_errors_unchanged() -> None:
    """非 412/429 错误原样保留（不套用风控文案，避免误引导）。"""
    from video_to_summary.web.tasks import _user_facing_error

    assert (
        _user_facing_error(RuntimeError("HTTP Error 503: Service Unavailable"))
        == "HTTP Error 503: Service Unavailable"
    )
    assert _user_facing_error(RuntimeError("boom")) == "boom"


def test_build_source_wires_user_agent(monkeypatch) -> None:
    """Web 任务构造源时把 Settings.user_agent 传给 URLAudioSource（VTS_USER_AGENT 通道生效）。"""
    from video_to_summary.config import Settings
    from video_to_summary.web import tasks as web_tasks

    captured: dict = {}

    class FakeURLSource:
        def __init__(self, url, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(web_tasks, "URLAudioSource", FakeURLSource)
    settings = Settings(url="https://x", user_agent="Wget/1.21.3")
    job = web_tasks.create_job({"source_type": "url", "url": "https://x"})
    web_tasks._build_source(settings, job)
    assert captured["user_agent"] == "Wget/1.21.3"
    assert captured["cookies"] is None


def test_job_timeout_marks_failed(monkeypatch, tmp_path) -> None:
    """超时端到端：transcribe 阶段超过 VIDEO_TO_SUMMARY_JOB_TIMEOUT → 任务 failed 且 error 含 timeout。"""
    import time as _time

    from video_to_summary.schemas import TranscriptResult
    from video_to_summary.web import tasks as web_tasks

    job = web_tasks.create_job({"source_type": "url", "url": "https://x"})
    job.mark_running()

    class FakeSource:
        def resolve(self):
            from pathlib import Path as _Path

            from video_to_summary.schemas import AudioMeta

            return _Path("a.wav"), AudioMeta(
                source_id="sid", title="T", source_url="https://x",
                duration=1.0, uploader="", upload_date="", audio_path="a.wav",
            )

    class FakeTranscriber:
        """不主动取消（旧式实现），依赖 pipeline 阶段边界的超时检查中止。"""

        def transcribe(self, audio_path, cancel_check=None):
            deadline = _time.monotonic() + 30
            while _time.monotonic() < deadline:
                if cancel_check is not None:
                    cancel_check()  # 超时后由此抛 JobCancelledError
                _time.sleep(0.05)
            return TranscriptResult(text="x", segments=[])

    monkeypatch.setattr(web_tasks, "_build_source", lambda settings, j: FakeSource())
    monkeypatch.setattr(web_tasks, "_build_transcriber", lambda settings, cfg: FakeTranscriber())
    monkeypatch.setenv("VIDEO_TO_SUMMARY_JOB_TIMEOUT", "0.2")
    monkeypatch.setattr(web_tasks, "output_base", lambda: str(tmp_path))

    web_tasks.run_job(job)
    assert job.status == "failed"
    assert "timeout" in (job.error or "")


def test_cancel_running_job_persists_intent() -> None:
    """running 任务取消：CANCEL_REQUESTED 必须落库（进程重启后恢复时不再重跑）。"""
    from video_to_summary import db as store_db
    from video_to_summary.web import tasks as web_tasks

    job = web_tasks.create_job({"source_type": "url", "url": "https://x"})
    job.mark_running()
    assert web_tasks.cancel_job(job.job_id) is True

    with store_db.get_conn() as conn:
        row = conn.execute("SELECT progress FROM jobs WHERE job_id = ?", (job.job_id,)).fetchone()
    assert row is not None and "cancel_requested" in (row["progress"] or "")
