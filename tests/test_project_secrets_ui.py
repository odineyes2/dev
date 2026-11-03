# 프로젝트 Modify 화면 비밀값 패널(DEV-239-2) — 표시·저장 body·결과 문구·원값 미노출·낮밤×데스크톱모바일 배치를 실제 브라우저와 모의 API로 검사한다.
import json
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
CAPTURES = ROOT / '.ui-captures' / 'dev-239'
TYPED = 'sample-typed-value-0000'  # 사람이 입력하는 모의 값 — 저장 뒤 DOM에 남으면 안 된다
STORED = 'sample-stored-value-1a2b'  # 서버에 있다고 가정한 값 — 응답에 끝 4자만 온다
PROJECT = {'key': 'CH', 'name': 'charchat', 'local_path': 'C:/Projects/charchat', 'service_app': 'charchat',
           'archived': False, 'issue_count': 0}


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class Api:
    """운영 API 대신 응답하고 비밀값 저장 요청을 기록한다."""

    def __init__(self):
        self.puts, self.restart = [], 'complete'
        self.keys = [{'key': 'LLM_API_KEY', 'set': True, 'last4': STORED[-4:], 'dev_available': False},
                     {'key': 'OTHER_TOKEN', 'set': False, 'last4': '', 'dev_available': True}]

    def handle(self, route):
        req = route.request
        url = req.url.split('?')[0]
        if not url.startswith('http://127.0.0.1:'):
            return route.abort()
        if '/api/' not in url:
            return route.continue_()
        path = url.split('/api/', 1)[1]
        if path == 'auth/me':
            return route.fulfill(json={'actor': {'kind': 'human', 'name': 'admin'}, 'nightshift_url': '#'})
        if path == 'projects':
            return route.fulfill(json={'projects': [PROJECT]})
        if path == 'agents':
            return route.fulfill(json={'agents': []})
        if path == 'settings/tokens':
            return route.fulfill(json={'unit': 'auto'})
        if path == 'projects/CH/restart':
            return route.fulfill(json={'available': True, 'deploy_available': False, 'status': ''})
        if path == 'projects/CH/secrets' and req.method == 'GET':
            return route.fulfill(json={'project': 'CH', 'keys': self.keys})
        if path == 'projects/CH/secrets' and req.method == 'PUT':
            body = json.loads(req.post_data)
            self.puts.append(body)
            for item in self.keys:
                change = body.get(item['key'])
                if change:
                    item['set'] = not change.get('clear')
                    item['last4'] = '' if change.get('clear') else 'abcd'
            restart = {'status': self.restart, 'error': '모의 재시작 오류예요.' if self.restart == 'failed' else ''}
            return route.fulfill(json={'project': 'CH', 'keys': self.keys, 'restart': restart})
        return route.fulfill(status=404, json={'detail': 'mock'})


def behaviour(page, api):
    panel = page.locator('.project-secrets')
    expect(panel).to_be_visible()
    rows = panel.locator('.secret-row')
    expect(rows).to_have_count(2)
    expect(rows.nth(0)).to_contain_text('LLM_API_KEY')
    expect(rows.nth(0)).to_contain_text('설정됨 ··1a2b')
    expect(rows.nth(1)).to_contain_text('비어 있어요')
    expect(rows.nth(0).locator('[data-secret-dev]')).to_have_count(0)
    expect(rows.nth(1).locator('[data-secret-dev]')).to_have_count(1)
    assert rows.nth(0).locator('input[type=password]').get_attribute('autocomplete') == 'off'
    # 빈 칸은 바꾸지 않는다 — 입력한 키만 body에 들어간다
    page.locator('[data-env-key="OTHER_TOKEN"]').fill(TYPED)
    page.locator('#secrets-save').click()
    expect(page.locator('#secrets-result')).to_contain_text('반영됨')
    assert api.puts[-1] == {'OTHER_TOKEN': {'value': TYPED}}, api.puts
    expect(page.locator('[data-env-key="OTHER_TOKEN"]')).to_have_value('')
    assert TYPED not in page.content() and STORED not in page.content(), '원값이 페이지에 남으면 안 된다'
    # 재시작 실패
    api.restart = 'failed'
    page.locator('[data-secret-clear="LLM_API_KEY"]').check()
    page.locator('#secrets-save').click()
    expect(page.locator('#secrets-result')).to_contain_text('재시작 실패')
    assert api.puts[-1] == {'LLM_API_KEY': {'clear': True}}, api.puts
    expect(rows.nth(0)).to_contain_text('비어 있어요')
    # dev 값 그대로 쓰기
    api.restart = 'skipped'
    page.locator('[data-secret-dev="OTHER_TOKEN"]').click()
    expect(page.locator('#secrets-result')).to_contain_text('재시작할 앱이 없어요')
    assert api.puts[-1] == {'OTHER_TOKEN': {'from_dev': True}}, api.puts
    # 아무것도 안 넣고 저장하면 요청하지 않는다
    count = len(api.puts)
    page.locator('#secrets-save').click()
    expect(page.locator('#secrets-result')).to_contain_text('바꿀 값을 입력해 주세요')
    assert len(api.puts) == count
    assert TYPED not in page.content() and STORED not in page.content()


def layout(page, path):
    panel = page.locator('.project-secrets')
    expect(panel.locator('.secret-row')).to_have_count(2)
    page.locator('[data-env-key="LLM_API_KEY"]').fill(TYPED)
    panel.scroll_into_view_if_needed()
    assert panel.evaluate('''p => {
      const r = p.getBoundingClientRect();
      const inside = [...p.querySelectorAll('.secret-row *')].every(e => { const b = e.getBoundingClientRect(); return b.right <= r.right + 1 && b.left >= r.left - 1; });
      return r.left >= 0 && r.right <= innerWidth && p.scrollWidth <= p.clientWidth && inside
        && document.documentElement.scrollWidth <= innerWidth;
    }'''), '비밀값 패널이 가로로 넘치면 안 된다'
    panel.screenshot(path=str(path))


def run():
    CAPTURES.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(ROOT / 'static')))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                def new_page(width, theme='light'):
                    api = Api()
                    page = browser.new_page(viewport={'width': width, 'height': 800}, reduced_motion='reduce')
                    page.route('**/*', api.handle)
                    page.goto(f'http://127.0.0.1:{server.server_port}/#/projects?project=CH&tab=manage')
                    page.wait_for_selector('.project-secrets')
                    page.evaluate('(t) => document.documentElement.dataset.theme = t', theme)
                    return page, api
                page, api = new_page(1300)
                behaviour(page, api)
                page.close()
                for theme in ('light', 'dark'):
                    for width in (1300, 390):
                        page, _ = new_page(width, theme)
                        layout(page, CAPTURES / f'{theme}-{width}-secrets.png')
                        page.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    print(f'PASS: 비밀값 표시·저장 body·지우기·dev 값·결과 문구·원값 미노출, 낮·밤 × 1300·390. 캡처: {CAPTURES}')


if __name__ == '__main__':
    run()
