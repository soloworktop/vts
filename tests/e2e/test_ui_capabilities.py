"""UI e2e：``GET /api/v1/capabilities`` 契约与固定 UI 形态（0 插件构建）。

核心不预置能力位、本构建不装载任何插件：接口恒返回空对象 ``{}``，
前端无任何能力驱动的动态分支——设置页 tab 固定为 任务默认/LLM 配置/
总结模板/标签管理 四个；导出菜单只有 Markdown 项（PDF 分支已从前端删除，
不存在渲染路径，而非 disabled）。
"""

import pytest

pytestmark = pytest.mark.e2e


def test_ui_capabilities_endpoint_returns_empty_object(e2e_server):
    """0 插件：``GET /api/v1/capabilities`` 返回 ``{}``（非 null、无任何键）。"""
    caps = requests_get_caps(e2e_server)
    assert caps == {}


def test_ui_settings_tabs_are_fixed(ui_page, e2e_server, goto_console):
    """设置页 tab 固定四个：任务默认 / LLM 配置 / 总结模板 / 标签管理。"""
    goto_console()
    ui_page.click('.nav-item[data-view="settings"]')
    ui_page.wait_for_selector("#view-settings:not(.hidden)")
    tabs = ui_page.locator("#view-settings .tab")
    assert tabs.count() == 4
    texts = [tabs.nth(i).inner_text().strip() for i in range(tabs.count())]
    assert texts == ["任务默认", "LLM 配置", "总结模板", "标签管理"]


def test_ui_export_menu_has_no_pdf_item(ui_page, e2e_server, goto_console, submit_local_job, wait_ui_status):
    """导出菜单只有 Markdown 项：PDF 项完全不渲染（分支已删除，不是 disabled）。"""
    submit_local_job("能力契约任务")
    wait_ui_status("completed")
    ui_page.click('.nav-item[data-view="history"]')
    ui_page.wait_for_selector("#historyDetailCard:not(.hidden)")
    ui_page.wait_for_selector("#productBody.rendered")

    ui_page.click("#exportProductBtn")
    ui_page.wait_for_selector("#exportMenu")
    items = ui_page.locator("#exportMenu .export-menu-item")
    assert items.count() == 1
    assert ui_page.locator("#exportMenu .export-menu-item[data-format='md']").count() == 1
    assert ui_page.locator("#exportMenu .export-menu-item[data-format='pdf']").count() == 0


def requests_get_caps(e2e_server):
    import requests

    return requests.get(f"{e2e_server.base_url}/api/v1/capabilities", timeout=5).json()
