"""UI e2e：产物「导出」（点导出→弹格式菜单→选中即触发/真实附件下载）与总结
「编辑」（保存/徽标/服务端落盘/未保存脏检查）。

对应功能：历史详情产物工具条「导出」走服务端真实 attachment（format=md，禁 Blob；
格式菜单不常驻）+ 总结编辑（PUT /api/v1/jobs/{id}/summary）。
本版不提供 PDF 导出（导出菜单无 PDF 项，该分支已从前端删除）——
PDF 断言由 test_ui_capabilities.py 覆盖。
"""

import pytest
import requests

pytestmark = pytest.mark.e2e


def _open_export_menu(ui_page):
    """点「导出」弹出格式菜单（格式选择不常驻）。"""
    ui_page.click("#exportProductBtn")
    ui_page.wait_for_selector("#exportMenu")


def _export_as(ui_page, fmt):
    """完整导出交互：点导出 → 弹菜单 → 选格式（触发导出）。"""
    _open_export_menu(ui_page)
    ui_page.click(f"#exportMenu .export-menu-item[data-format='{fmt}']")


def _open_completed_detail(ui_page, goto_console, submit_local_job, wait_ui_status):
    submit_local_job("UI 导出编辑任务")
    wait_ui_status("completed")
    ui_page.click('.nav-item[data-view="history"]')
    ui_page.wait_for_selector("#historyDetailCard:not(.hidden)")
    ui_page.wait_for_selector("#productBody.rendered")
    return ui_page


def test_ui_export_and_summary_edit(ui_page, goto_console, submit_local_job, wait_ui_status, e2e_server):
    _open_completed_detail(ui_page, goto_console, submit_local_job, wait_ui_status)

    # 「下载」「复制」「常驻格式选择」都已取消；「导出」+「编辑」就绪，菜单默认收起
    assert ui_page.locator("#downloadProductBtn").count() == 0
    assert ui_page.locator("#copyProductBtn").count() == 0
    assert ui_page.locator("#exportFormat").count() == 0
    export_btn = ui_page.locator("#exportProductBtn")
    export_btn.wait_for(state="attached")
    assert export_btn.is_enabled()
    assert ui_page.locator("#editProductBtn").is_visible()
    assert ui_page.locator("#summaryEditedBadge").is_hidden()
    assert ui_page.locator("#exportMenu").count() == 0

    # 导出 Markdown：点导出 → 弹菜单 → 选 Markdown → 真实附件下载（非 Blob）
    with ui_page.expect_download() as dl_md:
        _export_as(ui_page, "md")
    md_path = dl_md.value.path()
    assert md_path.read_text(encoding="utf-8").startswith("#")
    # 文件名 = `<标题>.md`（RFC 5987 filename*），不带源产物后缀
    assert dl_md.value.suggested_filename.startswith("UI 导出编辑任务")
    assert dl_md.value.suggested_filename.endswith(".md")

    # 菜单里没有 PDF 项（分支已删除 → UI 不渲染，避免死按钮）
    # 注：选完格式后菜单自动收起，需重新点开再断言菜单内容
    _open_export_menu(ui_page)
    assert ui_page.locator("#exportMenu .export-menu-item[data-format='pdf']").count() == 0
    assert ui_page.locator("#exportMenu .export-menu-item[data-format='md']").count() == 1

    # 编辑流程：进入编辑态 → 改内容 → 保存 → 徽标出现 + 内容重渲染
    ui_page.click("#editProductBtn")
    editor = ui_page.locator("#productEditor")
    editor.wait_for(state="visible")
    editor.fill("# 编辑后的总结\n\n这是 UI 编辑稿。")
    ui_page.click("#saveProductBtn")
    ui_page.wait_for_function(
        "document.getElementById('summaryEditedBadge') && "
        "!document.getElementById('summaryEditedBadge').classList.contains('hidden')"
    )
    assert "已编辑" in ui_page.locator("#summaryEditedBadge").inner_text()
    ui_page.wait_for_function(
        "document.getElementById('productBody').innerText.includes('这是 UI 编辑稿')"
    )

    # 服务端落盘验证：编辑稿写回产物文件 + summary_edited_at 标记
    jobs = requests.get(f"{e2e_server.base_url}/api/v1/jobs", timeout=5).json()["jobs"]
    mine = [j for j in jobs if j.get("title") == "UI 导出编辑任务"]
    assert mine
    detail = requests.get(f"{e2e_server.base_url}/api/v1/jobs/{mine[0]['job_id']}", timeout=5).json()
    assert detail["summary_edited_at"] > 0
    product = requests.get(
        f"{e2e_server.base_url}/api/v1/jobs/{mine[0]['job_id']}/file",
        params={"path": detail["result_paths"]["summary"]},
        timeout=5,
    ).json()
    assert "这是 UI 编辑稿" in product["content"]

    # 取消编辑：dirty 状态下取消 → 弹「保存/放弃/取消」确认 → 放弃后退出且内容不落盘
    ui_page.click("#editProductBtn")
    editor.wait_for(state="visible")
    editor.fill("# 不会被保存的草稿")
    ui_page.click("#cancelProductBtn")
    ui_page.wait_for_selector("#summaryEditModal")
    ui_page.click("#summaryEditDiscardBtn")
    editor.wait_for(state="hidden")
    assert "不会被保存的草稿" not in ui_page.locator("#productBody").inner_text()


def test_ui_summary_edit_dirty_switch_confirms(ui_page, goto_console, submit_local_job, wait_ui_status):
    """总结编辑未保存改动：切产物 tab 先弹「保存/放弃/取消」——
    放弃才切换，取消留在编辑态（草稿不丢）。"""
    submit_local_job("UI 编辑脏检查任务")
    wait_ui_status("completed")
    ui_page.click('.nav-item[data-view="history"]')
    ui_page.wait_for_selector("#historyDetailCard:not(.hidden)")
    ui_page.wait_for_selector("#productBody.rendered")

    editor = ui_page.locator("#productEditor")
    transcript_tab = ui_page.locator('#productTabs .product-tab[data-key="transcript"]')
    modal = ui_page.locator("#summaryEditModal")

    # 场景一：切 tab → 弹窗 → 放弃修改 → 编辑态退出并切到转写原文
    ui_page.click("#editProductBtn")
    editor.wait_for(state="visible")
    editor.fill("# 未保存的草稿 A")
    transcript_tab.click()
    modal.wait_for(state="visible")
    ui_page.click("#summaryEditDiscardBtn")
    editor.wait_for(state="hidden")
    ui_page.wait_for_function(
        "document.querySelector('#productTabs .product-tab[data-key=\"transcript\"]')"
        ".classList.contains('active')"
    )

    # 场景二：取消 → 留在编辑态，草稿原样保留
    ui_page.click('#productTabs .product-tab[data-key="summary"]')
    ui_page.wait_for_selector("#productBody.rendered")
    ui_page.click("#editProductBtn")
    editor.wait_for(state="visible")
    editor.fill("# 未保存的草稿 B")
    transcript_tab.click()
    modal.wait_for(state="visible")
    ui_page.click("#summaryEditCancelBtn")
    modal.wait_for(state="hidden")
    editor.wait_for(state="visible")
    assert editor.input_value() == "# 未保存的草稿 B"  # 取消未丢草稿
    # 清理：dirty 下取消 → 确认弹窗 → 显式放弃退出编辑态
    ui_page.click("#cancelProductBtn")
    modal.wait_for(state="visible")
    ui_page.click("#summaryEditDiscardBtn")
    editor.wait_for(state="hidden")


def test_ui_export_shows_feedback_toast(ui_page, goto_console, submit_local_job, wait_ui_status, e2e_server):
    """浏览器形态：导出后 toast 告知文件名与去处（不再是静默操作）。"""
    submit_local_job("UI 导出提示任务")
    wait_ui_status("completed")
    ui_page.click('.nav-item[data-view="history"]')
    ui_page.wait_for_selector("#historyDetailCard:not(.hidden)")
    ui_page.wait_for_selector("#productBody.rendered")

    with ui_page.expect_download():
        _export_as(ui_page, "md")
    toast = ui_page.locator(".toast:has-text('已导出')").last
    toast.wait_for(state="visible")
    assert "UI 导出提示任务.md" in toast.inner_text()
    assert "浏览器下载目录" in toast.inner_text()


def test_ui_export_desktop_form_shows_save_panel_hint(ui_page, goto_console, submit_local_job, wait_ui_status, e2e_server):
    """桌面形态（window.pywebview 存在但无导出桥/旧包）：回退真实 URL 下载，
    toast 诚实提示「请在系统保存框选择保存位置」。"""
    submit_local_job("UI 桌面导出任务")
    wait_ui_status("completed")
    ui_page.click('.nav-item[data-view="history"]')
    ui_page.wait_for_selector("#historyDetailCard:not(.hidden)")
    ui_page.wait_for_selector("#productBody.rendered")

    ui_page.evaluate("window.pywebview = { api: {} };")
    with ui_page.expect_download():
        _export_as(ui_page, "md")
    ui_page.wait_for_selector(".toast:has-text('系统保存框')", state="visible")


def test_ui_export_desktop_bridge_shows_completion_and_open(ui_page, goto_console, submit_local_job, wait_ui_status, e2e_server):
    """桌面正式路径（导出桥存在）：Python 接管保存后回传真实路径 → toast
    「已导出到…」+「打开所在文件夹」按钮（点按调用 reveal 桥）。"""
    submit_local_job("UI 桌面桥导出任务")
    wait_ui_status("completed")
    ui_page.click('.nav-item[data-view="history"]')
    ui_page.wait_for_selector("#historyDetailCard:not(.hidden)")
    ui_page.wait_for_selector("#productBody.rendered")

    ui_page.evaluate("""
      window.pywebview = { api: {
        export_product: (url, name) => Promise.resolve({saved: true, cancelled: false, path: "/fake/桌面桥导出任务.md"}),
        reveal_in_finder: (p) => { window.__revealed = p; return true; },
      }};
    """)
    _export_as(ui_page, "md")
    toast = ui_page.locator(".toast:has-text('已导出到')").last
    toast.wait_for(state="visible")
    assert "/fake/桌面桥导出任务.md" in toast.inner_text()
    ui_page.click(".toast-action:has-text('打开所在文件夹')")
    ui_page.wait_for_function("window.__revealed === '/fake/桌面桥导出任务.md'")


def test_ui_export_desktop_bridge_failure_shows_error(ui_page, goto_console, submit_local_job, wait_ui_status, e2e_server):
    """桌面桥保存失败 → toast 提示导出失败（不静默）。"""
    submit_local_job("UI 桌面桥失败任务")
    wait_ui_status("completed")
    ui_page.click('.nav-item[data-view="history"]')
    ui_page.wait_for_selector("#historyDetailCard:not(.hidden)")
    ui_page.wait_for_selector("#productBody.rendered")

    ui_page.evaluate("""
      window.pywebview = { api: {
        export_product: (url, name) => Promise.resolve({saved: false, cancelled: false, path: ""}),
        reveal_in_finder: () => true,
      }};
    """)
    _export_as(ui_page, "md")
    ui_page.wait_for_selector(".toast:has-text('导出失败')", state="visible")
