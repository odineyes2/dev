# 이슈 주기 스테퍼(DEV-240-2) — 목업 SC 11개 상황의 칸 상태·메타·바퀴·막대·차례·문장, 병합 버튼 유지, 스크롤, 넘침, reduced-motion을 실제 브라우저와 모의 API로 검사한다.
import re
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
CAPTURES = ROOT / '.ui-captures' / 'dev-240'
OK1 = {'verdict': 'approve', 'plan_version': 1, 'stale': False}
OK2 = {'verdict': 'approve', 'plan_version': 2, 'stale': False}


def plan(v):
    return {'version': v, 'body': '계획', 'author': 'agent:1', 'created_at': '2026-11-02T00:00:00+09:00', 'revision_note': None}


def tasks(ref, *statuses):
    return [{'ref': f'{ref}-{n}', 'status': st, 'sub_number': n} for n, st in enumerate(statuses, 1)]


def issue(ref, status, created, facts=None, **kw):
    f = {'tasks': [], 'failed_tasks': 0, 'running': None, 'loops': 0, 'parent_plan_version': None, 'planned_tasks': None, 'note': ''}
    f.update(facts or {})
    d = {'ref': ref, 'title': ref + ' 제목', 'status': status, 'created_at': f'2026-{created}:00+09:00', 'reporter': 'human:admin',
         'body': '', 'labels': [], 'types': [], 'type_ids': [], 'blocked_by': [], 'events': [], 'attachments': [],
         'processing_agents': [], 'plan': None, 'approval': None, 'lifecycle_facts': f}
    d.update(kw)
    # 상세 응답처럼 빠른 액션 사실(DEV-243)을 붙인다 — 자기·child가 같은 모양
    p = d['plan']
    ctx = {'plan_version': p['version'] if p else None, 'approval': d['approval'], 'revision_requested': False,
           'has_children': bool(f['tasks']), 'has_execution': False, 'has_active_job': bool(d.get('review_running') or d.get('job')),
           'claim_active': False, 'claimed_by': None, 'lease_until': None, 'superseded': False, 'parent': None}
    ctx.update(d.get('action_context') or {})
    d['action_context'] = ctx
    d['children'] = [{**t, 'parent_ref': ref, 'title': t['ref'], 'labels': [], 'blocked_by': [], 'processing_agents': [],
                      'action_context': {**ctx, 'plan_version': None, 'approval': None, 'has_children': False, 'claim_active': False,
                                         'has_active_job': False, 'parent': ctx}} for t in f['tasks']] if not d.get('parent_ref') else []
    return d


EXEC = {'mode': 'execute', 'provider': 'codex', 'ref': 'DEV-220-4', 'sub_number': 4, 'seconds': 760, 'phase': 'implement', 'failed': []}
# (키, 상세 응답, 칸 상태, 칸 메타(정규식), 바퀴, 막대 수, 차례, 문장)
SC = [
    ('new', issue('DEV-219', 'backlog', '11-02T09:20'), 'human todo todo todo todo', ['검토 맡기기 대기', '', '', '', ''], '', 0, '내 차례',
     '계획서가 없어요. Claude나 Codex에게 검토를 맡겨 계획서를 받아 보세요.'),
    ('plan', issue('DEV-216', 'in_progress', '11-02T09:26', {'running': {'mode': 'review', 'provider': 'claude', 'seconds': 252, 'phase': None, 'failed': []}}, review_running=True),
     'done agent todo todo todo', ['11/02 09:26', r'Claude 검토 중 04:1\d', '', '', ''], '', 0, '에이전트 차례', 'Claude가 코드를 읽고 계획서를 쓰는 중이에요. 끝나면 계획서가 올라와요.'),
    ('approve', issue('DEV-225', 'in_progress', '11-02T00:14', {'planned_tasks': 2}, plan=plan(2)), 'done human todo todo todo',
     ['11/02 00:14', 'v2 승인 대기', 'Task 2개 예정', '', ''], '', 0, '내 차례', '계획서 v2이 올라왔어요. 승인·메모 붙여 승인·재검토·거절 중에서 골라 주세요.'),
    ('exec', issue('DEV-220', 'in_progress', '11-01T01:03', {'tasks': tasks('DEV-220', 'done', 'done', 'done', 'in_progress', 'backlog', 'backlog'), 'running': EXEC},
                   plan=plan(1), approval=OK1, review_running=True),
     'done done agent todo todo', ['11/01 01:03', 'v1 승인', r'3/6 · Codex 구현 중 12:4\d', '', ''], '', 6, '에이전트 차례',
     'DEV-220-4를 Codex가 구현하고 있어요. 끝나면 서버가 검사를 다시 돌리고 병합해요.'),
    ('retry', issue('DEV-229', 'in_progress', '11-02T00:54', {'tasks': tasks('DEV-229', 'done', 'in_progress'), 'loops': 1,
                    'running': {'mode': 'execute', 'provider': 'claude', 'ref': 'DEV-229-2', 'sub_number': 2, 'seconds': 185, 'phase': 'rework', 'failed': ['python -m pytest tests/test_qa.py']}},
                    plan=plan(1), approval=OK1, review_running=True),
     'done done agent fail todo', ['11/02 00:54', 'v1 승인', r'2/2 · 재작업 중 03:\d\d', 'pytest 1개 실패', ''], '2바퀴', 2, '에이전트 차례',
     'DEV-229-2 검사에서 tests/test_qa.py가 실패해 Claude가 한 번 더 고치는 중이에요.'),
    ('review', issue('DEV-220', 'in_review', '11-01T01:03', {'tasks': tasks('DEV-220', *['done'] * 6)}, plan=plan(1), approval=OK1),
     'done done done human todo', ['11/01 01:03', 'v1 승인', '6/6 병합됨', '결과 확인 대기', ''], '', 6, '내 차례',
     '모든 Task가 검사를 통과하고 병합됐어요. 운영 화면에서 확인하고 결과를 승인해 주세요.'),
    ('changes', issue('DEV-215', 'changes_requested', '10-31T09:23', {'tasks': tasks('DEV-215', 'changes_requested'), 'loops': 1, 'note': '모바일에서 라벨이 잘려요'},
                      plan=plan(2), approval=OK2),
     'done done fail todo todo', ['10/31 09:23', 'v2 승인', '수정 요청됨', '', ''], '2바퀴', 0, '내 차례',
     '"모바일에서 라벨이 잘려요"라는 수정 요청이 남아 있어요. 에이전트에게 다시 맡겨 주세요.'),
    ('hold', issue('DEV-226', 'on_hold', '11-02T00:24', {'tasks': tasks('DEV-226', 'done')}, plan=plan(1), approval=OK1, execute={'retry_merge_available': True}),
     'done done done hold todo', ['11/02 00:24', 'v1 승인', '1/1', '병합 보류', ''], '', 0, '내 차례', '병합이 보류됐어요. 병합 재시도를 눌러 다시 진행할 수 있어요.'),
    ('done', issue('DEV-234', 'done', '11-02T09:24', {'tasks': tasks('DEV-234', 'done')}, plan=plan(1), approval=OK1,
                   last_done_at='2026-11-03T10:04:00+09:00', completion_seconds=2400),
     'done done done done done', ['11/02 09:24', 'v1 승인', '1/1 병합됨', '검사 통과', '11/03 · 40분'], '', 0, '끝남', '끝난 이슈예요. 발행부터 완료까지 40분 걸렸어요.'),
    ('closed', issue('DEV-212', 'closed', '10-30T23:59', {'note': 'DEV-238 방식으로 대체.'}, plan=plan(1), approval={'verdict': 'reject', 'plan_version': 1, 'stale': False}),
     'done fail skip skip skip', ['10/30 23:59', 'v1 거절', '', '', ''], '', 0, '끝남', '계획서를 거절해 닫았어요. 사유: DEV-238 방식으로 대체.'),
    ('task', issue('DEV-220-4', 'in_progress', '11-01T01:03', {'running': EXEC, 'parent_plan_version': 1}, parent_ref='DEV-220', sub_number=4, execute={}, review_running=True),
     'inherited inherited agent todo todo', ['DEV-220', '부모 v1', r'Codex 구현 중 12:4\d', '', ''], '', 0, '에이전트 차례',
     '부모 계획서 v1의 4번째 Task예요. Codex가 구현하고 있어요.'),
    # DEV-243: 대화형 세션이 잡고 상태만 바꾼 경우(run·job 없음, 계획서 없음) — 검토 맡기기 대기가 아니라 에이전트 차례
    ('claimed', issue('DEV-245', 'in_progress', '11-03T10:00', action_context={'claim_active': True, 'claimed_by': 'agent:7', 'lease_until': '2026-11-04T03:30:00+00:00'}),
     'agent todo todo todo todo', ['agent:7 작업 중', '', '', '', ''], '', 0, '에이전트 차례', 'agent:7이(가) 잡고 작업 중이에요(점유 만료 12:30).'),
]
# 목업 LIST 순서와 실제 목록 응답의 계획 요약을 사용한다.
LIST_KEYS = ['review', 'approve', 'retry', 'plan', 'changes', 'new', 'hold', 'done']
LIST = []
for n, key in enumerate(LIST_KEYS, 1):
    d = dict(next(row[1] for row in SC if row[0] == key))
    d.update(id=n, updated_at=d['created_at'])
    d.pop('execute', None)
    p = d.pop('plan', None)
    d['action_context'] = {'plan_version': p['version'] if p else None}
    if key == 'hold':
        d['merge_state'] = '보류'
    LIST.append(d)


def check_list(browser, port):
    page = browser.new_page(viewport={'width': 1050, 'height': 900}, timezone_id='Asia/Seoul')
    page.clock.install()
    page.route('**/*', handler(LIST[0]))
    page.goto(f'http://127.0.0.1:{port}/#/')
    page.wait_for_selector('#list-body tr.row', state='attached')
    rows = page.locator('#list-body tr.row')
    assert rows.count() == 8
    assert page.locator('tr.mine').count() == 5
    for n, key in enumerate(LIST_KEYS):
        expected = next(row for row in SC if row[0] == key)
        dots = rows.nth(n).locator('.issue-col-progress .lc-dots')
        assert dots.locator('i').evaluate_all('els => els.map(e => e.dataset.s).join(" ")') == expected[2], key
        cur = page.evaluate('(i) => lifecycle(i).cur', LIST[n])
        clock = dots.locator('.lc-clock')
        assert dots.locator('em').count() == 0, key
        tip = rows.nth(n).locator('td.issue-col-progress').get_attribute('title')
        first, msg = tip.split('\n')
        label = page.evaluate('(i) => lifecycle(i).steps[lifecycle(i).cur].label', LIST[n])
        assert first == label or first.startswith(label + ' · '), (key, first)
        meta = first[len(label):].removeprefix(' · ')
        meta = ' '.join(t for t in [meta] + clock.all_text_contents() if t)
        assert re.fullmatch(expected[3][cur], meta), (key, meta)
        assert msg == expected[-1], key
        assert dots.get_attribute('title') == tip, key
        assert rows.nth(n).locator('.lc-mobile .lc-dots').get_attribute('title') == tip, key
        assert rows.nth(n).locator('.lc-mobile em').count() == 0, key
        if key not in ('plan', 'retry'):
            assert clock.count() == 0, key
            continue
        # 시계는 점 다음 줄에 progress-clock 요소째 있어 1초마다 늘어난다
        assert clock.locator('.progress-clock').count() == 1, key
        assert clock.bounding_box()['y'] >= dots.locator('i').first.bounding_box()['y'] + 9, key
        secs = lambda: sum(int(p) * 60 ** k for k, p in enumerate(reversed(clock.text_content().split(':'))))
        # 첫 글자는 그린 때 값이라 1초 주기 틱 위상과 맞춘 뒤 잰다
        page.clock.run_for(1000)
        before = secs()
        page.clock.run_for(1000)
        assert secs() == before + 1, (key, before, secs())
    assert page.locator('[data-turn=mine]').text_content() == '내 차례만 · 5'
    for turn, count in [('mine', 5), ('agent', 2)]:
        page.locator(f'[data-turn={turn}]').click()
        assert page.locator('#list-body tr.row:visible').count() == count
    page.reload()
    page.wait_for_selector('#list-body tr.row', state='attached')
    assert page.locator('[data-turn=agent]').get_attribute('aria-pressed') == 'true'
    assert page.locator('#list-body tr.row:visible').count() == 2
    page.locator('[data-turn=all]').click()
    page.locator('.ops-main').screenshot(path=str(CAPTURES / '10-list.png'))
    for theme in ('light', 'dark'):
        for width in (800, 400):
            page.set_viewport_size({'width': width, 'height': 900})
            page.evaluate('(t) => document.documentElement.dataset.theme = t', theme)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, width)
            if width == 400:
                assert not page.locator('th.issue-col-progress').is_visible()
                assert page.locator('.lc-mobile:visible').count() == 8
            page.locator('.ops-main').screenshot(path=str(CAPTURES / f'{theme}-{width}-list.png'))
        page.set_viewport_size({'width': 800, 'height': 900})
        page.goto(f'http://127.0.0.1:{port}/#/board')
        page.wait_for_selector('.card .meta .lc-dots')
        assert page.locator('.card .meta .lc-dots').count() == 8
        assert page.locator('.card .meta .lc-dots em').count() == 8
        page.locator('.kanban').screenshot(path=str(CAPTURES / f'{theme}-800-board.png'))
        page.goto(f'http://127.0.0.1:{port}/#/')
        page.wait_for_selector('#list-body tr.row', state='attached')
    for key in ('retry', 'hold'):
        detail = next(row[1] for row in SC if row[0] == key)
        detail_page = open_issue(browser, port, detail)
        assert detail_page.locator('.lc-step').evaluate_all('els => els.map(e => e.dataset.s).join(" ")') == page.evaluate('(i) => lifecycle(i).steps.map(s => s.state).join(" ")', next(i for i in LIST if i['ref'] == detail['ref']))
        detail_page.close()
    page.close()
    # 실제 하위 행으로 접기 버튼과 접힘 기억을 확인한다.
    child = issue('DEV-220-1', 'done', '11-01T01:03', parent_ref='DEV-220', id=99, updated_at=LIST[0]['updated_at'])
    LIST.append(child)
    try:
        page = browser.new_page(viewport={'width': 1050, 'height': 900}, timezone_id='Asia/Seoul')
        page.route('**/*', handler(LIST[0]))
        page.goto(f'http://127.0.0.1:{port}/#/')
        parent = page.locator('#list-body tr[data-ref="DEV-220"]')
        toggle = parent.locator('.tog')
        toggle.wait_for()
        assert parent.locator('.kids').count() == 0
        assert 'Tasks ' not in parent.text_content()
        child_row = page.locator('#list-body tr[data-ref="DEV-220-1"]')
        assert child_row.is_visible()
        toggle.click()
        assert not child_row.is_visible()
        page.reload()
        page.wait_for_selector('#list-body tr.row', state='attached')
        assert toggle.get_attribute('aria-expanded') == 'false'
        assert not child_row.is_visible()
        toggle.click()
        assert child_row.is_visible()
        page.close()
    finally:
        LIST.pop()


SHOTS = {'approve': '02-approve', 'exec': '03-exec', 'retry': '04-retry', 'review': '05-review', 'closed': '06-closed', 'task': '07-task'}


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def handler(detail):
    def handle(route):
        url = route.request.url.split('?')[0]
        if not url.startswith('http://127.0.0.1:'):
            return route.abort()
        if '/api/' not in url:
            return route.continue_()
        path = url.split('/api/', 1)[1]
        if path == 'auth/me':
            return route.fulfill(json={'actor': {'kind': 'human', 'name': 'admin'}, 'nightshift_url': '#'})
        if path == 'issues':
            return route.fulfill(json={'issues': LIST, 'has_more': False})
        if path == f'issues/{detail["ref"]}':
            return route.fulfill(json=detail)
        if path.endswith('/runs'):
            return route.fulfill(json={'runs': []})
        if path == 'issue-types':
            return route.fulfill(json={'types': []})
        if path in ('agents', 'projects'):
            return route.fulfill(json={path: []})
        if path == 'settings/auto':
            return route.fulfill(json={'auto_review': False, 'auto_execute': False})
        if path == 'settings/tokens':
            return route.fulfill(json={'unit': 'auto'})
        return route.fulfill(status=404, json={'detail': 'mock'})
    return handle


def open_issue(browser, port, detail, width=1050, theme='light', motion='reduce'):
    page = browser.new_page(viewport={'width': width, 'height': 800}, reduced_motion=motion, timezone_id='Asia/Seoul')
    page.route('**/*', handler(detail))
    page.goto(f'http://127.0.0.1:{port}/#/issue/{detail["ref"]}')
    page.wait_for_selector('.lc .lc-next')
    page.evaluate('(t) => document.documentElement.dataset.theme = t', theme)
    return page


def check(page, key, states, metas, loop, bars, turn, msg):
    got = page.eval_on_selector_all('.lc-step', 'els => els.map(e => e.dataset.s).join(" ")')
    assert got == states, (key, got)
    texts = page.eval_on_selector_all('.lc-meta', 'els => els.map(e => e.textContent.replace(/\\s+/g, " ").trim())')
    for want, text in zip(metas, texts):
        assert re.fullmatch(want, text), (key, want, text)
    cur = page.eval_on_selector_all('.lc-step', 'els => els.findIndex(e => e.hasAttribute("data-cur"))')
    assert states.split()[cur] not in ('todo', 'skip', 'inherited') or key == 'closed', (key, cur)
    assert page.locator('.lc-loop').all_text_contents() == ([loop] if loop else []), key
    assert page.locator('.lc-segs i').count() == bars, key
    assert page.locator('.lc-turn').text_content() == turn, key
    assert page.locator('.lc-msg').text_content() == msg, (key, page.locator('.lc-msg').text_content())


def run():
    CAPTURES.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(ROOT / 'static')))
    port = server.server_port
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                check_list(browser, port)
                by_key = {}
                for key, detail, *expect in SC:
                    by_key[key] = detail
                    page = open_issue(browser, port, detail)
                    check(page, key, *expect)
                    if key in SHOTS:
                        page.locator('.lc').screenshot(path=str(CAPTURES / f'{SHOTS[key]}.png'))
                    page.close()
                # 점유 중이면 진행 줄 go도 검토 버튼도 없다. 계획서 v2 승인 대기면 계획서 보기만, 검토 버튼은 없다(NS-116)
                page = open_issue(browser, port, by_key['claimed'])
                assert page.locator('.lc-go').count() == 0 and page.locator('#ask-review').count() == 0
                page.close()
                page = open_issue(browser, port, by_key['approve'])
                assert page.locator('.lc-go').text_content() == '계획서 보기' and page.locator('#ask-review').count() == 0
                page.close()
                page = open_issue(browser, port, by_key['new'])
                assert page.locator('.lc-go').text_content() == '검토 맡기기' and page.locator('#ask-review').count() == 1
                page.close()
                page = open_issue(browser, port, by_key['changes'])
                assert page.locator('.lc-go').text_content() == '실행 맡기기' and page.locator('.run-task').count() == 2
                page.close()
                # 병합 보류: 기존 #retry-merge가 다음 할 일 줄 안에 그대로 있다
                page = open_issue(browser, port, by_key['hold'])
                assert page.locator('.lc-next #retry-merge').count() == 1
                assert page.locator('#stage').count() == 1
                page.close()
                # 계획 칸·go 버튼을 누르면 Plan 패널이 뷰포트 위쪽 1/3 안에 온다
                for sel in ('.lc-step:nth-child(2) .lc-jump', '.lc-go'):
                    page = open_issue(browser, port, by_key['approve'])
                    page.locator(sel).click()
                    page.wait_for_timeout(200)
                    top = page.eval_on_selector('#panel-plan', 'e => e.getBoundingClientRect().top')
                    assert 0 <= top <= 800 / 3, (sel, top)
                    page.close()
                # reduced-motion이면 맥박을 끈다(켜져 있으면 돈다)
                for motion, want in (('reduce', 'none'), ('no-preference', 'lc-pulse')):
                    page = open_issue(browser, port, by_key['exec'], motion=motion)
                    name = page.eval_on_selector('.lc-step[data-s=agent] .lc-node', 'e => getComputedStyle(e).animationName')
                    assert name == want, (motion, name)
                    page.close()
                for key, name, width, theme in (('exec', '08-dark', 1050, 'dark'), ('exec', '09-phone', 400, 'light')):
                    page = open_issue(browser, port, by_key[key], width, theme)
                    page.locator('.lc').screenshot(path=str(CAPTURES / f'{name}.png'))
                    page.close()
                # 800·400 × 낮·밤: 가로 넘침 없음, 400에서는 현재 칸 메타만·막대 숨김
                for theme in ('light', 'dark'):
                    for width in (800, 400):
                        for key, detail, *_ in SC:
                            page = open_issue(browser, port, detail, width, theme)
                            over = page.evaluate('() => document.documentElement.scrollWidth - innerWidth')
                            assert over <= 0, (theme, width, key, over)
                            if width == 400:
                                shown = page.eval_on_selector_all('.lc-step:not([data-cur]) .lc-meta, .lc-segs', 'els => els.filter(e => e.offsetParent).length')
                                assert shown == 0, (key, shown)
                            page.locator('.lc').screenshot(path=str(CAPTURES / f'{theme}-{width}-{key}.png'))
                            page.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
    print(f'PASS: 목업 11개 상황 + 점유 상황 스테퍼·다음 할 일 줄, 빠른 액션 버튼, 병합 버튼, 스크롤, reduced-motion, 800·400 × 낮·밤. 캡처: {CAPTURES}')


if __name__ == '__main__':
    run()
