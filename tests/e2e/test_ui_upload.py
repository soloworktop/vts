"""L2 浏览器 E2E：上传任务源的「前后端全流程」（真实 chromium + web-src 构建产物）。

用户操作链路：本地文件页签 → 点击上传 → 选择文件（隐藏 input 注入）→ XHR 上传
建任务 → 自动跳转任务状态页 → 历史可见 → UI 删除后服务端回收托管上传文件。
后端链路细节（multipart 落盘/413/扩展名校验）在 test_api_upload.py 已覆盖；
本文件验证的是前端接线与两端贯通（jsx → endpoints.ts → /api/v1/jobs/upload）。
"""

import time
from pathlib import Path

import pytest
import requests

pytestmark = pytest.mark.e2e

MEDIA_BYTES = b"\xff\xfb" + b"0" * 256


def _wait_upload_job(e2e_server, timeout_s: float = 30.0) -> dict:
    """轮询直到出现 source_path 位于 uploads 托管目录的任务（上传链路的创建凭证）。"""
    uploads_root = str((e2e_server.output_dir / "uploads").resolve())
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        jobs = requests.get(f"{e2e_server.base_url}/api/v1/jobs", timeout=5).json()["jobs"]
        mine = [j for j in jobs if j.get("source_path", "").startswith(uploads_root)]
        if mine:
            return mine[0]
        time.sleep(0.3)
    raise TimeoutError("上传任务未在时限内创建")


def test_ui_upload_full_flow(ui_page, goto_console, e2e_server, wait_ui_status, tmp_path) -> None:
    media = tmp_path / "ui-upload.mp3"
    media.write_bytes(MEDIA_BYTES)

    goto_console()
    ui_page.click('.tab[data-panel="tabLocal"]')
    # 上传入口在 Web 形态常驻（桌面壳才隐藏）；Playwright 对隐藏 input 可直接注入文件
    ui_page.wait_for_selector("#uploadFileBtn")
    ui_page.set_input_files("#uploadFileInput", str(media))

    # 前端 XHR → 后端落盘建任务：托管目录出现源文件
    job = _wait_upload_job(e2e_server)
    stored = Path(job["source_path"])
    assert stored.is_file() and stored.read_bytes() == MEDIA_BYTES

    # 创建成功 → 自动切换到任务状态页（上传链路的 UI 反馈；NewJobView 卸载）
    ui_page.wait_for_selector("#view-status")
    wait_ui_status("completed")

    # 未填标题 → 与本地文件源同一派生口径：文件名派生后被产物 H1（stem）回写
    detail = requests.get(f"{e2e_server.base_url}/api/v1/jobs/{job['job_id']}", timeout=5).json()
    assert detail["title"] == "ui-upload"

    # 历史页可见该任务（前端轮询收敛后渲染；展示的是回写后的标题）
    ui_page.click('.nav-item[data-view="history"]')
    ui_page.wait_for_selector('#jobHistory .history-title:has-text("ui-upload")')

    # UI 删除 → 服务端连带回收托管上传文件（前后端全流程闭环）
    ui_page.once("dialog", lambda dialog: dialog.accept())
    ui_page.click("#deleteHistoryBtn")
    ui_page.wait_for_function(
        "document.querySelectorAll('#jobHistory .history-item').length === 0"
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        if requests.get(f"{e2e_server.base_url}/api/v1/jobs/{job['job_id']}", timeout=5).status_code == 404:
            break
        time.sleep(0.2)
    else:
        raise TimeoutError("删除后任务仍可查询")
    uploads_root = e2e_server.output_dir / "uploads"
    assert list(uploads_root.iterdir()) == [], "删除任务后 uploads 托管目录未回收"
    assert not stored.exists()
