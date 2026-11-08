# 이미지 별칭의 선택 순서·재시도·요청 매핑·상세 표시와 낮밤 배치를 모의 API 브라우저로 검사한다.
from functools import partial
from http.server import ThreadingHTTPServer
from threading import Thread
from pathlib import Path
from urllib.parse import urlsplit
import base64
import time
from playwright.sync_api import sync_playwright, expect
from test_family_menu import QuietHandler
from test_action_cancel import ITEM

ROOT = Path(__file__).resolve().parents[1]
CAPTURES = ROOT / '.ui-captures' / 'dev-253-2'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aFOsAAAAASUVORK5CYII=')


class Api:
    def __init__(self):
        self.pending, self.posts, self.count = [], [], 0

    def handle(self, route):
        req = route.request
        url = urlsplit(req.url)
        if url.hostname != '127.0.0.1':
            return route.abort()
        path = url.path
        if not path.startswith('/api/'):
            return route.continue_()
        if path == '/api/attachments' and req.method == 'POST':
            self.count += 1
            self.pending.append((route, str(self.count)))
            return
        if req.method == 'DELETE':
            return route.fulfill(json={'ok': True})
        if path == '/api/issues' and req.method == 'POST':
            self.posts.append(req.post_data_json)
            return route.fulfill(status=500, json={'detail': '모의 발행 실패'})
        if path.endswith('/content'):
            return route.fulfill(body=PNG, content_type='image/png')
        images = [dict(id=x, kind='image', name='same.png', size=100, alias=alias)
                  for x, alias in [('a', '이미지2'), ('b', '이미지7'), ('old', None)]]
        detail = {**ITEM, 'ref': 'DEV-253', 'parent_ref': None, 'parent_id': None,
                  'children': [], 'attachments': images, 'events': [dict(kind=kind, actor='human:admin', body='이미지7 확인',
                  created_at=ITEM['created_at'], data={'attachment_ids': ['b', 'a']}) for kind in ('comment', 'status')]}
        data = {'/api/auth/me': {'actor': {'kind': 'human', 'name': 'admin'}},
                '/api/projects': {'projects': [{'key': 'DEV', 'name': 'dev', 'archived': False}]},
                '/api/agents': {'agents': []}, '/api/issue-types': {'types': []},
                '/api/issue-summary': {'summary': {'created_issue_count': 0, 'completed_issue_count': 0, 'agent_seconds': 0, 'total_tokens': 0}},
                '/api/settings/tokens': {'unit': 'auto'}, '/api/settings/auto': {},
                '/api/issues': {'issues': [], 'has_more': False}, '/api/jobs': {'jobs': []},
                '/api/issues/DEV-253': detail, '/api/issues/DEV-253/runs': {'runs': []}, '/api/projects/DEV/dep-candidates': {'issues': []}}
        return route.fulfill(json=data.get(path, {}))

    def wait_pending(self, page, count=1):
        deadline = time.monotonic() + 5
        while len(self.pending) < count:
            assert time.monotonic() < deadline, "업로드 요청 시간 초과"
            page.wait_for_timeout(20)

    def finish(self, page, count, fail=False):
        expect(page.locator('#n-attachments .attachment-heading, #cmp-files .cmp-chip')).to_have_count(count)
        # 브라우저 이벤트를 처리한 뒤 마지막 업로드부터 완료시킨다.
        self.wait_pending(page, count)
        batch, self.pending = self.pending, []
        for i, (route, ident) in reversed(list(enumerate(batch))):
            route.fulfill(status=500 if fail and i == count - 1 else 200,
                          json={'detail': '모의 업로드 실패'} if fail and i == count - 1 else {'id': ident})
            page.wait_for_timeout(20)
        return [ident for _, ident in batch]


def files():
    return [dict(name='same.png', mimeType='', buffer=PNG),
            dict(name='note.txt', mimeType='text/plain', buffer=b'document'),
            dict(name='same.png', mimeType='image/png', buffer=PNG + b'other')]


def capture(page, name, selectors):
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), name
    for selector in selectors:
        loc = page.locator(selector)
        for el in loc.all():
            el.scroll_into_view_if_needed()
            assert el.evaluate('''e => {
              const r=e.getBoundingClientRect();
              const boxes=[...e.children].map(c=>c.getBoundingClientRect()).filter(b=>b.width && b.height);
              const overlap=boxes.some((a,i)=>boxes.slice(i+1).some(b=>
                Math.min(a.right,b.right)-Math.max(a.left,b.left)>1 &&
                Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>1));
              return !overlap && r.left>=0 && r.right<=innerWidth+1 && e.scrollWidth<=e.clientWidth+1;
            }'''), (name, selector)
    page.evaluate('scrollTo(0,0)')
    page.screenshot(path=str(CAPTURES / f'{name}.png'), full_page=True)


def behaviour(page, api, theme, width):
    page.goto(page.url.split('#')[0] + '#/new')
    expect(page.locator('#new-form')).to_be_visible()
    page.locator('#n-title').fill('이미지 별칭')
    page.locator('#n-files').set_input_files(files())
    ids = api.finish(page, 3, fail=True)
    expect(page.locator('#n-attachments')).to_contain_text('이미지1')
    expect(page.locator('#n-attachments')).to_contain_text('이미지2')
    page.locator('[data-retry="2"]').click()
    api.wait_pending(page)
    route, retry = api.pending.pop()
    route.fulfill(json={'id': retry})
    expect(page.locator('[data-retry]')).to_have_count(0)
    page.locator('[data-insert="2"]').click()
    expect(page.locator('#n-body')).to_have_value('이미지2')
    page.locator('[data-preview="2"]').click()
    expect(page.locator('.attachment-lightbox')).to_be_visible()
    page.keyboard.press('Escape')
    page.locator('[data-remove="0"]').click()
    expect(page.locator('#n-attachments .attachment-heading')).to_have_count(2)
    page.locator('#n-files').set_input_files(dict(name='third.webp', mimeType='', buffer=PNG))
    api.wait_pending(page)
    route, third = api.pending.pop(); route.fulfill(json={'id': third})
    expect(page.locator('#n-attachments')).to_contain_text('이미지3')
    expect(page.locator('#new-form [type=submit]')).to_be_enabled()
    capture(page, f'{theme}-{width}-new', ['#n-attachments .attachment-heading'])
    page.locator('#new-form [type=submit]').click()
    expect(page.locator('#attachment-status')).to_contain_text('모의 발행 실패')
    payload = api.posts[-1]
    assert payload['body'] == '이미지2'
    assert payload['attachment_ids'] == [ids[1], retry, third]
    assert payload['attachment_image_numbers'] == {retry: 2, third: 3}

    page.goto(page.url.split('#')[0] + '#/')
    page.wait_for_timeout(100)
    page.locator('body').click(position={'x': 1, 'y': 200})
    page.keyboard.press('`')
    expect(page.locator('#composer')).to_be_visible()
    page.locator('#cmp-body').fill('이미지2를 확인해 주세요')
    page.locator('#cmp-file').set_input_files(files())
    ids = api.finish(page, 3, fail=True)
    page.locator('[data-cmp-retry]').click()
    api.wait_pending(page)
    route, retry = api.pending.pop(); route.fulfill(json={'id': retry})
    expect(page.locator('[data-cmp-retry]')).to_have_count(0)
    page.locator('[data-cmp-remove="0"]').click()
    page.locator('#cmp-file').set_input_files(dict(name='third.webp', mimeType='', buffer=PNG))
    api.wait_pending(page)
    route, third = api.pending.pop(); route.fulfill(json={'id': third})
    expect(page.locator('#cmp-files')).to_contain_text('이미지3')
    page.locator('#cmp-body').press('Escape')
    page.locator('body').click(position={'x': 1, 'y': 200}); page.keyboard.press('`')
    expect(page.locator('#cmp-files')).to_contain_text('이미지2')
    expect(page.locator('#cmp-send')).to_be_enabled()
    capture(page, f'{theme}-{width}-composer', ['.cmp-chip'])
    page.locator('#cmp-send').click()
    expect(page.locator('#cmp-error')).to_contain_text('모의 발행 실패')
    payload = api.posts[-1]
    assert payload['body'] == '이미지2를 확인해 주세요'
    assert payload['attachment_ids'] == [ids[1], retry, third]
    assert payload['attachment_image_numbers'] == {retry: 2, third: 3}

    page.goto(page.url.split('#')[0] + '#/issue/DEV-253')
    expect(page.locator('.attachments')).to_be_visible()
    assert page.locator('.attachments img').evaluate_all('(els)=>els.map(e=>e.alt)') == ['이미지2 · same.png', '이미지7 · same.png', 'same.png']
    for links in page.locator('.event-files').all():
        expect(links.locator('a')).to_have_text(['이미지7 · same.png', '이미지2 · same.png'])
        assert links.locator('a').first.get_attribute('href').endswith('/b/content')
    expect(page.locator('.event-files')).to_have_count(2)
    capture(page, f'{theme}-{width}-detail', ['.attachment-heading', '.event-files'])


def run():
    CAPTURES.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(ROOT / 'static')))
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                for theme in ('light', 'dark'):
                    for width in (1300, 390):
                        page = browser.new_page(viewport={'width': width, 'height': 900}, reduced_motion='reduce')
                        api = Api(); errors = []
                        page.on('pageerror', lambda e: errors.append(str(e)))
                        page.route('**/*', api.handle)
                        page.add_init_script(f"localStorage.setItem('dev.theme', '{theme}')")
                        page.goto(f'http://127.0.0.1:{server.server_port}/#/new')
                        behaviour(page, api, theme, width)
                        assert not errors, errors
                        page.close()
            finally:
                browser.close()
    finally:
        server.shutdown(); server.server_close(); thread.join()
    print(f'PASS: 이미지 별칭 선택 순서·역순 완료·제거·재시도·본문·ID 매핑·상세/댓글, 낮밤 1300/390. {CAPTURES}')


if __name__ == '__main__':
    run()
