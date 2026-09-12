"""L2 浏览器 E2E：设置视图（模板 CRUD / 任务默认 / LLM 配置 / 标签管理 / 主题 / 日志导出 / 目录浏览）。"""

import pytest
import requests

from video_to_summary.summarizers.openai import SUMMARY_TEMPLATES

pytestmark = pytest.mark.e2e


def _open_settings(ui_page, goto_console, panel: str = "tabDefaults"):
    goto_console()
    ui_page.click('.nav-item[data-view="settings"]')
    ui_page.wait_for_selector("#view-settings:not(.hidden)")
    if panel:
        ui_page.click(f'.tab[data-panel="{panel}"]')
        # tab 点击会触发异步回填，等网络空闲避免「先选择后被服务器状态覆盖」的竞态
        ui_page.wait_for_load_state("networkidle")


def test_ui_template_crud(ui_page, e2e_server, goto_console):
    _open_settings(ui_page, goto_console, panel="tabTemplate")
    # 启动即刷新模板列表：内置通用 + BiliNote 风格内置（option 元素不算 visible，等 attached）
    ui_page.wait_for_selector('#templateName option[value="通用"]', state="attached")
    assert ui_page.locator("#templateName option").count() == len(SUMMARY_TEMPLATES)

    # 新建自定义模板（v4 单段提示词模型）。
    # 注意不要断言「已保存：x」状态文案——保存后的刷新可能覆盖它，属瞬态文本；
    # 断言持久结果（下拉选项 + API 落库）。
    ui_page.click("#createTemplateBtn")
    ui_page.fill("#templateTitle", "e2e模板")
    ui_page.fill("#templatePrompt", "用一段话总结这份文稿的核心结论与关键数字")
    ui_page.click("#saveTemplateBtn")
    ui_page.wait_for_selector('#templateName option[value="e2e模板"]', state="attached")

    # 落库验证：DB 自定义模板可经 API 读回
    tpl = requests.get(f"{e2e_server.base_url}/api/v1/templates/e2e模板", timeout=5).json()
    assert "一段话" in tpl["template"]["prompt"]

    # 删除 → 回落内置模板集合（同样断言持久结果）
    ui_page.select_option("#templateName", "e2e模板")
    ui_page.once("dialog", lambda dialog: dialog.accept())
    ui_page.click("#deleteTemplateBtn")
    ui_page.wait_for_function(
        f"document.querySelectorAll('#templateName option').length === {len(SUMMARY_TEMPLATES)}"
        " && [...document.querySelectorAll('#templateName option')].some((o) => o.value === '通用')"
    )
    assert (
        requests.get(f"{e2e_server.base_url}/api/v1/templates", timeout=5).json()["templates"]
        == list(SUMMARY_TEMPLATES.keys())
    )

    # 内置模板只读：选中「通用」后编辑器与保存/删除按钮禁用
    ui_page.select_option("#templateName", "通用")
    ui_page.wait_for_function("document.getElementById('templatePrompt') && document.getElementById('templatePrompt').disabled")
    assert ui_page.locator("#saveTemplateBtn").is_disabled()
    assert ui_page.locator("#deleteTemplateBtn").is_disabled()


def test_ui_settings_task_defaults(ui_page, e2e_server, goto_console):
    _open_settings(ui_page, goto_console, panel="tabDefaults")
    ui_page.wait_for_selector("#defaultSubtitlePreference")

    ui_page.select_option("#defaultSubtitlePreference", "manual_only")
    ui_page.click("#saveDefaultsBtn")
    ui_page.wait_for_function(
        "document.getElementById('defaultsStatus').textContent === '已保存'"
    )

    # 后端确实持久化
    settings = requests.get(f"{e2e_server.base_url}/api/v1/settings", timeout=5).json()
    assert settings["subtitle_preference"] == "manual_only"

    # 刷新后回显（前端启动固定落在「新建」视图，需重新进入设置）
    ui_page.reload()
    ui_page.wait_for_selector("#submitBtn")
    ui_page.click('.nav-item[data-view="settings"]')
    ui_page.wait_for_selector("#view-settings:not(.hidden)")
    ui_page.click('.tab[data-panel="tabDefaults"]')
    ui_page.wait_for_load_state("networkidle")
    assert ui_page.locator("#defaultSubtitlePreference").input_value() == "manual_only"


def test_ui_theme_toggle(ui_page, goto_console):
    goto_console()

    # headless chromium 默认 prefers-color-scheme: light → auto 解析为 light
    initial = ui_page.evaluate("document.documentElement.dataset.theme")
    assert initial == "light"

    ui_page.click("#themeToggle")  # auto → dark
    assert ui_page.evaluate("document.documentElement.dataset.theme") == "dark"
    assert ui_page.evaluate("localStorage.getItem('vts_theme')") == "dark"

    ui_page.click("#themeToggle")  # dark → light
    assert ui_page.evaluate("document.documentElement.dataset.theme") == "light"

    # 刷新后记忆保持（回到 light 模式，localStorage 不重置）
    ui_page.reload()
    ui_page.wait_for_selector("#submitBtn")
    assert ui_page.evaluate("localStorage.getItem('vts_theme')") == "light"
    assert ui_page.evaluate("document.documentElement.dataset.theme") == "light"


def test_ui_browse_button_single(ui_page, goto_console):
    """本地文件 tab 只渲染一个「浏览…」按钮；pywebviewready 重放不产生重复注入。"""
    goto_console()
    ui_page.click('.tab[data-panel="tabLocal"]')
    assert ui_page.locator("#browseFileBtn").count() == 1

    # 模拟桌面环境的冗余事件：React 挂载一次，重放不复制 DOM
    ui_page.evaluate("window.dispatchEvent(new Event('pywebviewready'))")
    ui_page.wait_for_timeout(200)
    assert ui_page.locator("#browseFileBtn").count() == 1
    assert ui_page.locator("#recentPathSelect").count() <= 1


def test_ui_log_export_download(ui_page, e2e_server, goto_console):
    """设置视图「导出日志」→ 真实附件下载（/api/v1/logs/export），交付物含三段结构。"""
    goto_console()
    ui_page.click('.nav-item[data-view="settings"]')
    ui_page.wait_for_selector("#exportLogsBtn")

    with ui_page.expect_download() as download_info:
        ui_page.click("#exportLogsBtn")
    download = download_info.value
    assert download.suggested_filename.startswith("video-to-summary-logs-")

    content = download.path().read_text(encoding="utf-8")
    assert "=== video-to-summary 诊断日志 ===" in content
    assert "=== 最近任务" in content
    assert "=== 运行日志" in content
    assert "已自动脱敏" in content


def test_ui_web_file_browser(ui_page, goto_console, make_media_file, e2e_server):
    """浏览按钮：fs/pick 失败（无 GUI 会话）→ 回退后端目录浏览面板，点选回填绝对路径。"""
    media = make_media_file("browse.mp3")
    # 打桩 fs/pick 返回 error → 前端回退 /api/v1/fs/browse 目录面板
    ui_page.route(
        "**/api/v1/fs/pick",
        lambda route: route.fulfill(json={"error": "native file dialog unsupported (test)"}),
    )
    goto_console()
    ui_page.click('.tab[data-panel="tabLocal"]')

    # 真实用户流：先把已知路径粘进输入框（fill），点「浏览」后面板从该目录起步
    ui_page.fill("#audioPath", str(media))
    ui_page.click("#browseFileBtn")
    ui_page.wait_for_selector("#webFileBrowser:not(.hidden)")
    ui_page.wait_for_selector("#webFileBrowser .fb-file", state="visible")
    ui_page.click("#webFileBrowser .fb-file")

    # 点选媒体文件 → 绝对路径回填输入框、面板收起
    assert ui_page.locator("#audioPath").input_value() == str(media)
    assert ui_page.locator("#webFileBrowser").count() == 0


def test_ui_llm_slots_config(ui_page, e2e_server, goto_console):
    """LLM 配置（BYOK）：两槽位（推理模型 / 语音识别模型）表单保存 → API 读回掩码 Key。"""
    _open_settings(ui_page, goto_console, panel="tabLlm")
    # 两槽位表单渲染（值来自 GET /api/v1/llm；空库 = 未配置）
    ui_page.wait_for_selector("#llm-summary-baseUrl")
    assert ui_page.locator("#llm-asr-model").count() == 1

    # 填写推理模型槽位并保存
    ui_page.fill("#llm-summary-baseUrl", "https://api.example.test/v1")
    ui_page.fill("#llm-summary-model", "test-model")
    ui_page.fill("#llm-summary-apiKey", "sk-e2e-secret-123456")
    ui_page.click("#llmSaveBtn")

    # 保存成功后槽位刷新：Key 输入框占位符带出掩码值（前端收到 GET /llm 的新状态）
    ui_page.wait_for_selector('#llm-summary-apiKey[placeholder*="当前："]')

    # 落库验证：API 读回两槽位，掩码 Key（绝不返回明文）
    cfg = requests.get(f"{e2e_server.base_url}/api/v1/llm", timeout=5).json()
    assert cfg["summary"]["configured"] is True
    assert cfg["summary"]["base_url"] == "https://api.example.test/v1"
    assert cfg["summary"]["model"] == "test-model"
    assert "****" in cfg["summary"]["api_key"]
    assert "123456" not in cfg["summary"]["api_key"]
    # 未填写的语音识别槽位保持未配置
    assert cfg["asr"]["configured"] is False


def test_ui_label_management(ui_page, e2e_server, goto_console):
    """标签管理页：新建 / 重命名 / 合并 / 删除。"""
    requests.post(f"{e2e_server.base_url}/api/v1/labels", json={"name": "A标签"}, timeout=5)
    requests.post(f"{e2e_server.base_url}/api/v1/labels", json={"name": "B标签"}, timeout=5)
    _open_settings(ui_page, goto_console, panel="tabLabels")
    ui_page.wait_for_selector('#labelMgmtList .label-mgmt-row:has-text("A标签")')

    # 新建
    ui_page.fill("#newLabelName", "C标签")
    ui_page.click("#createLabelBtn")
    ui_page.wait_for_selector('#labelMgmtList .label-mgmt-row:has-text("C标签")')

    # 重命名 A标签 → A2标签
    row = ui_page.locator('#labelMgmtList .label-mgmt-row:has-text("A标签")')
    row.locator('button:has-text("重命名")').click()
    row.locator('input').fill("A2标签")
    row.locator('button:has-text("确认")').click()
    ui_page.wait_for_selector('#labelMgmtList .label-mgmt-row:has-text("A2标签")')
    assert ui_page.locator('#labelMgmtList .label-mgmt-row:has-text("A标签")').count() == 0

    # 合并 B标签 → A2标签（source=B, target=A2）
    row_b = ui_page.locator('#labelMgmtList .label-mgmt-row:has-text("B标签")')
    row_b.locator('button:has-text("合并到")').click()
    target_opt = row_b.locator('select option').filter(has_text="A2标签").first
    row_b.locator('select').select_option(target_opt.get_attribute("value"))
    row_b.locator('button:has-text("确认")').click()
    ui_page.wait_for_function(
        "[...document.querySelectorAll('#labelMgmtList .label-mgmt-row')]"
        ".every(r => !r.textContent.includes('B标签'))"
    )

    # 删除 C标签：confirm → 行消失
    ui_page.once("dialog", lambda dialog: dialog.accept())
    row_c = ui_page.locator('#labelMgmtList .label-mgmt-row:has-text("C标签")')
    row_c.locator('button:has-text("删除")').click()
    ui_page.wait_for_function(
        "[...document.querySelectorAll('#labelMgmtList .label-mgmt-row')]"
        ".every(r => !r.textContent.includes('C标签'))"
    )

    # 落库核对：只剩 A2标签
    labels = requests.get(f"{e2e_server.base_url}/api/v1/labels", timeout=5).json()["labels"]
    names = [l["name"] for l in labels]
    assert names == ["A2标签"]
