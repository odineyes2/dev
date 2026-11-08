# 로그인 분할 배치·이미지·인증 전환·짧은 화면 스크롤을 모의 API 브라우저로 검사한다.
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
import sys

from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_family_menu import Api as FamilyApi, QuietHandler

ROOT = Path(__file__).resolve().parents[1]
CAPTURES = ROOT / 'tests' / '__pycache__' / 'login-layout'
MOCK_INPUT = 'mock-password'
ERROR = '아이디와 비밀번호를 확인하고 다시 로그인해 주세요.'


class Api(FamilyApi):
    def __init__(self):
        super().__init__()
        self.logged_in = False
        self.fail = True
        self.attempts = 0

    def handle(self, route):
        if route.request.url.endswith('/api/auth/login'):
            assert route.request.method == 'POST'
            payload = route.request.post_data_json
            assert payload.keys() == {'username', 'password'}
            assert payload['username'] == 'admin'
            assert payload['password'] == MOCK_INPUT
            assert route.request.headers['x-requested-with'] == 'dev'
            self.attempts += 1
            if self.fail:
                return route.fulfill(status=401, json={'detail': ERROR})
        return super().handle(route)


def layout(page, width, centered=True):
    expect(page.locator('#login')).to_be_visible()
    expect(page.locator('#shell')).to_be_hidden()
    art = page.locator('.login-art')
    if width <= 800:
        expect(art).to_be_hidden()
    else:
        expect(art).to_be_visible()
        a = art.bounding_box()
        assert abs(a['width'] - width / 2) < 1 and a['x'] == 0
    panel = page.locator('.login-panel').bounding_box()
    form = page.locator('#login-form').bounding_box()
    assert abs(panel['width'] - (width if width <= 800 else width / 2)) < 1
    assert abs(form['x'] + form['width']/2 - (panel['x'] + panel['width']/2)) < 1
    if centered:
        assert abs(form['y'] + form['height']/2 - page.viewport_size['height']/2) < 1
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert page.locator('#login-form').evaluate('''f => {
      const r = f.getBoundingClientRect(), w = document.querySelector('.login-watermark').getBoundingClientRect();
      const boxes = [...f.children].map(e => e.getBoundingClientRect());
      return r.bottom <= w.top && boxes.every((b,i) => b.left >= r.left && b.right <= r.right + 1
        && (!i || b.top >= boxes[i-1].bottom - 1));
    }''')


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
                        layout(page, width)
                        page.wait_for_function('''() => {
                          const img = document.querySelector('.login-art img');
                          return img.complete && img.naturalWidth > 0;
                        }''')
                        expect(page.locator('.login-art img')).to_have_attribute('alt', '')
                        for field, autocomplete in (('username', 'username'), ('password', 'current-password')):
                            el = page.locator(f'#login-{field}')
                            expect(el).to_have_attribute('required', '')
                            expect(el).to_have_attribute('autocomplete', autocomplete)
                            assert el.get_attribute('aria-label')
                        expect(page.locator('#login-error')).to_have_attribute('role', 'alert')
                        page.screenshot(path=str(CAPTURES / f'{theme}-{width}.png'))
                        page.locator('#login-username').fill('admin')
                        page.locator('#login-password').fill(MOCK_INPUT)
                        page.locator('#login-password').press('Enter')
                        expect(page.locator('#login-error')).to_have_text(ERROR)
                        layout(page, width)
                        page.screenshot(path=str(CAPTURES / f'{theme}-{width}-error.png'))
                        # 키보드가 열린 상황처럼 높이를 줄여 모든 입력과 오류에 스크롤로 닿는지 본다.
                        page.set_viewport_size({'width': width, 'height': 280})
                        layout(page, width, centered=False)
                        assert page.evaluate('document.documentElement.scrollHeight > innerHeight')
                        for selector in ('#login-username', '#login-password', '#login-form button', '#login-error'):
                            el = page.locator(selector)
                            el.scroll_into_view_if_needed()
                            box = el.bounding_box()
                            assert box['y'] >= 0 and box['y'] + box['height'] <= 281
                        page.set_viewport_size({'width': width, 'height': 800})
                        api.fail = False
                        page.locator('#login-password').press('Enter')
                        expect(page.locator('#shell')).to_be_visible()
                        expect(page.locator('#login')).to_be_hidden()
                        expect(page.locator('.login-art img')).to_be_hidden()
                        expect(page.locator('#login-password')).to_have_value('')
                        page.locator('#user-chip').click()
                        page.locator('#logout').click()
                        layout(page, width)
                        assert api.attempts == 2 and api.logins == api.logouts == 1
                        # 세션 만료로 초기 진입해도 같은 화면을 보여야 한다.
                        page.reload()
                        layout(page, width)
                        assert not errors, errors
                        if width == 1300:
                            for boundary in (800, 801):
                                page.set_viewport_size({'width': boundary, 'height': 800})
                                layout(page, boundary)
                        page.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    print(f'PASS: 로그인 분할·이미지·낮/밤 × 1300/390·800/801 경계·짧은 화면·실패·키보드 제출·로그아웃. 캡처: {CAPTURES}')


if __name__ == '__main__':
    run()
