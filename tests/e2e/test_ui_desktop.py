"""Browser contract test; native WebView checks run separately from frozen artifacts."""
import pytest

pytestmark = pytest.mark.e2e


def test_delayed_bridge_blocks_initial_api_and_authenticates(ui_page, e2e_server):
    base_url = e2e_server.base_url
    page = ui_page
    seen = []
    def main(route):
        response = route.fetch()
        route.fulfill(response=response, body=response.text().replace("<head>", '<head><meta name="vts-desktop" content="1">'))
    page.route(base_url + "/", main)
    page.route("**/api/v1/**", lambda route: (seen.append(route.request.headers.get("authorization")), route.continue_()))
    page.add_init_script("""
      setTimeout(() => {
        window.pywebview = { api: {
          get_session: async () => ({ origin: location.origin, token: 'fixture-desktop-session', version: 'test' }),
          pick_file: async () => null
        } };
        window.dispatchEvent(new Event('pywebviewready'));
      }, 800);
    """)
    page.goto(base_url)
    assert page.evaluate("Boolean(window.__APP_READY__)") is False
    assert not seen
    page.wait_for_function("window.__APP_READY__ === true")
    page.wait_for_function("document.querySelector('#envStatus').textContent.includes('服务正常')")
    assert seen and all(header == "Bearer fixture-desktop-session" for header in seen)
    assert page.evaluate("Object.values(localStorage).some(value => value.includes('fixture-desktop-session'))") is False


def test_exit_finds_active_job_beyond_first_page(ui_page, e2e_server):
    page = ui_page
    base = e2e_server.base_url
    def main(route):
        response = route.fetch()
        route.fulfill(response=response, body=response.text().replace("<head>", '<head><meta name="vts-desktop" content="1">'))
    page.route(base + "/", main)
    def jobs(route):
        from urllib.parse import parse_qs, urlparse
        offset = int(parse_qs(urlparse(route.request.url).query).get("offset", ["0"])[0])
        values = [{"status": "completed"}] * 200 if offset == 0 else [{"status": "pending"}]
        route.fulfill(json={"jobs": values, "total": 201})
    page.route("**/api/v1/jobs?*", jobs)
    page.add_init_script("""
      window.pywebview = { api: {
        get_session: async () => ({ origin: location.origin, token: 'fixture-session', version: 'test' }),
        request_exit: async () => { window.__EXIT_CALLED__ = true; }
      } };
    """)
    page.goto(base)
    page.wait_for_function("window.__APP_READY__ === true")
    page.evaluate("window.dispatchEvent(new Event('vts:desktop-close'))")
    page.locator('#desktopExitConfirm').wait_for(state='visible')
    assert not page.evaluate("Boolean(window.__EXIT_CALLED__)")
    page.locator('#desktopExitConfirm').click()
    page.wait_for_function("window.__EXIT_CALLED__ === true")
