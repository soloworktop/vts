"""L2 浏览器 E2E：真实 chromium 驱动新 React 前端（web-src 构建产物）。

断言的是前端真实渲染的 DOM 状态。视图拆分后的页面结构：
- 任务状态页（#view-status）：非 completed 任务的流水线卡片（stepper/停止/重试），
  任务完成即从状态页移出，由 toast 引导去历史页。
- 历史任务页（#view-history）：左列筛选/搜索/分组列表（状态点），右列产物详情
  （总结默认渲染 markdown，转写原文纯文本，分段/字幕收进「更多」折叠区）。
后端为真实服务器 + 进程内 fakes（见 tests/e2e/fakes.py）。
"""

import time

import pytest
import requests

pytestmark = pytest.mark.e2e


def _wait_any_job(e2e_server, timeout_s: float = 30.0) -> dict:
    """轮询 API 直到任务创建（假体秒完时卡片可能从不渲染，UI 瞬态不可靠）。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        jobs = requests.get(f"{e2e_server.base_url}/api/v1/jobs", timeout=5).json()["jobs"]
        if jobs:
            return jobs[0]
        time.sleep(0.3)
    raise TimeoutError("任务未在时限内创建")


def _submit_url_job(ui_page, goto_console, e2e_server, title, url, *, template=None):
    goto_console()
    ui_page.fill("#url", url)  # URL tab 只有 #url；标题交给 download_done 回写
    if template:
        # 单任务总结模板：下拉异步填充（/api/v1/templates），等选项就绪再选择
        ui_page.wait_for_selector('#jobTemplate option[value="通用"]', state="attached")
        ui_page.select_option("#jobTemplate", template)
    ui_page.click("#submitBtn")
    _wait_any_job(e2e_server)


def _open_history(ui_page):
    ui_page.click('.nav-item[data-view="history"]')
    ui_page.wait_for_selector("#historyDetailCard:not(.hidden)")


def test_ui_local_job_happy_path(ui_page, goto_console, submit_local_job, wait_ui_status):
    submit_local_job("UI 本地任务")
    wait_ui_status("completed")

    # 完成即移出：状态页在下一个轮询周期内移除该任务的流水线卡片
    ui_page.wait_for_function("document.querySelectorAll('#statusList .status-card').length === 0")

    # 历史页：自动选中最新任务，总结默认 markdown 渲染（marked + DOMPurify 白名单）
    _open_history(ui_page)
    ui_page.wait_for_selector("#productBody.rendered")
    body = ui_page.locator("#productBody")
    assert body.locator("h1", has_text="UI 本地任务").count() >= 1
    assert body.locator("strong").count() >= 1  # 加粗
    assert body.locator("table").count() >= 1  # 表格
    assert body.locator('span[style*="color"]').count() >= 1  # 受限 color span 存活
    assert "珊瑚色强调文本" in body.inner_text()

    # 转写原文 tab：纯文本内容（不渲染 markdown）
    ui_page.click('.product-tab[data-key="transcript"]')
    ui_page.wait_for_function("document.getElementById('productBody').textContent.includes('fake transcript')")
    assert "rendered" not in (ui_page.locator("#productBody").get_attribute("class") or "")

    # 低频产物收进「更多」折叠区：分段 / 字幕(SRT)
    ui_page.wait_for_selector("#moreGroup:not(.hidden)")
    ui_page.click("#moreGroup summary")
    ui_page.click('#moreGroup .more-item[data-key="subtitle"]')
    ui_page.wait_for_function("document.getElementById('productBody').textContent.includes('fake segment')")

    # 过程回放：折叠区展开后可见终态 stepper 与完整事件链（无需额外请求）
    ui_page.wait_for_selector("#processGroup:not(.hidden)")
    ui_page.click("#processGroup summary")
    ui_page.wait_for_selector("#processLog .progress-item")
    ui_page.wait_for_selector('#processStepper .stage[data-stage="transcribe"].done')

    # 已完成任务保留重试入口，语义为「重新生成」（用当前全局默认重跑）
    assert ui_page.locator("#retryHistoryBtn").is_visible()
    assert ui_page.locator("#retryHistoryBtn").inner_text() == "重新生成"


def test_ui_url_job_via_form(ui_page, e2e_server, goto_console, make_media_file, wait_ui_status, monkeypatch):
    from video_to_summary.web import tasks as web_tasks

    from fakes import FakeUrlSource

    media = make_media_file()
    monkeypatch.setattr(web_tasks, "_build_source", lambda settings, job: FakeUrlSource(media))

    _submit_url_job(ui_page, goto_console, e2e_server, "UI URL 任务", "https://example.test/watch?v=ui", template="精简笔记")
    wait_ui_status("completed")

    # download_done 携带真实标题 → 任务标题回写，历史列表展示真实标题
    jobs = requests.get(f"{e2e_server.base_url}/api/v1/jobs", timeout=5).json()["jobs"]
    row = next(j for j in jobs if j["title"] == "E2E 视频标题")
    # 单任务选择的总结模板落进 payload，列表接口返回该字段
    assert row["summary_template"] == "精简笔记"
    detail = requests.get(f"{e2e_server.base_url}/api/v1/jobs/{row['job_id']}", timeout=5).json()
    assert detail["status"] == "completed"
    _open_history(ui_page)
    ui_page.wait_for_selector('#jobHistory .history-title:has-text("E2E 视频标题")')
    # 历史列表行展示该任务使用的总结模板
    ui_page.wait_for_selector('#jobHistory .tpl-pill:has-text("精简笔记")')


def test_ui_failed_job_error_banner(ui_page, e2e_server, goto_console, submit_local_job, wait_ui_status):
    e2e_server.transcriber.fail = RuntimeError("boom-e2e-ui")
    submit_local_job("UI 失败任务")
    wait_ui_status("failed")

    # 失败卡片保留在状态页：三段式错误（人话标题 + 折叠原始错误全文）
    error = ui_page.locator("#statusList .status-card .card-error")
    assert "visible" in (error.get_attribute("class") or "")
    assert error.locator(".error-head").inner_text() == "任务失败"
    raw_text = error.locator(".error-raw pre").text_content() or ""
    assert "boom-e2e-ui" in raw_text
    # 失败态允许重试
    assert ui_page.locator('#statusList [data-action="retry"]').is_visible()
    # 卡片展示该任务使用的总结模板 + 重试换模板下拉（选项与新建表单一致）
    ui_page.wait_for_selector('#statusList .status-card .tpl-pill')
    retry_select = ui_page.locator("#statusList .card-retry-template")
    assert retry_select.is_visible()
    assert retry_select.locator("option").count() > 1
    # 换模板重试：选中「精简笔记」后提交 → payload 模板被替换
    retry_select.select_option("精简笔记")
    ui_page.wait_for_timeout(200)  # 等 React 状态回流到重试按钮
    ui_page.click('#statusList [data-action="retry"]')
    row = next(j for j in requests.get(f"{e2e_server.base_url}/api/v1/jobs", timeout=5).json()["jobs"] if j["title"] == "UI 失败任务")
    deadline = time.time() + 30
    while True:
        detail = requests.get(f"{e2e_server.base_url}/api/v1/jobs/{row['job_id']}", timeout=5).json()
        # detail 响应的 summary_template 在顶层（见 get_job_api）
        if detail.get("summary_template") == "精简笔记":
            break
        if time.time() > deadline:
            raise TimeoutError("重试后的 payload 模板未替换")
        time.sleep(0.3)
    wait_ui_status("failed")  # 二次失败后任务回到可重试态

    # 历史页也会出现失败任务：详情给同样的人话错误 + 重试入口
    _open_history(ui_page)
    ui_page.wait_for_selector("#historyDetailCard .error-head:has-text('任务失败')")
    assert ui_page.locator("#retryHistoryBtn").is_visible()
    # 操作栏不放内联模板下拉；点击「重试」后弹出模板选择弹窗，默认选中任务当前模板
    assert ui_page.locator("#retryTplWrap").count() == 0
    ui_page.click("#retryHistoryBtn")
    ui_page.wait_for_selector("#retryTplModal:not(.hidden)")
    modal_select = ui_page.locator("#retryTplModalSelect")
    assert modal_select.input_value() == "精简笔记"
    # 取消 → 关闭弹窗、不触发重试（状态仍为 failed）
    modal_select.select_option("详细笔记")
    ui_page.click("#retryTplCancelBtn")
    ui_page.wait_for_function("document.getElementById('retryTplModal') === null || document.getElementById('retryTplModal').offsetParent === null")
    # 再点重试 → 选「详细笔记」确认 → payload 模板替换
    ui_page.click("#retryHistoryBtn")
    ui_page.wait_for_selector("#retryTplModal")
    ui_page.select_option("#retryTplModalSelect", "详细笔记")
    ui_page.click("#retryTplConfirmBtn")
    ui_page.wait_for_function("document.getElementById('retryTplModal') === null || document.getElementById('retryTplModal').offsetParent === null")
    row = next(j for j in requests.get(f"{e2e_server.base_url}/api/v1/jobs", timeout=5).json()["jobs"] if j["title"] == "UI 失败任务")
    assert row["summary_template"] == "详细笔记"


def test_ui_cancel_button(ui_page, e2e_server, goto_console, submit_local_job, wait_ui_status):
    e2e_server.transcriber.delay = 3.0
    submit_local_job("UI 取消任务", wait_terminal=False)
    wait_ui_status("running")

    ui_page.click('#statusList [data-action="stop"]')
    wait_ui_status("cancelled")
    # 已停止卡片保留在状态页：等前端轮询收敛后停止按钮消失、重试可见
    ui_page.wait_for_function(
        "document.querySelectorAll('#statusList [data-action=\"stop\"]').length === 0",
        timeout=10_000,
    )
    assert ui_page.locator('#statusList [data-action="retry"]').is_visible()


def test_ui_stage_stepper(ui_page, e2e_server, goto_console, submit_local_job, wait_ui_status):
    e2e_server.transcriber.delay = 3.0
    e2e_server.transcriber.progress_total = 3
    submit_local_job("UI 阶段任务", wait_terminal=False)

    # 创建后自动进入任务状态页；转写进行中 → transcribe 阶段 active（3s 延迟提供稳定窗口），
    # 分片进度以「就地单行」呈现在卡片内（chunk-progress），不逐片刷事件链
    ui_page.wait_for_selector('#statusList .status-card .stage[data-stage="transcribe"].active')
    ui_page.wait_for_selector("#statusList .status-card .chunk-progress:not(.hidden)")
    assert "0/3" in ui_page.locator("#statusList .chunk-progress").inner_text()

    wait_ui_status("completed")
    # 完成即移出：等状态页卡片清空、空状态引导出现（下一个轮询周期内完成）
    ui_page.wait_for_function("document.querySelectorAll('#statusList .status-card').length === 0")
    assert ui_page.locator("#statusEmpty").is_visible()


def test_ui_history_grouping_and_delete(ui_page, goto_console, submit_local_job, wait_ui_status):
    submit_local_job("UI 历史任务一")
    wait_ui_status("completed")
    submit_local_job("UI 历史任务二")  # 内部重新打开控制台，回到新建视图
    wait_ui_status("completed")

    _open_history(ui_page)
    # 提交后的 refreshHistory 是异步渲染：等列表收敛到 2 条，而非瞬时 count（竞态）
    ui_page.wait_for_function(
        "document.querySelectorAll('#jobHistory .history-item').length === 2"
    )

    # 删除入口在右列详情（防误触）：选中 → 详情加载 → 点删除 → confirm → 列表收敛
    ui_page.once("dialog", lambda dialog: dialog.accept())
    before = ui_page.locator("#jobHistory .history-item").count()
    ui_page.click("#deleteHistoryBtn")
    ui_page.wait_for_function(
        f"document.querySelectorAll('#jobHistory .history-item').length === {before - 1}"
    )
    # 删除后列表仍有一条 → 自动选中剩余任务，右列继续展示其详情
    ui_page.wait_for_selector('#historyTitle:has-text("UI 历史任务一")')


def test_ui_history_search(ui_page, goto_console, submit_local_job, wait_ui_status):
    """搜索框：服务端检索过滤左列列表，Esc 清空恢复。"""
    submit_local_job("UI 搜索苹果任务")
    wait_ui_status("completed")
    submit_local_job("UI 搜索香蕉任务")
    wait_ui_status("completed")

    _open_history(ui_page)
    ui_page.wait_for_function(
        "document.querySelectorAll('#jobHistory .history-item').length === 2"
    )
    ui_page.fill("#historySearch", "苹果")
    ui_page.wait_for_function(
        "document.querySelectorAll('#jobHistory .history-item').length === 1"
    )
    assert "苹果" in ui_page.locator("#jobHistory .history-title").first.inner_text()
    # Esc 清空恢复全量
    ui_page.focus("#historySearch")
    ui_page.keyboard.press("Escape")
    ui_page.wait_for_function(
        "document.querySelectorAll('#jobHistory .history-item').length === 2"
    )


def test_ui_label_pill_and_filter(ui_page, e2e_server, goto_console, make_media_file, wait_ui_status):
    """历史条目出现标签 pill、点筛选 chip 列表收敛、清空恢复。"""
    # 通过 API 创建一个带标签任务（绕过前端 tag editor 的异步补全，直接验证渲染）
    media = make_media_file()
    res = requests.post(
        f"{e2e_server.base_url}/api/v1/jobs",
        json={"source_type": "local", "audio_path": str(media), "title": "UI标签任务", "labels": ["UI系列"]},
        timeout=5,
    )
    job_id = res.json()["job_id"]
    for _ in range(60):
        d = requests.get(f"{e2e_server.base_url}/api/v1/jobs/{job_id}", timeout=5).json()
        if d["status"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.5)

    goto_console()
    _open_history(ui_page)
    ui_page.wait_for_selector("#jobHistory .history-item")
    # 历史条目出现标签 pill
    ui_page.wait_for_selector('#jobHistory .history-item .tag-pill:has-text("UI系列")', state="attached")
    # 筛选栏出现对应 chip
    ui_page.wait_for_selector('#labelFilterBar .label-chip:has-text("UI系列")', state="attached")
    before = ui_page.locator("#jobHistory .history-item").count()
    # 点筛选 chip：列表收敛到只含该标签任务（至少不报错且 chip 变 active）
    ui_page.locator('#labelFilterBar .label-chip:has-text("UI系列")').click()
    ui_page.wait_for_selector('#labelFilterBar .label-chip.active:has-text("UI系列")')
    # 再点取消：恢复全部
    ui_page.locator('#labelFilterBar .label-chip.active:has-text("UI系列")').click()
    ui_page.wait_for_function(
        f"document.querySelectorAll('#jobHistory .history-item').length >= {before}"
    )


def test_ui_label_manage_link(ui_page, e2e_server, goto_console):
    """筛选栏「管理」chip → 跳转设置视图并激活标签管理 tab。"""
    requests.post(f"{e2e_server.base_url}/api/v1/labels", json={"name": "管理跳转"}, timeout=5)
    goto_console()
    ui_page.click('.nav-item[data-view="history"]')
    ui_page.wait_for_selector("#labelFilterBar:not(.hidden)")
    ui_page.click("#labelFilterBar .manage-link")
    ui_page.wait_for_selector("#view-settings:not(.hidden)")
    ui_page.wait_for_selector('.tab[data-panel="tabLabels"].active')
    ui_page.wait_for_selector('#labelMgmtList .label-mgmt-row:has-text("管理跳转")')


def test_ui_recent_source_history_clear(ui_page, goto_console):
    """最近 URL/路径历史：下拉可选用、可一键清空，两类历史互不影响。"""
    ui_page.add_init_script(
        'localStorage.setItem("vts_recent_source_paths",'
        ' JSON.stringify(["/Users/fan/Videos/a.mp4", "/Users/fan/Videos/b.mp3"]));'
        'localStorage.setItem("vts_recent_urls",'
        ' JSON.stringify(["https://www.bilibili.com/video/BV1xx411c7mD", "https://example.com/v.mp4"]));'
    )
    goto_console()
    # 原生自动填充已关闭：历史只走应用内可清空下拉，不与浏览器输入记忆混淆
    assert ui_page.locator("#url").get_attribute("autocomplete") == "off"
    assert ui_page.locator("#audioPath").get_attribute("autocomplete") == "off"
    # URL tab（默认）：最近 URL 下拉注入，占位 + 2 条 + 分隔 + 清空 = 5 项
    ui_page.wait_for_selector("#recentUrlSelect")
    assert ui_page.locator("#recentUrlSelect option").count() == 5
    # 本地 tab：最近路径下拉，选用回填输入框
    ui_page.click('.tab[data-panel="tabLocal"]')
    ui_page.wait_for_selector("#recentPathSelect")
    ui_page.select_option("#recentPathSelect", "/Users/fan/Videos/a.mp4")
    assert ui_page.input_value("#audioPath") == "/Users/fan/Videos/a.mp4"
    # 一键清空路径历史：下拉移除 + storage 清掉，URL 历史不受影响
    ui_page.select_option("#recentPathSelect", "__clear_recent__")
    assert ui_page.locator("#recentPathSelect").count() == 0
    assert ui_page.evaluate("() => localStorage.getItem('vts_recent_source_paths')") is None
    ui_page.click('.tab[data-panel="tabUrl"]')
    assert ui_page.locator("#recentUrlSelect").count() == 1


def test_ui_onboarding_card_visibility(ui_page, goto_console, submit_local_job):
    """引导卡显隐接通：全新库（无任务）可见；创建首个任务后收起。

    开源版没有门控：是否配置 LLM 不阻断创建（字幕优先路径零 API 成本），
    因此引导卡的显隐只由「是否存在历史任务」决定。
    """
    goto_console()
    ui_page.wait_for_selector("#jobEmpty:not(.hidden)")
    submit_local_job("引导卡显隐任务")
    # 提交成功 → 历史已有任务 → 引导卡收起（或已切走新建视图导致元素卸载，同样通过）
    ui_page.wait_for_function(
        "!document.getElementById('jobEmpty')"
        " || document.getElementById('jobEmpty').classList.contains('hidden')"
        " || getComputedStyle(document.getElementById('jobEmpty')).display === 'none'"
    )


def test_ui_history_row_keyboard_access(ui_page, goto_console, submit_local_job):
    """A21 历史行键盘可达：role=button + tabindex=0，Enter 选中并加载详情。"""
    submit_local_job("键盘可达任务一")
    submit_local_job("键盘可达任务二")
    _open_history(ui_page)
    ui_page.wait_for_function(
        "document.querySelectorAll('#jobHistory .history-item').length === 2"
    )
    rows = ui_page.locator("#jobHistory .history-item")
    assert rows.nth(0).get_attribute("role") == "button"
    assert rows.nth(0).get_attribute("tabindex") == "0"
    # 进入历史视图时自动选中最新一条（第 0 行）；聚焦第 1 行按 Enter → 选中态翻转
    rows.nth(1).focus()
    ui_page.keyboard.press("Enter")
    ui_page.wait_for_function(
        "document.querySelectorAll('#jobHistory .history-item')[1].getAttribute('aria-selected') === 'true'"
    )
    ui_page.wait_for_function(
        "document.querySelectorAll('#jobHistory .history-item')[0].getAttribute('aria-selected') === 'false'"
    )


def test_ui_toast_aria_live(ui_page, goto_console):
    """A22 toast 读屏可达：容器 aria-live=polite；错误类单条 role=alert。"""
    goto_console()
    ui_page.click("#submitBtn")  # 空表单提交 → 「请填写视频 URL」error toast
    ui_page.wait_for_selector(".toast-wrap[aria-live='polite']")
    ui_page.wait_for_selector(".toast[role='alert']")


def test_ui_history_fulltext_search(ui_page, goto_console, submit_local_job, e2e_server):
    """搜索框走服务端 ?q=：正文关键词命中带 <mark> 片段；无命中给空态。"""
    submit_local_job("检索用任务")
    _open_history(ui_page)
    # e2e 假体转写固定为 "fake transcript line one/two"——输入正文关键词
    ui_page.fill("#historySearch", "transcript line")
    ui_page.wait_for_selector("#jobHistory .history-item .history-snippet", timeout=10_000)
    snippet = ui_page.locator("#jobHistory .history-snippet").first
    assert "<mark>" in snippet.inner_html()
    # 无命中 → 空态文案
    ui_page.fill("#historySearch", "绝对不存在的词xyz")
    ui_page.wait_for_selector("#jobHistory .empty")
    assert "没有匹配的任务" in ui_page.locator("#jobHistory .empty").inner_text()


def test_ui_duplicate_source_soft_warning(ui_page, goto_console, e2e_server, make_media_file, monkeypatch):
    """A5 重复来源软提示：同源 URL 提交弹警示（可查看/可放行），不阻断主流程；
    「仍要创建」后本会话内同 URL 不再重复提示。"""
    from video_to_summary.web import tasks as web_tasks

    from fakes import FakeUrlSource

    media = make_media_file()
    monkeypatch.setattr(web_tasks, "_build_source", lambda settings, job: FakeUrlSource(media))
    url = "https://example.test/watch?v=dup"

    goto_console()
    ui_page.fill("#url", url)
    ui_page.click("#submitBtn")
    # 无历史时直接创建（无警示），等任务创建保证检索/列表可见
    deadline = time.time() + 30
    while time.time() < deadline:
        jobs = requests.get(f"{e2e_server.base_url}/api/v1/jobs", timeout=5).json()["jobs"]
        if any(j.get("source_url") == url for j in jobs):
            break
        time.sleep(0.3)
    else:
        raise TimeoutError("首个同源任务未创建")

    # 第二次提交同源 URL → 软提示出现；「仍要创建」放行
    goto_console()
    # 前置断言：服务端检索必须能命中首条同源任务（软提示的数据来源）
    import urllib.parse as _up

    q = _up.quote(url, safe="")
    deadline = time.time() + 15
    while time.time() < deadline:
        total = requests.get(f"{e2e_server.base_url}/api/v1/jobs?q={q}&limit=1", timeout=5).json().get("total", 0)
        if total >= 1:
            break
        time.sleep(0.3)
    else:
        raise TimeoutError("服务端检索未命中首条同源任务（软提示前提不成立）")
    ui_page.fill("#url", url)
    ui_page.click("#submitBtn")
    ui_page.wait_for_selector("#submitWarnings .warn-item")
    assert "同源" in ui_page.locator("#submitWarnings .warn-item").inner_text()
    ui_page.click("#submitWarnings .warn-proceed")

    # 放行后任务创建（总数 2 条同源），且本会话再次提交不再弹提示
    deadline = time.time() + 30
    while time.time() < deadline:
        jobs = requests.get(f"{e2e_server.base_url}/api/v1/jobs", timeout=5).json()["jobs"]
        if sum(1 for j in jobs if j.get("source_url") == url) >= 2:
            break
        time.sleep(0.3)
    else:
        raise TimeoutError("第二次同源任务未创建")
    # 提交成功会切到状态视图：点导航回新建（页面不 reload，会话内记忆仍在）
    ui_page.click('.nav-item[data-view="new"]')
    ui_page.fill("#url", url)
    ui_page.click("#submitBtn")
    # 会话内记忆：不再弹提示、直接放行创建——以「第 3 条同源任务出现」为证
    deadline = time.time() + 30
    while time.time() < deadline:
        jobs = requests.get(f"{e2e_server.base_url}/api/v1/jobs", timeout=5).json()["jobs"]
        if sum(1 for j in jobs if j.get("source_url") == url) >= 3:
            break
        time.sleep(0.3)
    else:
        raise TimeoutError("第三次同源任务未创建（会话内记忆可能失效）")
    assert ui_page.locator("#submitWarnings .warn-item").count() == 0
