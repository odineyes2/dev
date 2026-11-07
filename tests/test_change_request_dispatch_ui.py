# 공통 Action의 취소 시 화면 보존·버튼 복구와 요청 성공·오류 시 갱신을 모의 API 브라우저로 검사한다.
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
CAPTURES = ROOT / 'tests' / '__pycache__' / 'change-dispatch'
APPROVAL = {'verdict': 'approve', 'plan_version': 1, 'stale': False}
ITEM = {'id': 1, 'ref': 'DEV-248-1', 'title': '실행 취소 화면 유지', 'status': 'changes_requested',
        'parent_ref': None, 'parent_id': None, 'labels': [], 'types': [], 'type_ids': [],
        'created_at': '2026-11-06T00:00:00+00:00', 'updated_at': '2026-11-06T00:00:00+00:00',
        'processing_agents': [], 'action_context': {'parent': None, 'plan_version':1, 'approval':APPROVAL, 'can_dispatch_change_request':True, 'change_request_event_id':42}}
from test_lifecycle_ui import issue, plan
DETAIL = issue('DEV-248-1', 'changes_requested', '11-06T00:00', plan=plan(1), approval=APPROVAL, action_context=ITEM['action_context'])
DETAIL.update(ITEM)
DETAIL.update(body='', reporter='human:admin', blocked_by=[], events=[], attachments=[], children=[], plan=plan(1), approval=APPROVAL)



class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def scenario(browser, port, mode, provider, theme, width):
    page = browser.new_page(viewport={'width': width, 'height': 800}, reduced_motion='reduce')
    # 폴링 시간을 고정해 클릭에서 발생한 조회만 센다.
    page.clock.install()
    calls = []
    state = {'error': False, 'detail_error': False}

    def handle(route):
        req = route.request
        path = urlsplit(req.url).path
        if not req.url.startswith(f'http://127.0.0.1:{port}/'):
            return route.abort()
        if not path.startswith('/api/'):
            return route.continue_()
        calls.append((req.method, path, req.post_data_json if req.method == 'POST' else None))
        if req.method == 'POST':
            return route.fulfill(status=500 if state['error'] else 200,
                                 json={'detail': 'mock error'} if state['error'] else {})
        if path == '/api/issues/DEV-248-1' and state['detail_error']:
            return route.fulfill(status=500, json={'detail': 'mock detail error'})
        data = {'/api/auth/me': {'actor': {'kind': 'human', 'name': 'admin'}, 'nightshift_url': '#'},
                '/api/projects': {'projects': []}, '/api/agents': {'agents': []},
                '/api/issue-types': {'types': []}, '/api/settings/tokens': {'unit': 'auto'},
                '/api/settings/auto': {'auto_review': True, 'auto_execute': False},
                '/api/issues': {'issues': [ITEM], 'has_more': False},
                '/api/issues/DEV-248-1': DETAIL, '/api/issues/DEV-248-1/runs': {'runs':[]}, '/api/jobs': {'jobs': []}}
        return route.fulfill(json=data.get(path, {}))

    page.route('**/*', handle)
    page.goto(f'http://127.0.0.1:{port}/#/{mode}')
    query_path = '/api/issues/DEV-248-1' if mode.startswith('issue/') else '/api/issues'
    buttons = page.locator('[data-action="change-review"][data-ref="DEV-248-1"]')
    expect(buttons).to_have_count(2)
    page.evaluate('(t) => document.documentElement.dataset.theme=t', theme)
    button = buttons.filter(has=page.locator('use'))  # 아래에서 provider로 정확히 고른다.
    button = page.locator(f'[data-action="change-review"][data-provider="{provider}"]')
    page.add_style_tag(content='html,body,* { scroll-behavior: auto !important; }')
    page.evaluate('''() => {
      window.savedButton=document.querySelector('[data-action="change-review"]');
      window.savedParent=savedButton.parentElement;
      window.savedHTML=savedParent.innerHTML;
      window.savedScroll=[scrollX,scrollY,document.querySelector('.kanban')?.scrollLeft || 0];
    }''')
    baseline = len([c for c in calls if c[:2] == ('GET', query_path)])
    for cancel in ('cancel', 'escape'):
        button.click()
        dialog = page.locator('.confirm-dialog')
        expect(dialog).to_be_visible()
        page.evaluate('window.savedScroll=[scrollX,scrollY,document.querySelector(".kanban")?.scrollLeft || 0]')
        expect(buttons.nth(0)).to_be_disabled()
        expect(buttons.nth(1)).to_be_disabled()
        assert dialog.evaluate('d => {const r=d.getBoundingClientRect();return r.left>=0 && r.right<=innerWidth && r.top>=0 && r.bottom<=innerHeight && d.scrollWidth<=d.clientWidth}')
        if cancel == 'cancel':
            page.screenshot(path=str(CAPTURES / f'{mode.replace(chr(47), chr(45)) or "list"}-{provider}-{theme}-{width}-before.png'))
            dialog.locator('.confirm-cancel').click()
        else:
            page.keyboard.press('Escape')
        expect(dialog).to_have_count(0)
        for b in buttons.all():
            expect(b).to_be_enabled()
            expect(b).to_have_attribute('aria-disabled', 'false')
        assert page.evaluate('pendingActions.size') == 0
        assert page.evaluate('''() => savedButton.isConnected && savedButton.parentElement===savedParent
          && savedParent.innerHTML===savedHTML && JSON.stringify(savedScroll)===JSON.stringify([scrollX,scrollY,document.querySelector('.kanban')?.scrollLeft || 0])'''), (mode, provider, theme, width, page.evaluate('[savedScroll,[scrollX,scrollY,document.querySelector(".kanban")?.scrollLeft || 0],savedParent.innerHTML===savedHTML]'))
        assert len([c for c in calls if c[:2] == ('GET', query_path)]) >= baseline
        assert not [c for c in calls if c[0] == 'POST']
    page.screenshot(path=str(CAPTURES / f'{mode.replace(chr(47), chr(45)) or "list"}-{provider}-{theme}-{width}-after.png'))
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    # 확인 성공·POST 오류·상세 GET 오류는 모두 최신 화면으로 갱신한다.
    for result in (('success', 'post-error') if mode.startswith('issue/') else ('success', 'post-error', 'detail-error')):
        state.update(error=result == 'post-error', detail_error=result == 'detail-error')
        before_posts = len([c for c in calls if c[0] == 'POST'])
        button.click()
        if result != 'detail-error':
            expect(page.locator('.confirm-dialog')).to_be_visible()
            buttons.nth(1).evaluate("b => b.dispatchEvent(new MouseEvent('click', {bubbles:true}))")
            page.locator('.confirm-ok').click()
        page.wait_for_function('window.savedButton !== document.querySelector("[data-action=change-review]")')
        expect(buttons).to_have_count(2)
        for b in buttons.all():
            expect(b).to_be_enabled()
        posts = [c for c in calls if c[0] == 'POST']
        assert len(posts) == before_posts + (result != 'detail-error')
        if result != 'detail-error':
            assert posts[-1][1] == '/api/issues/DEV-248-1/review'
            assert posts[-1][2] == {'provider':provider, 'plan_version':1, 'change_request_event_id':42}
        baseline += 1
        assert len([c for c in calls if c[:2] == ('GET', query_path)]) >= baseline
        assert page.evaluate('pendingActions.size') == 0
        page.evaluate('window.savedButton=document.querySelector("[data-action=change-review]")')
    page.close()


def run():
    CAPTURES.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(ROOT / 'static')))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                from test_action_cancel import scenario as task_scenario, CAPTURES as TASK_CAPTURES
                TASK_CAPTURES.mkdir(parents=True, exist_ok=True)
                for provider in ('claude', 'codex'):
                    task_scenario(browser, server.server_port, '', provider, 'light', 390)
                for theme in ('light', 'dark'):
                    for width in (1300, 390):
                        for mode in ('', 'board', 'issue/DEV-248-1'):
                            for provider in ('claude', 'codex'):
                                scenario(browser, server.server_port, mode, provider, theme, width)
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    print(f'PASS: 목록·보드 × Claude·Codex × 낮·밤 × 1300·390 취소/확인/오류. 캡처: {CAPTURES}')


if __name__ == '__main__':
    run()
