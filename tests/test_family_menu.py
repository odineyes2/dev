# 계정 메뉴의 Family 링크·로그인 전환과 낮·밤 데스크톱·모바일 배치를 검사한다.
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
CAPTURES = ROOT / 'tests' / '__pycache__' / 'family-menu'
LINKS = ['nightshift', 'moai', 'charchat', 'ssh']


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class Api:
    def __init__(self):
        self.logged_in = True
        self.logins = self.logouts = 0

    def handle(self, route):
        url = urlsplit(route.request.url)
        if url.hostname != '127.0.0.1':
            return route.abort()
        if not url.path.startswith('/api/'):
            return route.continue_()
        path = url.path[5:]
        if path == 'auth/me':
            data = {'actor': {'kind': 'human', 'name': 'admin'} if self.logged_in else None,
                    'nightshift_url': 'https://nightshift.lomebrote.com/'}
        elif path == 'auth/logout':
            self.logged_in = False
            self.logouts += 1
            data = {'ok': True}
        elif path == 'auth/login':
            self.logged_in = True
            self.logins += 1
            data = {'ok': True}
        elif path in ('projects', 'agents', 'jobs'):
            data = {path: []}
        elif path == 'settings/tokens':
            data = {'unit': 'auto'}
        elif path == 'health':
            data = {'ok': True}
        else:
            raise AssertionError(f'예상하지 않은 API: {path}')
        route.fulfill(json=data)


def menu(page):
    page.locator('#user-chip').click()
    panel = page.get_by_role('menu')
    expect(panel).to_be_visible()
    expect(page.locator('#user-chip')).to_have_attribute('aria-expanded', 'true')
    expect(panel.locator('a')).to_have_text([f'{name} 열기' for name in LINKS])
    for name in LINKS:
        link = panel.get_by_role('menuitem', name=f'{name} 열기', exact=True)
        expect(link).to_be_visible()
        expect(link).to_have_attribute('href', f'https://{name}.lomebrote.com/')
        expect(link).to_have_attribute('target', '_blank')
        expect(link).to_have_attribute('rel', 'noopener')
        expect(link.locator('svg use')).to_have_attribute('href', '#i-external-link')
    expect(page.locator('[id*="jupyter"], a[href*="jupyter"]')).to_have_count(0)
    assert 'jupyter' not in panel.inner_text().lower()
    assert panel.evaluate('''m => {
      const r = m.getBoundingClientRect(), items = [...m.children];
      const bounds = items.map(x => x.getBoundingClientRect());
      return r.left >= 0 && r.right <= innerWidth && r.top >= 0 && r.bottom <= innerHeight
        && m.scrollWidth <= m.clientWidth && m.scrollHeight <= m.clientHeight
        && document.documentElement.scrollWidth <= innerWidth
        && bounds.every((b, i) => b.left >= r.left && b.right <= r.right
          && b.top >= r.top && b.bottom <= r.bottom
          && (!i || b.top >= bounds[i-1].bottom)
          && document.elementFromPoint(b.x + b.width/2, b.y + b.height/2)?.closest('#user-menu') === m)
        && items.every(x => x.scrollWidth <= x.clientWidth);
    }'''), '메뉴와 항목은 화면 안에서 잘림·겹침·가로 넘침 없이 보여야 한다'
    return panel


def run():
    CAPTURES.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(ROOT / 'static')))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                for theme in ('light', 'dark'):
                    for width in (1300, 390):
                        page = browser.new_page(viewport={'width': width, 'height': 800}, reduced_motion='reduce')
                        errors = []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        api = Api()
                        page.route('**/*', api.handle)
                        page.add_init_script(f"localStorage.setItem('dev.theme', '{theme}')")
                        page.goto(f'http://127.0.0.1:{server.server_port}/#/settings/fonts')
                        expect(page.locator('#shell')).to_be_visible()
                        expect(page.locator('#user-chip-name')).to_have_text('admin')
                        panel = menu(page)
                        page.screenshot(path=str(CAPTURES / f'{theme}-{width}.png'))
                        page.keyboard.press('Escape')
                        expect(panel).to_be_hidden()
                        expect(page.locator('#user-chip')).to_have_attribute('aria-expanded', 'false')
                        menu(page)
                        page.locator('.top').click(position={'x': 1, 'y': 1})
                        expect(panel).to_be_hidden()
                        menu(page)
                        page.locator('#logout').click()
                        expect(page.locator('#login')).to_be_visible()
                        expect(page.locator('#shell')).to_be_hidden()
                        expect(panel).to_be_hidden()
                        page.locator('#login-username').fill('admin')
                        page.locator('#login-password').fill('mock-password')
                        page.locator('#login-form button[type=submit]').click()
                        expect(page.locator('#shell')).to_be_visible()
                        expect(page.locator('#login')).to_be_hidden()
                        menu(page)
                        assert api.logins == api.logouts == 1
                        assert not errors, errors
                        page.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    print(f'PASS: Family 링크·로그아웃·재로그인·Esc·바깥 클릭, 낮/밤 × 1300/390. 캡처: {CAPTURES}')


if __name__ == '__main__':
    run()
