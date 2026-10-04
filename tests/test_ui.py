"""화면 — 임시 데이터 폴더로 dev 서버와 가짜 nightshift를 띄우고 브라우저로 돈다.
로그인 폼 → 프로젝트 만들기 → 새 이슈 → 상세(계획서·댓글·상태) → 목록·칸반(끌어서 상태) → 에이전트 키 발급 → 모바일 폭."""
import os, socket, subprocess, sys, tempfile, time
from pathlib import Path
import json
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
FAKE_NS = r'''
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
app = FastAPI()
@app.get("/api/auth/me")
def me(request: Request):
    users = {"adm": {"id": 1, "username": "admin", "role": "admin"},
             "mem": {"id": 2, "username": "admin", "role": "user"}}
    return {"user": users.get(request.cookies.get("ns_session"))}
@app.post("/api/auth/login")
async def login(request: Request):
    b = await request.json()
    if b.get("password") != "pw":
        return JSONResponse({"detail": "아이디 또는 비밀번호가 올바르지 않아요."}, 401)
    r = JSONResponse({"user": {"id": 1, "username": "admin", "role": "admin"}})
    r.set_cookie("ns_session", "adm", path="/")
    return r
@app.post("/api/auth/logout")
def logout():
    r = JSONResponse({}); r.delete_cookie("ns_session", path="/"); return r
'''


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def wait_port(port):
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close(); return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"port {port} not up")


def check_quick_actions(page, shots):
    """빠른 동작은 유료 요청을 전부 가로채고 실제 API의 조회 형태로 검사한다."""
    from copy import deepcopy
    template = page.request.get(BASE + '/api/issues/NS-1').json()
    def make(ref, task=False):
        return {**deepcopy(template), 'ref':ref, 'title':'빠른 동작 검사', 'status':'backlog',
                'parent_id':1 if task else None, 'parent_ref':'QA-1' if task else None,
                'plan':{'version':1, 'body':'## 정해야 할 것\n- 모바일도 확인\n## 구현\n안전한 <script>Plan</script>', 'author':'human:admin'},
                'approval':None, 'review_running':False, 'review_busy':False, 'job':None,
                'execute':{'blocked':None, 'branch':None, 'budget_usd':2, 'timeout_sec':1800} if task else None,
                'children':[], 'events':[], 'labels':[], 'claimed_by':None}
    state = {'QA-1':make('QA-1'), 'QA-1-1':make('QA-1-1', True)}
    gets, calls, pending = [], [], []
    hold_get = []
    def listing(route):
        route.fulfill(json={'issues':list(state.values()), 'has_more':False})
    def detail(route):
        ref = route.request.url.rsplit('/', 1)[-1]
        gets.append(ref)
        if hold_get:
            hold_get.pop(); pending.append(route)
        else:
            route.fulfill(json=state[ref])
    def action(route):
        calls.append((route.request.url, route.request.post_data_json))
        pending.append(route)
    def runs(route):
        ref = route.request.url.split('/')[-2]
        route.fulfill(json={'runs':[{'status':'running','mode':'execute' if state[ref]['execute'] else 'review','provider':'codex'}]})
    page.route('**/api/issues?*', listing)
    page.route('**/api/issues/QA-*', detail)
    page.route('**/api/issues/QA-*/runs', runs)
    for mode in ('review', 'execute', 'decision'):
        page.route('**/api/issues/QA-*/' + mode, action)
    def settle(status=200, data=None):
        page.wait_for_function("document.querySelector('.quick-body[aria-busy=true]') !== null")
        assert len(pending) == 1
        pending.pop().fulfill(status=status, json=data or {'queued':True, 'position':2})
        page.wait_for_function("document.querySelector('.quick-body[aria-busy=true]') === null")
    page.clock.install()
    for name in ('', 'board'):
        state['QA-1'] = make('QA-1'); state['QA-1-1'] = make('QA-1-1', True)
        page.set_viewport_size({'width':1300,'height':850})
        page.goto(BASE + '/#/' + name); page.reload()
        panel = page.locator('[data-quick-ref="QA-1"]')
        task = page.locator('[data-quick-ref="QA-1-1"]')
        panel.wait_for(); before = len(gets)
        assert not panel.evaluate('e => e.open')
        page.wait_for_timeout(50); assert len(gets) == before
        hold_get.append(True)
        panel.locator('summary').focus(); page.keyboard.press('Enter')
        page.wait_for_timeout(50); assert len(pending) == 1
        pending.pop().fulfill(status=500, json={'detail':'조회 실패'})
        panel.locator('[data-retry]').click()
        panel.locator('[data-quick-id="ask-codex-review"]').wait_for()
        assert len(gets) == before + 2
        assert panel.locator('.quick-plan script').count() == 0
        assert '<script>' in panel.locator('.quick-plan').inner_text()
        assert page.url.endswith('/#/' + name)
        # 양쪽 Agent와 승인까지 함께 잠가 중복 등록을 막는다.
        start = len(calls)
        panel.locator('[data-quick-id="ask-codex-review"]').click()
        page.wait_for_function("document.querySelector('.quick-body[aria-busy=true]') !== null")
        assert panel.locator('button:not(:disabled)').count() == 0
        panel.locator('[data-quick-id="ask-claude-review"]').evaluate('e => e.click()')
        assert len(calls) == start + 1 and calls[-1][1] == {'provider':'codex'}
        settle(500, {'detail':'요청 실패'})
        panel.locator('[data-quick-id="ask-codex-review"]:not(:disabled)').wait_for()
        assert '다시' in panel.locator('.quick-message').inner_text()
        state['QA-1']['job'] = {'id':999,'mode':'review','provider':'claude','position':2,'note':'검사 대기'}
        panel.locator('[data-quick-id="ask-claude-review"]').click(); settle()
        panel.locator('.queued').wait_for()
        assert '대기 2번째' in panel.inner_text()
        # 진행 항목 폴링과 떠난 화면 정리는 브라우저 시계를 전진해 검사한다.
        state['QA-1']['job'] = None; state['QA-1']['review_running'] = True
        page.clock.fast_forward(15000)
        panel.locator('[data-quick-id="ask-codex-review"]:disabled').wait_for()
        assert panel.locator('[data-verdict="approve"]').count() == 0
        state['QA-1']['review_running'] = False
        page.clock.fast_forward(15000)
        panel.locator('[data-verdict="approve"]').wait_for()
        # 승인한 판과 409 새 판을 구별한다. 자동 재승인하지 않는다.
        panel.locator('[data-verdict="approve"]').click()
        assert calls[-1][1]['plan_version'] == 1
        state['QA-1']['plan']['version'] = 2
        settle(409, {'detail':'계획서가 바뀌었어요'})
        panel.locator('h3:text("v2")').wait_for()
        count = len(calls); page.wait_for_timeout(50); assert len(calls) == count
        panel.locator('[data-verdict="approve_notes"]').click()
        assert '모바일도 확인' in panel.locator('textarea').input_value()
        panel.locator('textarea').fill('모바일도 확인해 주세요')
        panel.locator('[data-quick-id="decision-send"]').click()
        assert calls[-1][1] == {'verdict':'approve_notes','note':'모바일도 확인해 주세요','plan_version':2}
        state['QA-1']['approval'] = {'verdict':'approve_notes','plan_version':2,'stale':False,'actor':'human:admin','created_at':template['created_at'],'note':'모바일'}
        settle(); panel.locator('.decision-state:has-text("조건부")').wait_for()
        assert panel.locator('[data-verdict]').count() == 0
        assert '하위 Task' in panel.inner_text()
        # stale만 재승인 가능하며 Plan 없음·종결 상태에는 승인 버튼이 없다.
        for patch, available in (({'approval':{**state['QA-1']['approval'],'stale':True}}, True),
                                 ({'plan':None,'approval':None}, False),
                                 ({'plan':make('QA-1')['plan'],'status':'done'}, False)):
            state['QA-1'].update(patch); panel.locator('[data-refresh]').click()
            page.wait_for_timeout(100)
            assert bool(panel.locator('[data-verdict="approve"]').count()) == available
        state['QA-1'] = make('QA-1'); panel.locator('[data-refresh]').click()
        panel.locator('[data-verdict="approve"]').wait_for()
        task.locator('summary').click()
        task.locator('[data-quick-id="ask-codex-execute"]').wait_for()
        for blocked, off in (('부모 계획서 승인이 필요해요', True), ('선행 Task 대기', False), (None, False)):
            state['QA-1-1']['execute']['blocked'] = blocked
            task.locator('[data-refresh]').click(); page.wait_for_timeout(100)
            assert task.locator('[data-quick-id="ask-codex-execute"]').is_disabled() == off
        task.locator('[data-quick-id="ask-codex-execute"]').click()
        assert calls[-1][0].endswith('/QA-1-1/execute') and calls[-1][1] == {'provider':'codex'}
        count = len(calls)
        task.locator('[data-quick-id="ask-execute"]').evaluate('e => e.click()')
        assert len(calls) == count and task.locator('[data-quick-id="ask-execute"]').is_disabled()
        state['QA-1-1']['status'] = 'waiting'
        state['QA-1-1']['job'] = {'id':998,'mode':'execute','provider':'codex','position':3,'note':'선행 대기'}
        settle(); task.locator('.queued').wait_for()
        state['QA-1-1']['job'] = None; state['QA-1-1']['status'] = 'in_progress'; state['QA-1-1']['review_running'] = True
        page.clock.fast_forward(15000)
        task.locator('[data-quick-id="ask-codex-execute"]:disabled').wait_for()
        assert '실행 중' in task.locator('[data-quick-id="ask-codex-execute"]').inner_text()
        state['QA-1-1']['review_running'] = False; state['QA-1-1']['status'] = 'backlog'
        task.locator('[data-refresh]').click(); page.wait_for_timeout(100)
        for scheme in ('light','dark'):
            page.evaluate("s => {localStorage.setItem('dev.theme',s); document.documentElement.dataset.theme=s}", scheme)
            page.emulate_media(color_scheme=scheme)
            for width, tag in ((1300,'desktop'),(390,'mobile')):
                page.set_viewport_size({'width':width,'height':850})
                page.wait_for_timeout(200)
                assert page.evaluate('document.documentElement.scrollWidth') <= width + 1
                for button in panel.locator('button:visible').all():
                    box = button.bounding_box(); assert box['height'] >= 40 and box['width'] >= 40
                page.screenshot(path=str(shots / f'quick_{name or "list"}_{scheme}_{tag}.png'), full_page=True)
        # 늦은 조회 응답은 다른 화면에 반영되지 않는다.
        page.set_viewport_size({'width':1300,'height':850}); hold_get.append(True)
        panel.locator('[data-refresh]').click(); page.wait_for_timeout(50)
        assert len(pending) == 1
        page.goto(BASE + '/#/agents'); page.wait_for_selector('#agent-form')
        pending.pop().fulfill(json=state['QA-1']); page.wait_for_timeout(50)
        assert page.locator('.quick-actions').count() == 0
        assert page.evaluate('quickTimers.size') == 0
    page.unroute('**/api/issues?*', listing); page.unroute('**/api/issues/QA-*', detail)
    page.unroute('**/api/issues/QA-*/runs', runs)
    for mode in ('review','execute','decision'):
        page.unroute('**/api/issues/QA-*/' + mode, action)
    page.evaluate("localStorage.setItem('dev.theme','light'); document.documentElement.dataset.theme='light'")
    page.goto(BASE + '/#/')


def check_project_dialog(page, shots):
    """생성창의 키보드·오류 복구·경합과 네 화면을 브라우저에서 검사한다."""
    opener = page.locator('#project-create')
    dialog = page.locator('#project-dialog')
    assert dialog.is_hidden()
    for scheme in ('light', 'dark'):
        page.evaluate("s => { localStorage.setItem('dev.theme', s); document.documentElement.dataset.theme = s; }", scheme)
        page.emulate_media(color_scheme=scheme)
        for width, tag in ((1300, 'desktop'), (390, 'mobile')):
            page.set_viewport_size({'width': width, 'height': 850})
            box = opener.bounding_box()
            assert box['width'] >= 40 and box['height'] >= 40 and box['x'] >= 0
            page.screenshot(path=str(shots / f'project_button_{scheme}_{tag}.png'), full_page=True)
            opener.click()
            assert page.locator('#p-key').evaluate('e => e === document.activeElement')
            box = dialog.bounding_box()
            assert box['x'] >= 0 and box['x'] + box['width'] <= width
            assert box['y'] >= 0 and box['y'] + box['height'] <= 850
            assert dialog.evaluate('e => e.scrollWidth <= e.clientWidth + 1')
            assert page.evaluate('document.documentElement.scrollWidth') <= width + 1
            page.keyboard.press('Tab')
            assert dialog.evaluate('e => e.contains(document.activeElement)')
            page.screenshot(path=str(shots / f'project_dialog_{scheme}_{tag}.png'), full_page=True)
            page.keyboard.press('Escape')
            assert dialog.is_hidden() and opener.evaluate('e => e === document.activeElement')
            opener.click(); page.click('#p-cancel')
            assert dialog.is_hidden() and opener.evaluate('e => e === document.activeElement')
    page.set_viewport_size({'width': 1300, 'height': 850})
    page.evaluate("localStorage.setItem('dev.theme', 'light'); document.documentElement.dataset.theme = 'light'")
    page.emulate_media(color_scheme='light')
    opener.click()
    values = {'#p-key': 'BAD', '#p-name': '입력 보존', '#p-repo': 'https://example.com/repo.git', '#p-path': '/tmp/repo'}
    for selector, value in values.items():
        page.fill(selector, value)
    pending = []
    def hold(route):
        if route.request.method == 'POST':
            pending.append(route)
        else:
            route.continue_()
    page.route('**/api/projects', hold)
    page.click('#project-form button[type=submit]')
    for _ in range(100):
        if pending:
            break
        page.wait_for_timeout(20)
    assert len(pending) == 1 and page.is_disabled('#p-cancel')
    page.evaluate("document.querySelector('#project-form').dispatchEvent(new Event('submit', {bubbles:true,cancelable:true}))")
    page.keyboard.press('Escape'); page.wait_for_timeout(100)
    assert dialog.is_visible() and len(pending) == 1
    pending.pop().fulfill(status=400, content_type='application/json', body=json.dumps({'detail': '검사 오류'}))
    page.wait_for_selector('#project-error:text("검사 오류")')
    assert all(page.input_value(selector) == value for selector, value in values.items())
    assert page.is_enabled('#p-cancel') and page.is_enabled('#project-form button[type=submit]')
    page.unroute('**/api/projects', hold)
    page.click('#p-cancel')
    assert opener.evaluate('e => e === document.activeElement')
    opener.click()
    assert page.input_value('#p-key') == ''
    page.click('#p-cancel')
    page.goto(BASE + '/#/new'); page.wait_for_selector('text=먼저')
    assert page.locator('#project-create').count() == 0
    page.goto(BASE + '/#/projects'); page.wait_for_selector('#project-create')


def check_project_documents(page, shots):
    """설명 저장과 문서 요청·재시도·원문 안전성·반응형 화면을 검사한다."""
    page.click('#project-create')
    page.fill('#p-key', 'DOC'); page.fill('#p-name', '문서 프로젝트')
    page.fill('#p-path', '/mock/docs'); page.fill('#p-description', '제품 의도 <script>')
    page.click('#project-form button[type=submit]')
    page.wait_for_selector('[data-edit="DOC"]')
    assert next(p for p in page.request.get(BASE + '/api/projects').json()['projects'] if p['key'] == 'DOC')['description'] == '제품 의도 <script>'
    page.click('[data-edit="DOC"]'); page.fill('#p-description', '수정한 설명')
    page.click('#project-form button[type=submit]')
    page.wait_for_selector('#p-description')
    page.wait_for_function("document.querySelector('#p-description').value === '수정한 설명'")
    docs = [{'path':'AGENTS.md','purpose':'작업 규칙','status':'available'},
            {'path':'docs/project/01_PRD.md','purpose':'요구사항','status':'missing'}]
    def listing(route):
        route.fulfill(json={'documents':docs, 'requests':[{'ref':'DOC-1','state':'merge_pending'}]})
    def content(route):
        route.fulfill(json={'status':'missing'} if '01_PRD' in route.request.url else
                      {'status':'available','content':'# 규칙\n<script>window.docAttack=1</script>\n<img src=x onerror="window.docAttack=2">'})
    calls, pending = [], []
    def request(route):
        calls.append(route.request.post_data_json['provider'])
        if len(calls) == 1:
            pending.append(route)
        elif len(calls) == 4:
            pending.append(route)
        else:
            route.fulfill(json={'ref':'DOC-1','reused':True})
    page.route('**/api/projects/DOC/documents', listing)
    page.route('**/api/projects/DOC/documents/content?*', content)
    page.route('**/api/projects/DOC/documents/request', request)
    page.click('[data-project-tab="documents"]')
    page.wait_for_selector('.document-source')
    assert '<script>' in page.inner_text('.document-source')
    assert page.evaluate('window.docAttack || 0') == 0
    assert page.locator('#doc-content script, #doc-content img').count() == 0
    assert '병합 대기' in page.inner_text('#docs-requests')
    def check_document_buttons(active=None):
        for provider, name, symbol in (('codex', 'Codex로 문서 생성', '#i-openai'),
                                       ('claude', 'Claude로 문서 생성', '#i-claude')):
            button = page.locator(f'[data-doc-provider="{provider}"]')
            assert button.inner_text() == ('등록 중…' if provider == active else name)
            assert button.locator('svg.ico.brand-icon').count() == 1
            assert button.locator('svg').get_attribute('aria-hidden') == 'true'
            assert button.locator('svg use').get_attribute('href') == symbol
            assert button.is_disabled() == (active is not None)
    check_document_buttons()
    for scheme in ('light','dark'):
        page.evaluate("s => {localStorage.setItem('dev.theme',s); document.documentElement.dataset.theme=s}", scheme)
        page.emulate_media(color_scheme=scheme)
        for width, tag in ((1300,'desktop'),(390,'mobile')):
            page.set_viewport_size({'width':width,'height':850})
            page.wait_for_timeout(200)
            assert page.evaluate('document.documentElement.scrollWidth') <= width + 1
            check_document_buttons()
            page.focus('[data-doc-provider="codex"]')
            page.keyboard.press('Tab')
            assert page.locator('[data-doc-provider="claude"]').evaluate('e => e === document.activeElement')
            page.screenshot(path=str(shots / f'project_docs_{scheme}_{tag}.png'), full_page=True)
            page.click('[data-project-tab="manage"]')
            assert page.input_value('#p-description') == '수정한 설명'
            assert page.evaluate('document.documentElement.scrollWidth') <= width + 1
            page.screenshot(path=str(shots / f'project_manage_{scheme}_{tag}.png'), full_page=True)
            page.click('[data-project-tab="documents"]'); page.wait_for_selector('.document-source')
    page.select_option('#doc-select', 'docs/project/01_PRD.md')
    page.wait_for_selector('#doc-content .empty')
    assert '기준 브랜치' in page.inner_text('#doc-content')
    page.click('[data-doc-provider="codex"]')
    page.wait_for_function("document.querySelector('[data-doc-provider=codex]').disabled")
    assert page.is_disabled('[data-doc-provider="claude"]')
    check_document_buttons('codex')
    page.evaluate("document.querySelector('[data-doc-provider=codex]').click()")
    assert calls == ['codex'] and len(pending) == 1
    pending.pop().fulfill(json={'ref':'DOC-1','queue_error':'연결 실패','retryable':True})
    page.wait_for_selector('#docs-message:text("연결에 실패")')
    assert page.locator('#docs-message a').get_attribute('href') == '#/issue/DOC-1'
    page.wait_for_function("!document.querySelector('[data-doc-provider=codex]').disabled")
    check_document_buttons()
    page.click('[data-doc-provider="codex"]')
    page.wait_for_selector('#docs-message:text("기존 생성 요청")')
    page.wait_for_function("!document.querySelector('[data-doc-provider=codex]').disabled")
    check_document_buttons()
    page.click('[data-doc-provider="claude"]')
    page.wait_for_function("!document.querySelector('[data-doc-provider=claude]').disabled")
    check_document_buttons()
    page.click('[data-doc-provider="claude"]')
    page.wait_for_function("document.querySelector('[data-doc-provider=claude]').disabled")
    check_document_buttons('claude')
    pending.pop().fulfill(status=500, json={'detail':'검사 실패'})
    page.wait_for_selector('#docs-message:text("검사 실패")')
    page.wait_for_function("!document.querySelector('[data-doc-provider=claude]').disabled")
    check_document_buttons()
    assert calls == ['codex','codex','claude','claude']
    docs.clear(); page.click('#docs-refresh')
    page.wait_for_selector('#docs-body .empty')
    assert '아직 문서가 없어요' in page.inner_text('#docs-body')
    docs.append({'path':'AGENTS.md','purpose':'작업 규칙','status':'available'})
    page.click('#docs-refresh'); page.wait_for_selector('.document-source')
    # 키보드로 서브탭과 문서 선택에 접근한다.
    page.focus('[data-project-tab="manage"]'); page.keyboard.press('Enter')
    assert page.locator('#p-description').is_visible()
    page.focus('[data-project-tab="documents"]'); page.keyboard.press('Enter')
    page.wait_for_selector('#doc-select'); page.focus('#doc-select')
    assert page.locator('#doc-select').evaluate('e => e === document.activeElement')
    page.unroute('**/api/projects/DOC/documents', listing)
    page.unroute('**/api/projects/DOC/documents/content?*', content)
    page.unroute('**/api/projects/DOC/documents/request', request)
    assert page.request.delete(BASE + '/api/projects/DOC', headers={'X-Requested-With':'dev'}).ok
    page.set_viewport_size({'width':1300,'height':850})
    page.goto(BASE + '/#/projects'); page.reload(); page.wait_for_selector('#project-create')


def check_project_flows(page, shots, answers, asked):
    """문자열 제안과 임시 DB의 생성·삭제·재확인을 검사한다."""
    cases = [
        ('https://example.com/team/old.git', r'C:\work\old', 'https://example.com/team/new.git', r'C:\work\new'),
        ('https://example.com/team/old/', '/work/old/', 'https://example.com/team/new/', '/work/new/'),
        ('', 'C:\\work\\old\\', '', 'C:\\work\\new\\'),
        ('', '/work/old', '', '/work/new'),
        ('https://example.com/old.git', '', 'https://example.com/new.git', ''),
        ('https://example.com', 'C:', None, None),
    ]
    for repo, path, expected_repo, expected_path in cases:
        result = page.evaluate("""([repo,path]) => {
            const saved = projects;
            projects = [{id:1, created_at:'2026-01-01', repo_url:repo, local_path:path}];
            try { return projectSuggestion('new'); } finally { projects = saved; }
        }""", [repo, path])
        assert result == (None if expected_repo is None else {'repo_url': expected_repo, 'local_path': expected_path}), result
    result = page.evaluate("""() => {
        const saved = projects;
        projects = [{id:9,created_at:'2025-01-01',repo_url:'',local_path:'/wrong/old'},
                    {id:2,created_at:'2026-01-01',repo_url:'',local_path:'/first/old'},
                    {id:3,created_at:'2026-01-01',archived:true,repo_url:'',local_path:'/latest/old'}];
        try { return projectSuggestion('new'); } finally { projects = saved; }
    }""")
    assert result['local_path'] == '/latest/new'
    H = {'X-Requested-With': 'dev'}
    def create(key, **fields):
        r = page.request.post(BASE + '/api/projects', headers=H, data={'key':key,'name':key,**fields})
        assert r.ok, r.text()
    def refresh():
        page.reload(); page.wait_for_selector('#project-create')
    def empty_form(key, name):
        page.click('#project-create'); page.fill('#p-key', key); page.fill('#p-name', name)
        page.click('#project-form button[type=submit]')
    create('REF', repo_url='https://example.com/team/old.git', local_path='/work/old/')
    refresh(); empty_form('NEW', 'new')
    page.wait_for_selector('#project-suggestion')
    assert 'https://example.com/team/new.git' in page.inner_text('#project-suggestion')
    for scheme in ('light', 'dark'):
        page.evaluate("s => { localStorage.setItem('dev.theme', s); document.documentElement.dataset.theme = s; }", scheme)
        page.emulate_media(color_scheme=scheme)
        for width, tag in ((1300,'desktop'),(390,'mobile')):
            page.set_viewport_size({'width':width,'height':850})
            page.wait_for_timeout(300)
            assert page.evaluate('document.documentElement.scrollWidth') <= width + 1
            page.screenshot(path=str(shots / f'project_suggestion_{scheme}_{tag}.png'), full_page=True)
    page.set_viewport_size({'width':1300,'height':850})
    page.click('#suggest-reject')
    assert page.input_value('#p-key') == 'NEW' and page.input_value('#p-name') == 'new'
    assert len(page.request.get(BASE + '/api/projects').json()['projects']) == 1
    page.fill('#p-repo','https://example.com/manual.git')
    page.click('#project-form button[type=submit]'); page.wait_for_selector('[data-delete-project="NEW"]')
    new = next(p for p in page.request.get(BASE + '/api/projects').json()['projects'] if p['key']=='NEW')
    assert new['repo_url']=='https://example.com/manual.git' and new['local_path']==''
    empty_form('YES','yes'); page.wait_for_selector('#suggest-accept'); page.click('#suggest-accept')
    page.wait_for_selector('[data-delete-project="YES"]')
    yes = next(p for p in page.request.get(BASE + '/api/projects').json()['projects'] if p['key']=='YES')
    assert yes['repo_url']=='https://example.com/yes.git' and yes['local_path']==''
    empty_form('BAD','bad/name')
    page.wait_for_selector('#project-error:text("직접 입력")')
    assert page.input_value('#p-name')=='bad/name'; page.click('#p-cancel')
    # 비어 있으면 확인창 없이 삭제하고 저장된 필터도 지운다.
    page.select_option('#project-filter','YES'); page.wait_for_selector('[data-delete-project="YES"]')
    asked.clear(); page.click('[data-delete-project="YES"]')
    page.wait_for_selector('[data-delete-project="YES"]',state='detached')
    assert not asked and page.input_value('#project-filter')==''
    assert page.evaluate("localStorage.getItem('dev.project')") is None
    # 사전 확인 뒤 내용 추가는 서버가 거부하고 다음 클릭에서 경고한다.
    raced = []
    def add_content(route):
        if route.request.method == 'DELETE' and not raced:
            r=page.request.post(BASE+'/api/issues', headers=H, data={'project':'NEW','title':'경합 내용'})
            assert r.ok; raced.append(True)
        route.continue_()
    page.route('**/api/projects/NEW', add_content)
    asked.clear(); page.click('[data-delete-project="NEW"]')
    page.wait_for_function("document.getElementById('toast').textContent.includes('사전 확인')")
    page.unroute('**/api/projects/NEW', add_content)
    assert not asked and page.locator('[data-delete-project="NEW"]').count()==1
    answers.append(None); page.click('[data-delete-project="NEW"]')
    page.wait_for_function("!document.querySelector('[data-delete-project=NEW]').disabled")
    assert 'Issue·Task 1개' in asked[-1] and '의존 연결' in asked[-1] and '댓글' in asked[-1]
    page.click('[data-delete-project="NEW"]'); page.wait_for_selector('[data-delete-project="NEW"]',state='detached')
    asked.clear(); page.click('[data-delete-project="REF"]'); page.wait_for_selector('[data-delete-project="REF"]',state='detached')
    assert not asked
    # 가장 최근 프로젝트에 참고값이 없으면 이전 값을 가져오지 않는다.
    create('EMPTY')
    refresh(); empty_form('NONE', 'none')
    page.wait_for_selector('[data-delete-project="NONE"]')
    none = next(p for p in page.request.get(BASE + '/api/projects').json()['projects'] if p['key']=='NONE')
    assert none['repo_url']=='' and none['local_path']==''
    for key in ('NONE','EMPTY'):
        page.click(f'[data-delete-project="{key}"]')
        page.wait_for_selector(f'[data-delete-project="{key}"]',state='detached')
    page.emulate_media(color_scheme='light')
    page.evaluate("localStorage.setItem('dev.theme', 'light'); document.documentElement.dataset.theme = 'light'")


def check_account_menu(page, shots):
    """검증된 admin에게만 외부 링크를 보여 주고 로그인 전환 시 숨긴다."""
    link = page.locator('#open-jupyter')
    page.click('#user-chip')
    assert link.is_visible() and link.inner_text() == 'jupyter 열기'
    assert link.get_attribute('href') == 'https://jupyter.lomebrote.com/'
    assert link.get_attribute('target') == '_blank'
    assert link.get_attribute('rel') == 'noopener'
    assert link.get_attribute('role') == 'menuitem'
    assert page.get_attribute('#open-nightshift', 'href') == page.request.get(BASE + '/api/auth/me').json()['nightshift_url']
    targets = []
    def intercept(route):
        targets.append(route.request.url)
        route.fulfill(status=200, content_type='text/html', body='<title>검사</title>')
    page.context.route('https://jupyter.lomebrote.com/**', intercept)
    with page.expect_popup() as popup:
        link.click()
    popup.value.wait_for_load_state()
    assert targets == ['https://jupyter.lomebrote.com/']
    assert popup.value.evaluate('window.opener === null')
    popup.value.close()
    page.context.unroute('https://jupyter.lomebrote.com/**', intercept)
    page.keyboard.press('Escape')
    assert page.locator('#user-menu').is_hidden()
    page.click('#user-chip'); page.click('header .brand')
    assert page.locator('#user-menu').is_hidden()
    for scheme in ('light', 'dark'):
        page.evaluate("s => { localStorage.setItem('dev.theme', s); document.documentElement.dataset.theme = s; }", scheme)
        page.emulate_media(color_scheme=scheme)
        for width, tag in ((1300, 'desktop'), (390, 'mobile')):
            page.set_viewport_size({'width': width, 'height': 850})
            page.locator('#user-chip').focus(); page.keyboard.press('Enter')
            page.keyboard.press('Tab'); assert page.locator('#theme').evaluate('e => e === document.activeElement')
            page.keyboard.press('Tab'); assert page.locator('#open-nightshift').evaluate('e => e === document.activeElement')
            page.keyboard.press('Tab'); assert link.evaluate('e => e === document.activeElement')
            assert link.evaluate('e => getComputedStyle(e).outlineStyle') != 'none'
            box = page.locator('#user-menu').bounding_box()
            assert box['x'] >= 0 and box['x'] + box['width'] <= width
            assert page.evaluate('document.documentElement.scrollWidth') <= width + 1
            page.screenshot(path=str(shots / f'jupyter_menu_{scheme}_{tag}.png'), full_page=True)
            page.keyboard.press('Escape')
    page.click('#user-chip'); page.click('#logout')
    page.wait_for_selector('#login:not([hidden])')
    assert link.is_hidden() and link.get_attribute('hidden') is not None
    page.reload(); page.wait_for_selector('#login:not([hidden])')
    assert link.get_attribute('hidden') is not None
    page.context.add_cookies([{'name': 'ns_session', 'value': 'mem', 'url': BASE}])
    page.reload(); page.wait_for_function("document.getElementById('login-error').textContent.includes('관리자')")
    assert link.is_hidden() and link.get_attribute('hidden') is not None
    assert page.request.get(BASE + '/api/auth/me').json()['reason'] == 'not_admin'
    page.context.clear_cookies()
    page.set_viewport_size({'width': 1300, 'height': 850})
    page.evaluate("localStorage.setItem('dev.theme', 'light'); document.documentElement.dataset.theme = 'light'")
    page.emulate_media(color_scheme='light')
    page.fill('#login-username', 'admin'); page.fill('#login-password', 'pw'); page.click('#login-form button')
    page.wait_for_selector('#shell:not([hidden])')
    assert link.get_attribute('hidden') is None


def check_task_actions(page, ref, shots, answers, asked):
    """실행 요청을 가로채 계정 호출 없이 provider·경합·복구를 확인한다."""
    url = '**/api/issues/DEV-4-1/execute'
    pending = []
    def hold(route):
        pending.append(route)
    page.route(url, hold)
    def buttons():
        return page.locator('.run-task[data-ref="DEV-4-1"]')
    for provider, icon in (('claude', 'claude'), ('codex', 'openai')):
        selected = buttons().filter(has=page.locator(f'use[href="#i-{icon}"]'))
        assert selected.count() == 1
        answers.append(None)
        selected.click()
        page.wait_for_function("() => [...document.querySelectorAll('.run-task[data-ref=\"DEV-4-1\"]')].every(b => !b.disabled)")
        assert not pending and page.evaluate('location.hash') == f'#/issue/{ref}'
        assert ('Codex' if provider == 'codex' else 'Claude') in asked[-1]
        assert ('비용 상한은 없어요' if provider == 'codex' else '비용 상한은 $2') in asked[-1]
        selected.click()
        for _ in range(100):
            if pending:
                break
            page.wait_for_timeout(20)
        assert len(pending) == 1 and pending[0].request.post_data_json == {'provider': provider}
        assert all(buttons().nth(i).is_disabled() for i in range(2))
        # disabled 속성을 우회한 교차 클릭도 Task 단위 잠금으로 차단한다.
        buttons().evaluate_all("bs => bs.forEach(b => b.dispatchEvent(new MouseEvent('click', {bubbles:true})))")
        page.wait_for_timeout(100)
        assert len(pending) == 1 and page.evaluate('location.hash') == f'#/issue/{ref}'
        pending.pop().fulfill(status=500, content_type='application/json', body='{"detail":"시험 실패"}')
        page.wait_for_function("() => [...document.querySelectorAll('.run-task[data-ref=\"DEV-4-1\"]')].every(b => !b.disabled)")
    page.unroute(url, hold)
    # 서버의 기존 승인·상태 조건을 유지한다.
    assert page.evaluate("() => [null, {stale:true, verdict:'approve'}, {stale:false, verdict:'reject'}].every(approval => taskActionsHtml({approval}, {status:'backlog'}) === '')")
    assert page.evaluate("() => ['in_progress', 'in_review', 'done', 'closed', 'on_hold'].every(status => taskActionsHtml({approval:{verdict:'approve'}}, {status}) === '')")
    for scheme in ('light', 'dark'):
        page.evaluate('s => { document.documentElement.dataset.theme = s; }', scheme)
        for width, tag in ((1300, 'desktop'), (390, 'mobile')):
            page.set_viewport_size({'width': width, 'height': 850})
            page.wait_for_timeout(150)
            assert page.evaluate('document.documentElement.scrollWidth') <= width + 1
            for i in range(2):
                button = buttons().nth(i)
                assert button.get_attribute('title') == button.get_attribute('aria-label')
                if tag == 'mobile':
                    box = button.bounding_box()
                    assert box['width'] >= 40 and box['height'] >= 40
                    assert not button.inner_text()
                page.keyboard.press('Tab')
                button.focus()
                assert button.evaluate("b => b.matches(':focus-visible') && getComputedStyle(b).outlineStyle !== 'none'")
            page.screenshot(path=str(shots / f'task_actions_{scheme}_{tag}.png'), full_page=True)
    page.set_viewport_size({'width': 1300, 'height': 850})
    # 실제 POST는 running 가짜 run을 넣은 뒤 큐에만 추가한다.


def check_execute_buttons(page, shots, answers):
    """실행 요청과 기록을 모의하여 provider 표시·중복 방지·복구를 확인한다."""
    ref = 'DEV-4-1'
    url = f'**/api/issues/{ref}/execute'
    pending = []
    def hold(route):
        pending.append(route)
    page.route(url, hold)
    ids = {'claude': '#ask-execute', 'codex': '#ask-codex-execute'}
    def ready():
        page.wait_for_selector('#ask-execute:not([disabled])')
        assert page.is_enabled('#ask-codex-execute')
    def active(provider):
        for key, selector in ids.items():
            assert page.is_disabled(selector)
            assert page.inner_text(selector) == ('실행 중…' if key == provider else f'{"Claude" if key == "claude" else "Codex"}에게 실행 맡기기')
    page.goto(BASE + f'/#/issue/{ref}'); page.reload(); ready()
    for provider in ids:
        answers.append(None); page.click(ids[provider]); ready()
        assert not pending
        page.click(ids[provider])
        page.wait_for_timeout(100)
        assert len(pending) == 1 and pending[0].request.post_data_json == {'provider': provider}
        active(provider)
        page.evaluate('() => { renderIssue("DEV-4-1"); }')
        page.wait_for_timeout(200); active(provider)
        page.locator('.exec button').evaluate_all("bs => bs.forEach(b => b.dispatchEvent(new MouseEvent('click', {bubbles:true})))")
        page.wait_for_timeout(100); assert len(pending) == 1
        pending.pop().fulfill(status=500, content_type='application/json', body='{"detail":"시험 실패"}')
        ready()
    page.unroute(url, hold)
    # 서버 재조회와 브라우저 새로고침은 실행 기록의 provider를 사용한다.
    original = page.request.get(BASE + f'/api/issues/{ref}').json()
    state = {'running': True, 'provider': 'claude'}
    def issue(route):
        route.fulfill(json={**original, 'review_running': state['running']})
    def runs(route):
        route.fulfill(json={'runs': [{'id': 999, 'mode': 'execute', 'status': 'running' if state['running'] else 'ok', 'provider': state['provider'], 'started_at': '2026-10-03T00:00:00+00:00'}]})
    page.route(f'**/api/issues/{ref}', issue)
    page.route(f'**/api/issues/{ref}/runs', runs)
    for provider in ids:
        state.update(running=True, provider=provider)
        page.reload(); page.wait_for_selector(ids[provider]); active(provider)
        page.evaluate('() => { renderIssue("DEV-4-1"); }'); page.wait_for_timeout(200); active(provider)
        for scheme in ('light', 'dark'):
            page.evaluate('s => { document.documentElement.dataset.theme = s; }', scheme)
            for width, tag in ((1300, 'desktop'), (390, 'mobile')):
                page.set_viewport_size({'width': width, 'height': 850})
                assert page.evaluate('document.documentElement.scrollWidth') <= width + 1
                page.screenshot(path=str(shots / f'execute_running_{provider}_{scheme}_{tag}.png'), full_page=True)
        state['running'] = False
        page.reload(); ready()
        assert '실행 중' not in page.inner_text('.exec')
    page.unroute(f'**/api/issues/{ref}', issue)
    page.unroute(f'**/api/issues/{ref}/runs', runs)
    page.set_viewport_size({'width': 1300, 'height': 850})


def check_list_loading(page, shots):
    """응답을 보류하여 요청 수명·경합·시각 상태를 검사한다."""
    # 로그인은 shell 표시 뒤에도 초기 조회와 route가 이어진다. 이전 요청을 가로채지 않는다.
    page.wait_for_selector('#list-body[aria-busy="false"]')
    pending = []
    page.route("**/api/issues?*", lambda route: pending.append(route))
    def request_count(n):
        for _ in range(100):
            if len(pending) >= n:
                return
            page.wait_for_timeout(20)
        raise AssertionError(f"목록 요청 누락: {n}")
    def reply(route, items=(), more=False, status=200):
        route.fulfill(status=status, content_type="application/json", body=json.dumps(
            {"issues": list(items), "has_more": more} if status == 200 else {"detail": "시험 오류"}))
    def item(n):
        return {"ref": f"TEST-{n}", "title": f"시험 Issue {n}", "status": "backlog", "labels": []}
    def reload_list():
        page.evaluate("() => { loadList(); }")
        request_count(1)
        return pending.pop(0)
    def settled():
        page.wait_for_function("document.querySelector('#list-body').getAttribute('aria-busy') === 'false'")
        assert page.locator('.list-loading').count() == 0

    page.goto(BASE + '/#/projects')
    page.wait_for_selector('#project-create')
    page.goto(BASE + '/#/')
    request_count(1)
    first = pending.pop(0)
    assert page.get_attribute('#list-body', 'aria-busy') == 'true'
    page.wait_for_selector('#list-body .list-loading')
    for scheme in ('light', 'dark'):
        for width, tag in ((1300, 'desktop'), (390, 'mobile')):
            page.evaluate("s => { document.documentElement.dataset.theme = s; }", scheme)
            page.set_viewport_size({"width": width, "height": 850})
            page.wait_for_timeout(200)
            assert page.evaluate('document.documentElement.scrollWidth') <= width + 1
            box = page.locator('.list-loading').bounding_box()
            assert box['x'] >= 0 and box['x'] + box['width'] <= width
            page.screenshot(path=str(shots / f'loading_{scheme}_{tag}.png'), full_page=True)
    page.emulate_media(reduced_motion='reduce')
    assert page.locator('.list-spinner').evaluate("el => getComputedStyle(el).animationName") == 'none'
    assert page.locator('.list-loading').inner_text() == 'Issue를 불러오는 중이에요'
    page.screenshot(path=str(shots / 'loading_reduced_motion.png'), full_page=True)
    page.emulate_media(reduced_motion='no-preference')
    page.set_viewport_size({"width": 1300, "height": 850})
    reply(first, [item(1)])
    page.wait_for_selector('tr.row'); settled()

    # 빠른 응답과 빈 결과: 남은 타이머가 표시를 되살리지 않는다.
    reply(reload_list()); settled()
    page.wait_for_timeout(400)
    assert page.locator('.empty').count() == 1 and page.locator('.list-loading').count() == 0
    failed = reload_list(); page.wait_for_selector('.list-loading')
    reply(failed, status=500); settled()
    assert page.locator('#list-body .list-error').count() == 1
    page.wait_for_timeout(500); assert not pending

    # 필터 경합: 이전 성공·실패가 새 요청의 busy·표시·결과를 건드리지 않는다.
    for status in (200, 500):
        old = reload_list()
        page.click('[data-st="backlog"]')
        request_count(1); new = pending.pop(0)
        page.wait_for_selector('.list-loading')
        reply(old, [item(99)], status=status)
        page.wait_for_timeout(100)
        assert page.get_attribute('#list-body', 'aria-busy') == 'true'
        assert page.locator('.list-loading').count() == 1
        reply(new, [item(2)]); settled()
        assert page.locator('tr.row').get_attribute('data-ref') == 'TEST-2'
    # 새 결과 후 늦은 응답도 무시한다.
    old = reload_list(); new = reload_list()
    reply(new, [item(3)]); settled()
    reply(old, [item(99)]); page.wait_for_timeout(100)
    assert page.locator('tr.row').get_attribute('data-ref') == 'TEST-3'

    # 검색 debounce와 프로젝트·승인 필터에도 같은 로딩 수명을 적용한다.
    page.fill('#q', '검색 시험'); request_count(1)
    search = pending.pop(0)
    assert 'q=' in search.request.url
    page.wait_for_selector('.list-loading'); reply(search); settled()
    page.fill('#q', ''); request_count(1); reply(pending.pop(0)); settled()
    page.check('#only-approved'); request_count(1)
    approved = pending.pop(0)
    assert 'approved=true' in approved.request.url
    page.wait_for_selector('.list-loading'); reply(approved); settled()
    page.uncheck('#only-approved'); request_count(1); reply(pending.pop(0)); settled()
    page.evaluate("() => { const s = document.querySelector('#list-project'); s.add(new Option('시험', 'TEST')); }")
    page.select_option('#list-project', 'TEST'); request_count(1)
    project = pending.pop(0)
    assert 'project=TEST' in project.request.url
    page.wait_for_selector('.list-loading'); reply(project); settled()
    page.select_option('#list-project', ''); request_count(1); reply(pending.pop(0)); settled()
    network = reload_list(); page.wait_for_selector('.list-loading')
    network.abort(); settled()
    assert page.locator('#list-body .list-error').count() == 1

    # 추가 페이지를 자동으로 이어 받는 동안 기존 행을 유지한다.
    first = reload_list(); reply(first, [item(1)], more=True)
    request_count(1); extra = pending.pop(0)
    page.wait_for_selector('#list-more .list-loading')
    assert page.locator('tr.row').count() == 1
    reply(extra, [item(2)]); settled()
    assert page.locator('tr.row').count() == 2
    first = reload_list(); reply(first, [item(1)], more=True)
    request_count(1); extra = pending.pop(0)
    page.wait_for_selector('#list-more .list-loading')
    reply(extra, status=500); settled()
    assert page.locator('tr.row').count() == 1 and page.locator('#list-more .list-error').count() == 1
    page.wait_for_timeout(500); assert not pending

    # 탭 이동 후의 지연 응답과 검색 debounce가 목록을 되살리지 않는다.
    for status in (200, 500):
        old = reload_list()
        page.fill('#q', '나중 검색')
        page.goto(BASE + '/#/projects'); page.wait_for_selector('#project-create')
        reply(old, [item(99)], more=True, status=status)
        page.wait_for_timeout(500)
        assert page.locator('#project-form').count() == 1
        assert page.locator('.list-loading').count() == 0 and not pending
        page.goto(BASE + '/#/'); request_count(1)
        reply(pending.pop(0)); settled()
    page.unroute('**/api/issues?*')
    page.evaluate("localStorage.removeItem('dev.list'); document.documentElement.dataset.theme = 'light'")


def check_review_buttons(page, ref, shots):
    """실제 Agent 호출 없이 provider별 요청·실행·복구 상태를 검사한다."""
    url = f'{BASE}/api/issues/{ref}'
    original = page.request.get(url).json()
    state = {'running': False, 'provider': None, 'mode': 'review', 'records': True}
    pending = []
    page.route(url, lambda route: route.fulfill(json={**original, 'review_running': state['running'], 'job': None}))
    def runs(route):
        route.fulfill(json={'runs': [{'id': 999, 'mode': state['mode'], 'provider': state['provider'],
            'status': 'running', 'actor': 'human:admin', 'started_at': '2026-10-03T00:00:00+00:00'}]
            if state['running'] and state['records'] else []})
    page.route(url + '/runs', runs)
    page.route(url + '/review', lambda route: pending.append(route))
    def refresh():
        page.evaluate('(ref) => renderIssue(ref)', ref)
    def buttons(provider=None, busy=False):
        for name, selector in (('claude', '#ask-review'), ('codex', '#ask-codex-review')):
            button = page.locator(selector)
            assert button.is_disabled() == busy
            assert button.inner_text() == ('검토 중…' if name == provider else f'{name.title()}에게 검토 맡기기')
            assert button.locator('svg').get_attribute('aria-hidden') == 'true'
            assert button.locator('use').get_attribute('href') == ('#i-claude' if name == 'claude' else '#i-openai')
    for provider, selector in (('claude', '#ask-review'), ('codex', '#ask-codex-review')):
        state.update(running=False, provider=provider)
        refresh(); buttons()
        page.click(selector)
        page.wait_for_timeout(100)
        assert len(pending) == 1 and pending[0].request.post_data_json == {'provider': provider}
        buttons(provider, True)
        page.evaluate("() => { document.querySelector('#ask-review').click(); document.querySelector('#ask-codex-review').click(); }")
        assert len(pending) == 1
        refresh(); buttons(provider, True)
        # 오류 후 두 버튼과 라벨이 복구되어 재시도할 수 있다.
        pending.pop().fulfill(status=500, json={'detail': '시험 오류'})
        page.wait_for_function("!document.querySelector('#ask-review').disabled")
        buttons()
        page.click(selector); page.wait_for_timeout(100)
        state['running'] = True
        pending.pop().fulfill(json={'status': 'running'})
        page.wait_for_timeout(100); buttons(provider, True)
        refresh(); buttons(provider, True)
        page.reload(); page.wait_for_selector('#ask-review'); buttons(provider, True)
        # 실제 15초 갱신 뒤에도 서버 기록으로 provider를 복원한다.
        if provider == 'codex':
            page.wait_for_timeout(15500); buttons(provider, True)
        state['running'] = False
        refresh(); buttons()
    state.update(running=True, provider='codex')
    refresh(); buttons('codex', True)
    for scheme in ('light', 'dark'):
        for width, tag in ((1300, 'desktop'), (390, 'mobile')):
            page.evaluate('s => { document.documentElement.dataset.theme = s; }', scheme)
            page.set_viewport_size({'width': width, 'height': 850})
            assert page.evaluate('document.documentElement.scrollWidth') <= width + 1
            for selector in ('#ask-review', '#ask-codex-review'):
                box = page.locator(selector + ' svg').bounding_box()
                assert box['width'] > 0 and box['x'] >= 0 and box['x'] + box['width'] <= width
                assert page.locator(selector + ' svg').evaluate('el => getComputedStyle(el).stroke') == 'none'
            page.screenshot(path=str(shots / f'review_running_{scheme}_{tag}.png'), full_page=True)
    # 실행 기록의 시차나 execute 실행을 Claude 검토로 단정하지 않는다.
    state['records'] = False
    refresh(); buttons(busy=True)
    state.update(records=True, mode='execute')
    refresh(); buttons(busy=True)
    page.unroute(url); page.unroute(url + '/runs'); page.unroute(url + '/review')
    page.set_viewport_size({'width': 1300, 'height': 850})
    page.reload(); page.wait_for_selector('#ask-review')


def check_edit_saving(page, shots):
    """본문과 Plan의 지연·실패·재시도 및 저장 값 스냅샷을 검사한다."""
    ref = page.request.post(f'{BASE}/api/issues', data={
        'project': 'NS', 'title': '저장 시험', 'body': '원래 본문'}, headers=H).json()['ref']
    page.goto(BASE + f'/#/issue/{ref}')
    # 해시 이동 뒤에도 이전 Issue의 편집 버튼이 남아 있으므로 대상 번호까지 확인한다.
    page.wait_for_function("ref => document.querySelector('.detail > div > .ref')?.textContent.trim() === ref", arg=ref)
    page.wait_for_selector('#edit-body')
    for box, method, suffix in (('body', 'PATCH', ''), ('plan', 'POST', '/plans')):
        pending = []
        url = f'**/api/issues/{ref}{suffix}'
        def hold(route):
            if route.request.method == method:
                pending.append(route)
            else:
                route.continue_()
        page.route(url, hold)
        page.click(f'#edit-{box}')
        text = f'수정한 {box}\n재시도에도 보존해요'
        page.fill(f'#{box} textarea', text)
        if box == 'body':
            page.fill('#body .edit-title', '수정한 제목')
        width = page.locator(f'#{box} .save').bounding_box()['width']
        page.click(f'#{box} .save')
        page.wait_for_function('() => document.querySelector("#' + box + ' .save").disabled')
        for _ in range(100):
            if pending:
                break
            page.wait_for_timeout(20)
        assert len(pending) == 1
        assert pending[0].request.post_data_json['body'] == text
        assert page.locator(f'#{box} input:enabled, #{box} textarea:enabled, #{box} button:enabled').count() == 0
        assert page.get_attribute(f'#{box} .save', 'aria-label') == '저장 중'
        assert page.get_attribute(f'#{box} .save svg use', 'href') == '#i-loader-circle'
        assert abs(page.locator(f'#{box} .save').bounding_box()['width'] - width) < 1
        # 비활성 버튼의 합성 이벤트까지 중복 제출을 막는다.
        page.locator(f'#{box} .save').dispatch_event('click')
        page.wait_for_timeout(100)
        assert len(pending) == 1
        if box == 'body':
            assert pending[0].request.post_data_json['title'] == '수정한 제목'
            for scheme in ('light', 'dark'):
                page.evaluate("s => { localStorage.setItem('dev.theme', s); document.documentElement.dataset.theme = s; }", scheme)
                page.emulate_media(color_scheme=scheme)
                for w, tag in ((1300, 'desktop'), (390, 'mobile')):
                    page.set_viewport_size({'width': w, 'height': 850})
                    page.wait_for_timeout(300)
                    assert page.evaluate('document.documentElement.scrollWidth') <= w + 1
                    page.screenshot(path=str(shots / f'saving_{scheme}_{tag}.png'), full_page=True)
            page.emulate_media(reduced_motion='reduce')
            assert page.locator('#body .list-spinner').evaluate("e => getComputedStyle(e).animationName") == 'none'
            page.emulate_media(reduced_motion='no-preference')
            page.set_viewport_size({'width': 1300, 'height': 850})
        pending.pop().fulfill(status=500, content_type='application/json', body=json.dumps({'detail': '시험 저장 실패'}))
        page.wait_for_function('() => !document.querySelector("#' + box + ' .save").disabled')
        assert page.input_value(f'#{box} textarea') == text
        assert page.inner_text(f'#{box} .save') == '저장'
        assert page.get_attribute(f'#{box} .save', 'aria-busy') is None
        assert page.is_enabled(f'#{box} .cancel')
        if box == 'body':
            assert page.input_value('#body .edit-title') == '수정한 제목'
        page.click(f'#{box} .save')
        for _ in range(100):
            if pending:
                break
            page.wait_for_timeout(20)
        assert len(pending) == 1
        pending.pop().continue_()
        page.wait_for_selector(f'#{box} textarea', state='detached')
        got = page.request.get(f'{BASE}/api/issues/{ref}').json()
        assert (got['body'] if box == 'body' else got['plan']['body']) == text
        page.unroute(url, hold)
    page.evaluate("localStorage.setItem('dev.theme', 'light'); document.documentElement.dataset.theme = 'light'")
    page.emulate_media(color_scheme='light')


def check_board_pan(page, shots):
    """빈 영역 이동과 포인터 정리, 기본 스크롤 및 카드 클릭을 검사한다."""
    board = page.locator('.kanban')
    changes = []
    def record(req):
        if req.method == 'POST' and req.url.endswith('/status'):
            changes.append(req.url)
    page.on('request', record)
    def left():
        return board.evaluate('e => e.scrollLeft')
    def drag(dx, selector='.kanban', outside=False):
        box = page.locator(selector).first.bounding_box()
        x, y = box['x'] + min(box['width'] - 20, 240), box['y'] + box['height'] - 20
        page.mouse.move(x, y); page.mouse.down()
        page.mouse.move(x + dx, y, steps=10)
        if outside:
            page.mouse.move(x + dx, 5)
        page.mouse.up()
        assert not board.evaluate("e => e.classList.contains('panning')")
    assert board.evaluate("e => getComputedStyle(e).scrollbarWidth") == 'none'
    assert board.evaluate("e => getComputedStyle(e, '::-webkit-scrollbar').display") == 'none'
    assert board.evaluate('e => e.scrollWidth > e.clientWidth')
    drag(-150, outside=True); assert left() >= 140
    drag(100); assert left() < 70
    board.evaluate('e => e.scrollLeft = 0')
    drag(-100, '.col[data-col="triage"] .cards'); assert left() >= 90
    for event in ('pointercancel', 'lostpointercapture'):
        box = board.bounding_box()
        page.mouse.move(box['x'] + 200, box['y'] + box['height'] - 20)
        page.mouse.down(); page.mouse.move(box['x'] + 150, box['y'] + box['height'] - 20)
        board.dispatch_event(event, {'pointerId': 1})
        assert not board.evaluate("e => e.classList.contains('panning')")
        page.mouse.up()
        drag(-50)
    board.evaluate('e => e.scrollLeft = 0')
    drag(100); assert left() == 0
    board.evaluate('e => e.scrollLeft = e.scrollWidth')
    end = left(); drag(-100); assert left() == end
    board.focus(); page.keyboard.press('ArrowLeft'); assert left() < end
    page.keyboard.press('ArrowRight'); assert left() == end
    board.evaluate('e => e.scrollLeft = 0')
    box = board.bounding_box()
    page.mouse.move(box['x'] + 100, box['y'] + 150)
    page.mouse.wheel(160, 0)
    page.wait_for_function("document.querySelector('.kanban').scrollLeft > 0")
    # 길어진 열은 독립적으로 세로 스크롤한다. 임시 DOM은 화면 재진입으로 제거한다.
    page.locator('.cards').filter(has=page.locator('.card')).first.evaluate('e => { const card = e.querySelector(".card"); for(let i=0;i<20;i++) e.append(card.cloneNode(true)); e.scrollTop=100; }')
    assert page.locator('.cards').filter(has=page.locator('.card')).first.evaluate('e => e.scrollTop') > 0
    assert changes == []
    page.remove_listener('request', record)
    page.goto(BASE + '/#/'); page.wait_for_selector('table.issues')
    page.goto(BASE + '/#/board'); page.wait_for_selector('.kanban')
    drag(-100); assert 90 <= left() <= 110
    board.evaluate('e => e.scrollLeft = 0')
    page.locator('.card[data-ref="NS-1-1"] .t').click()
    page.wait_for_selector('h1#title')
    assert page.url.endswith('/issue/NS-1-1')
    page.goto(BASE + '/#/board'); page.wait_for_selector('.kanban')
    for scheme in ('light', 'dark'):
        page.evaluate('s => { localStorage.setItem("dev.theme", s); document.documentElement.dataset.theme = s; }', scheme)
        page.emulate_media(color_scheme=scheme)
        for width, tag in ((1300, 'desktop'), (390, 'mobile')):
            page.set_viewport_size({'width': width, 'height': 850})
            board.evaluate('e => e.scrollLeft = 0')
            assert page.evaluate('document.documentElement.scrollWidth') <= width + 1
            page.screenshot(path=str(shots / f'board_pan_{scheme}_{tag}.png'), full_page=True)
            board.evaluate('e => e.scrollLeft = e.scrollWidth')
            assert page.locator('.col').last.bounding_box()['x'] < width
            assert board.evaluate('e => getComputedStyle(e).touchAction') == 'auto'
            before = left()
            board.dispatch_event('pointerdown', {'pointerType': 'touch', 'pointerId': 9, 'button': 0, 'clientX': 200})
            board.dispatch_event('pointermove', {'pointerType': 'touch', 'pointerId': 9, 'buttons': 1, 'clientX': 100})
            assert left() == before and not board.evaluate("e => e.classList.contains('panning')")
    page.set_viewport_size({'width': 1300, 'height': 850})
    page.emulate_media(color_scheme='light')
    page.evaluate('localStorage.setItem("dev.theme", "light"); document.documentElement.dataset.theme = "light"')
    board.evaluate('e => e.scrollLeft = 0')


tmp = Path(tempfile.mkdtemp())
(tmp / "fake_ns.py").write_text(FAKE_NS, "utf-8")
ns_port, dev_port = free_port(), free_port()
env = {**os.environ, "DEV_DATA_DIR": str(tmp / "data"), "DEV_NIGHTSHIFT_URL": f"http://127.0.0.1:{ns_port}"}
procs = [subprocess.Popen([sys.executable, "-m", "uvicorn", "fake_ns:app", "--port", str(ns_port)], cwd=tmp, stderr=subprocess.DEVNULL),
         subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--port", str(dev_port)], cwd=ROOT / "server", env=env, stderr=subprocess.DEVNULL)]
BASE = f"http://127.0.0.1:{dev_port}"
shots = ROOT / "tests" / "shots"; shots.mkdir(exist_ok=True)
try:
    wait_port(ns_port); wait_port(dev_port)
    with sync_playwright() as p:
        page = p.chromium.launch().new_page(viewport={"width": 1300, "height": 850})
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        answers = []   # 입력창(prompt)에 줄 답 — 비었으면 기본값. None이면 취소.
        asked = []     # 띄운 확인 창 문구
        def on_dialog(d):
            asked.append(d.message)
            a = answers.pop(0) if answers else "모바일도 확인해 주세요"
            d.dismiss() if a is None else d.accept(a) if d.type == "prompt" else d.accept()
        page.on("dialog", on_dialog)
        page.goto(BASE)
        page.wait_for_selector("#login-form")
        assert page.locator('#open-jupyter').get_attribute('hidden') is not None
        page.fill("#login-username", "admin"); page.fill("#login-password", "nope"); page.click("#login-form button")
        page.wait_for_function("document.getElementById('login-error').textContent.includes('올바르지')")
        page.fill("#login-password", "pw"); page.click("#login-form button")
        page.wait_for_selector("#shell:not([hidden])")
        check_account_menu(page, shots)
        if '--edit-saving-only' in sys.argv:
            H = {'X-Requested-With': 'dev'}
            page.wait_for_selector('#list-body[aria-busy="false"]')
            assert page.request.post(BASE + '/api/projects', data={'key': 'NS', 'name': '저장 검사'}, headers=H).ok
            previous = page.request.post(BASE + '/api/issues', data={'project': 'NS', 'title': '이전 화면'}, headers=H).json()['ref']
            page.goto(BASE + f'/#/issue/{previous}')
            page.wait_for_function("ref => document.querySelector('.detail > div > .ref')?.textContent.trim() === ref", arg=previous)
            check_edit_saving(page, shots)
            assert not errs, errs
            print('OK: edit saving')
            sys.exit(0)
        if '--account-menu-only' in sys.argv:
            assert not errs, errs
            print('OK: account menu')
            sys.exit(0)
        if '--project-flows-only' not in sys.argv:
            check_list_loading(page, shots)
        if '--list-loading-only' in sys.argv:
            assert not errs, errs
            print('OK: list loading')
            sys.exit(0)

        # 프로젝트 없이 New issue → 안내
        page.click("a[href=\"#/new\"]")
        page.wait_for_selector("text=먼저")
        page.goto(BASE + "/#/projects")
        check_project_dialog(page, shots)
        check_project_flows(page, shots, answers, asked)
        check_project_documents(page, shots)
        if '--project-flows-only' in sys.argv:
            assert not errs, errs
            print('OK: project flows')
            sys.exit(0)
        page.click("#project-create")
        page.fill("#p-key", "NS"); page.fill("#p-name", "nightshift"); page.click("#project-form button[type=submit]")
        page.wait_for_selector("td.ref:text('NS')")
        assert page.get_attribute("#p-path", "placeholder").startswith("C:\\Users\\")
        # 고치기 — 키는 잠기고, 이름·경로가 바뀐다
        page.click('[data-edit="NS"]')
        assert page.is_disabled("#p-key") and page.input_value("#p-name") == "nightshift"
        page.fill("#p-path", r"C:\Users\Simon Lomebrote\Projects\nightshift")
        page.click("#project-form button[type=submit]")
        page.wait_for_function("p => [...document.querySelectorAll('td')].some(td => td.textContent === p)",
                               arg=r"C:\Users\Simon Lomebrote\Projects\nightshift")
        assert page.is_disabled("#p-key")   # 저장 후에도 현재 프로젝트 관리 범위를 유지한다.
        page.click('[data-edit="NS"]'); page.click("#p-cancel")
        page.check('[data-archive="NS"]')
        page.wait_for_function("projects.find(p => p.key === 'NS').archived")
        page.wait_for_selector('[data-archive="NS"]:checked')
        page.uncheck('[data-archive="NS"]')
        page.wait_for_function("!projects.find(p => p.key === 'NS').archived")
        assert page.locator("#project-dialog").is_hidden()

        page.click("a[href=\"#/new\"]")
        page.fill("#n-title", "보드 카드 복사")
        page.fill("#n-body", "## 요구\n- 우클릭 메뉴\n- `Ctrl+C`\n\n<script>alert(1)</script> NS-1 참고")
        page.select_option("#n-priority", "high")
        page.fill("#n-labels", "board, ui")
        page.click("#new-form button[type=submit]")
        page.wait_for_selector("h1#title:text('보드 카드 복사')")
        assert page.url.endswith("#/issue/NS-1")
        body_html = page.inner_html("#body")
        assert "<h2>요구</h2>" in body_html and "<code>Ctrl+C</code>" in body_html and "<script>" not in body_html and "&lt;script&gt;" in body_html

        # 계획서 쓰기 → v1, 댓글, 상태
        page.click("#edit-plan"); page.fill("#plan textarea", "1. 메뉴\n2. 붙여넣기"); page.click("#plan .save")
        page.wait_for_selector("text=v1")
        page.fill("#comment", "시작할게요"); page.click("#send-comment")
        page.wait_for_selector(".timeline >> text=시작할게요")
        page.select_option("#status", "in_review")
        page.wait_for_selector("#approve")
        assert page.locator('#result').count() == 1
        assert page.locator('#result').evaluate("e => e.closest('.panel').previousElementSibling.querySelector('h2').textContent.startsWith('Plan')")
        page.click("#request-changes")
        page.click("#result-send"); page.wait_for_timeout(200)   # 메모 없이는 안 보낸다
        assert page.evaluate("document.getElementById('status').value") == "in_review"
        page.fill("#result-note", "모바일도 확인해 주세요"); page.click("#result-send")
        page.wait_for_function("document.getElementById('status').value === 'changes_requested'")
        assert "모바일도 확인해 주세요" in page.inner_text(".timeline")
        page.screenshot(path=str(shots / "issue.png"), full_page=True)

        # 하위 Task
        page.click("text=Task 추가")
        page.fill("#n-title", "Task: 메뉴 UI"); page.click("#new-form button[type=submit]")
        page.wait_for_selector("text=Task of")
        assert page.url.endswith("#/issue/NS-1-1")

        # 목록 — 기본은 열린 것만, 검색
        page.goto(BASE + "/#/")
        page.wait_for_selector("tr.row")
        assert page.locator("tr.row").count() == 2
        page.fill("#q", "메뉴 UI"); page.wait_for_function("document.querySelectorAll('tr.row').length === 1")
        page.fill("#q", ""); page.wait_for_function("document.querySelectorAll('tr.row').length === 2")
        page.screenshot(path=str(shots / "list.png"))

        # Task는 부모 바로 아래에 들여써서 붙고, 접으면 숨고, 새로고침해도 기억한다
        kids = lambda: page.evaluate("[...document.querySelectorAll('tr.row')].map(r => r.dataset.ref + (r.hidden ? ':hidden' : ''))")
        assert kids() == ["NS-1", "NS-1-1"], kids()   # 부모 다음에 자식
        assert page.locator('tr.row[data-ref="NS-1-1"]').get_attribute("class").split() == ["row", "child"]
        assert page.text_content('tr.row[data-ref="NS-1"] .kids') == "Tasks 1"
        page.click('tr.row[data-ref="NS-1"] .tog')
        assert "NS-1-1:hidden" in kids() and page.get_attribute('tr.row[data-ref="NS-1"] .tog', "aria-expanded") == "false"
        assert page.url.endswith("#/") or page.url.endswith("/")   # 토글은 상세로 이동하지 않는다
        page.reload(); page.wait_for_selector("tr.row"); page.wait_for_selector(".tog")
        assert "NS-1-1:hidden" in kids()
        page.screenshot(path=str(shots / "list_collapsed.png"))
        page.click('tr.row[data-ref="NS-1"] .tog')
        assert "NS-1-1" in kids() and page.get_attribute('tr.row[data-ref="NS-1"] .tog', "aria-expanded") == "true"
        for scheme in ("light", "dark"):
            for w, h, tag in ((1300, 700, "desktop"), (390, 700, "mobile")):
                page.emulate_media(color_scheme=scheme); page.set_viewport_size({"width": w, "height": h}); page.wait_for_timeout(300)
                assert page.evaluate("document.documentElement.scrollWidth") <= w + 1
                page.screenshot(path=str(shots / f"list_tree_{scheme}_{tag}.png"))
        page.emulate_media(color_scheme="light"); page.set_viewport_size({"width": 1300, "height": 850})

        # 칸반 — 끌어서 상태 바꾸기
        page.click("[data-nav=board]")
        page.wait_for_selector('.card[data-ref="NS-1-1"]')
        page.drag_and_drop('.card[data-ref="NS-1-1"]', '.col[data-col="in_progress"] .cards')
        page.wait_for_selector('.col[data-col="in_progress"] .card[data-ref="NS-1-1"]')
        page.screenshot(path=str(shots / "board.png"))
        check_board_pan(page, shots)

        # 에이전트 — 키는 한 번만
        page.click("[data-nav=agents]")
        page.fill("#a-name", "claude"); page.fill("#a-vendor", "anthropic"); page.fill("#a-model", "claude-opus-5-5")
        page.click("#agent-form button")
        page.wait_for_selector(".keybox code")
        key = page.inner_text("#key")
        assert key.startswith("dev_")
        page.click("[data-nav=issues]"); page.click("[data-nav=agents]")
        page.wait_for_selector("td:text('claude-opus-5-5')")
        assert page.locator(".keybox").count() == 0 and key not in page.content()

        # 에이전트가 API로 계획서를 올리면 상세에 모델과 함께 보인다
        r = page.request.post(f"{BASE}/api/issues/NS-1/plans", data={"body": "v2 by agent"}, headers={"Authorization": f"Bearer {key}"})
        assert r.ok, r.text()
        page.goto(BASE + "/#/issue/NS-1"); page.wait_for_selector("text=v2 by agent")
        assert "claude-opus-5-5" in page.inner_text(".timeline")

        # 모바일 폭
        page.set_viewport_size({"width": 390, "height": 800})
        page.goto(BASE + "/#/issue/NS-1"); page.wait_for_selector("h1#title")
        assert page.evaluate("document.documentElement.scrollWidth") <= 391
        page.screenshot(path=str(shots / "issue_mobile.png"), full_page=True)

        # 밤/낮 — nightshift처럼 두 상태: 고른 적 없으면 운영체제를 따르고, 누르면 반대로 고정, 새로고침해도 유지
        bg = lambda: page.evaluate("getComputedStyle(document.body).backgroundColor")
        theme = lambda: page.evaluate("document.documentElement.dataset.theme || 'auto'")
        icon = lambda: page.get_attribute("#theme use", "href")
        page.set_viewport_size({"width": 1300, "height": 850})
        page.evaluate("localStorage.removeItem('dev.theme')"); page.reload(); page.wait_for_selector("h1#title")
        page.emulate_media(color_scheme="light")
        assert theme() == "auto" and icon() == "#i-moon"
        light_bg = bg()
        page.emulate_media(color_scheme="dark")
        page.wait_for_function("document.querySelector('#theme use').getAttribute('href') === '#i-sun'", timeout=3000)
        assert theme() == "auto"   # 운영체제를 따라 아이콘도
        dark_bg = bg(); assert dark_bg != light_bg
        page.click("#theme"); assert theme() == "light" and bg() == light_bg and icon() == "#i-moon"   # 밤이던 화면을 낮으로 고정
        page.reload(); page.wait_for_selector("h1#title")
        assert theme() == "light" and bg() == light_bg
        page.click("#theme"); assert theme() == "dark" and bg() == dark_bg and icon() == "#i-sun"
        page.emulate_media(color_scheme="light"); page.wait_for_timeout(400)
        page.screenshot(path=str(shots / "theme_dark.png"))
        page.click("#theme"); page.wait_for_timeout(400)

        # 헤더·탭 — 사용자 칩(이름·ADMIN), 탭 아이콘, 마우스를 올리면 제목이 펼쳐진다, 활성 탭 밑줄
        assert page.inner_text("#user-chip-name") == "admin"
        page.goto(BASE + "/#/"); page.wait_for_selector("tr.row")
        assert page.get_attribute('[data-nav="issues"]', "class") == "tab-btn active"
        label_w = lambda: page.evaluate("document.querySelector('[data-nav=board] .tab-label').getBoundingClientRect().width")
        assert label_w() < 1
        page.hover('[data-nav="board"]'); page.wait_for_timeout(400)
        assert label_w() > 20
        page.screenshot(path=str(shots / "header.png"), clip={"x": 0, "y": 0, "width": 1300, "height": 160})
        # 사용자 칩 → 메뉴 → 바깥 누르면 닫힘
        page.click("#user-chip"); assert page.is_visible("#user-menu")
        assert page.get_attribute("#open-nightshift", "href").startswith("http")
        page.screenshot(path=str(shots / "user_menu.png"), clip={"x": 900, "y": 0, "width": 400, "height": 200})
        page.mouse.click(600, 500); assert not page.is_visible("#user-menu")
        page.set_viewport_size({"width": 390, "height": 800}); page.wait_for_timeout(200)
        page.screenshot(path=str(shots / "header_mobile.png"), clip={"x": 0, "y": 0, "width": 390, "height": 300})
        assert page.evaluate("document.documentElement.scrollWidth") <= 391

        # Approve → Done이면 목록으로 돌아간다(DEV-5), 상태 칸에서 Closed를 골라도
        page.set_viewport_size({"width": 1300, "height": 850})
        page.request.post(f"{BASE}/api/issues/NS-1-1/status", data={"status": "in_review"}, headers={"X-Requested-With": "dev"})
        page.goto(BASE + "/#/issue/NS-1-1"); page.wait_for_selector("#approve")
        # Task는 Activity(댓글 입력 포함) 바로 뒤에 Result를 한 번만 출력한다.
        assert page.locator('#result').count() == 1
        assert page.locator('.detail > div > .panel > h2').evaluate_all("es => es.map(e => e.firstChild.textContent.trim())") == ['Description', 'Plan', 'Tasks', 'Activity', 'Result']
        assert page.locator('#result').evaluate("e => e.closest('.panel').previousElementSibling.contains(document.getElementById('comment'))")
        for scheme in ('light', 'dark'):
            page.evaluate("s => { localStorage.setItem('dev.theme', s); document.documentElement.dataset.theme = s; }", scheme)
            page.emulate_media(color_scheme=scheme)
            for width, tag in ((1300, 'desktop'), (390, 'mobile')):
                page.set_viewport_size({'width': width, 'height': 850})
                page.locator('#comment').focus()
                for target in ('#send-comment', '#approve', '#result-actions .complete-tree', '#request-changes', '#result-actions [data-to="closed"]'):
                    page.keyboard.press('Tab')
                    assert page.locator(target).evaluate('e => e === document.activeElement')
                    box = page.locator(target).bounding_box()
                    assert box and box['x'] >= 0 and box['x'] + box['width'] <= width + 1
                assert page.evaluate('document.documentElement.scrollWidth') <= width + 1
                page.screenshot(path=str(shots / f'task_result_{scheme}_{tag}.png'), full_page=True)
        page.set_viewport_size({'width': 1300, 'height': 850})
        page.emulate_media(color_scheme='light')
        page.evaluate("localStorage.setItem('dev.theme', 'light'); document.documentElement.dataset.theme = 'light'")
        page.click("#approve")
        page.wait_for_function("location.hash === '#/'")
        assert "NS-1-1을(를) 끝냈어요" in page.inner_text("#toast")
        page.wait_for_selector("tr.row")
        assert "NS-1-1" not in page.inner_text("table.issues")   # 기본 목록은 끝난 것을 숨긴다
        page.goto(BASE + "/#/issue/NS-1"); page.wait_for_selector("#status")
        # Closed는 사유를 묻는다(DEV-6) — 취소·빈칸이면 닫지 않고 원래 상태로
        answers[:] = [None]
        page.select_option("#status", "closed"); page.wait_for_timeout(300)
        assert page.input_value("#status") == "changes_requested" and "사유가 없어서" in page.inner_text("#toast")
        answers[:] = ["   "]
        page.select_option("#status", "closed"); page.wait_for_timeout(300)
        assert page.input_value("#status") == "changes_requested"
        assert page.request.get(f"{BASE}/api/issues/NS-1").json()["status"] == "changes_requested"
        answers[:] = ["NS-3과 중복"]
        page.select_option("#status", "closed")
        page.wait_for_function("location.hash === '#/'")
        ev = page.request.get(f"{BASE}/api/issues/NS-1").json()["events"][-1]
        assert ev["kind"] == "status" and ev["data"]["to"] == "closed" and ev["body"] == "NS-3과 중복", ev
        page.goto(BASE + "/#/issue/NS-1-1"); page.wait_for_selector("#status")
        page.select_option("#status", "in_progress")   # 끝내는 게 아니면 그 자리에 머문다
        page.wait_for_function("document.getElementById('status') && document.getElementById('status').value === 'in_progress'")
        assert page.evaluate("location.hash") == "#/issue/NS-1-1"
        # 검토 상태 외에는 Task Result를 표시하지 않는다.
        for status in ('backlog', 'triage', 'waiting', 'in_progress', 'changes_requested', 'on_hold', 'done', 'closed'):
            assert page.evaluate("s => resultHtml({status:s, parent_ref:'NS-1', children:[]})", status) == ''
        assert page.locator('#result').count() == 0

        # 목록 나눠 읽기(DEV-7) — 처음 50개, 끝까지 내리면 더 붙는다
        for n in range(110):
            page.request.post(f"{BASE}/api/issues", data={"project": "NS", "title": f"많은 이슈 {n}"}, headers={"X-Requested-With": "dev"})
        page.goto(BASE + "/#/"); page.wait_for_selector("tr.row")
        rows = lambda: page.locator("tr.row").count()
        page.wait_for_timeout(500)
        assert rows() == 50, rows()
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_function("document.querySelectorAll('tr.row').length === 100")
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_function("document.querySelectorAll('tr.row').length === 111")   # NS-1-1(in_progress) + 110
        refs = page.evaluate("[...document.querySelectorAll('tr.row')].map(r => r.dataset.ref)")
        assert len(refs) == len(set(refs))
        # 검색하면 처음부터, 검색 글칸 포커스 유지
        page.fill("#q", "많은 이슈 10")
        page.wait_for_function("document.querySelectorAll('tr.row').length === 11")   # 10, 100~109
        assert page.evaluate("document.activeElement.id") == "q"
        page.fill("#q", "")
        page.wait_for_function("document.querySelectorAll('tr.row').length === 50")

        # 제목 없이 본문만(DEV-8) — 만들어지고 "(제목 없음…)"으로 보인다
        page.goto(BASE + "/#/new"); page.wait_for_selector("#n-body")
        page.fill("#n-body", "제목 없이 쓴 요구")
        page.click("#new-form button[type=submit]")
        page.wait_for_function("location.hash.startsWith('#/issue/NS-')")
        assert "제목 없음" in page.inner_text("h1#title")

        # Create를 연달아 두 번 눌러도 한 번만 저장(DEV-12)
        page.goto(BASE + "/#/new"); page.wait_for_selector("#n-body")
        page.fill("#n-title", "두 번 누르기 시험"); page.fill("#n-body", "본문")
        page.evaluate("() => { const b = document.querySelector('#new-form button[type=submit]'); b.click(); b.click(); b.click(); }")
        page.wait_for_function("location.hash.startsWith('#/issue/NS-')")
        page.wait_for_timeout(500)
        same = [i for i in page.request.get(f"{BASE}/api/issues?q=두 번 누르기 시험").json()["issues"]]
        assert len(same) == 1, same

        # Issues 화면의 프로젝트 필터(DEV-11) — 탭 줄 필터와 같이 움직인다
        H = {"X-Requested-With": "dev"}
        page.request.post(f"{BASE}/api/projects", data={"key": "DEV", "name": "dev"}, headers=H)
        page.request.post(f"{BASE}/api/issues", data={"project": "DEV", "title": "dev 쪽 이슈"}, headers=H)
        page.evaluate("loadProjects()"); page.goto(BASE + "/#/"); page.wait_for_selector("#list-project")
        page.wait_for_selector("tr.row")
        page.select_option("#list-project", "DEV")
        page.wait_for_function("() => [...document.querySelectorAll('tr.row')].every(r => r.dataset.ref.startsWith('DEV-')) && document.querySelectorAll('tr.row').length === 1")
        assert page.input_value("#project-filter") == "DEV"
        page.select_option("#list-project", "")
        page.wait_for_function("() => [...document.querySelectorAll('tr.row')].some(r => r.dataset.ref.startsWith('NS-'))")

        # 계획서 결정(DEV-14) — 답 칸에 "정해야 할 것"이 인용되고, 조건부 승인 → 새 판이면 무효 표시 → 거절하면 닫혀 목록으로
        ref = page.request.post(f"{BASE}/api/issues", data={"project": "DEV", "title": "결정 화면", "status": "triage"}, headers=H).json()["ref"]
        page.request.post(f"{BASE}/api/issues/{ref}/plans", data={"body": "## 방향\n하나\n\n## 정해야 할 것\n1. 끌까 지울까(추천: 끄기)\n2. 제한 시간 15분?"}, headers=H)
        page.goto(BASE + f"/#/issue/{ref}"); page.wait_for_selector("#decision-actions")
        page.click("#decision-actions [data-verdict=approve_notes]")
        note = page.input_value("#decision-note")
        assert "> 1. 끌까 지울까(추천: 끄기)\n→ " in note and "2. 제한 시간 15분?" in note, note
        page.screenshot(path=str(shots / "decision_form.png"), full_page=True)
        page.fill("#decision-note", note + "끄기로")
        page.click("#decision-send"); page.wait_for_selector(".decision-state .status")
        assert "조건부 승인됨" in page.inner_text(".decision-state") and "끄기로" in page.inner_text(".decision-state")
        assert page.locator(".decision-state blockquote").count() == 2   # 인용이 그대로 보이지 않고 인용 블록으로(DEV-18)
        page.screenshot(path=str(shots / "decision_done.png"), full_page=True)
        page.request.post(f"{BASE}/api/issues/{ref}/plans", data={"body": "v2"}, headers=H)
        page.reload(); page.wait_for_selector("#decision-actions")   # 같은 주소로 goto하면 다시 그리지 않는다
        assert "무효" in page.inner_text(".decision-state")
        page.click("#decision-actions [data-verdict=reject]"); page.click("#decision-send")   # 메모 없이는 안 닫힌다
        page.wait_for_timeout(300); assert page.evaluate("location.hash") == f"#/issue/{ref}"
        page.fill("#decision-note", "필요 없어짐"); page.click("#decision-send")
        page.wait_for_function("location.hash === '#/'")
        assert page.request.get(f"{BASE}/api/issues/{ref}").json()["status"] == "closed"

        # 목록의 승인 배지와 "승인된 것만" 필터(DEV-17)
        ok = page.request.post(f"{BASE}/api/issues", data={"project": "DEV", "title": "승인된 이슈"}, headers=H).json()["ref"]
        page.request.post(f"{BASE}/api/issues/{ok}/plans", data={"body": "p"}, headers=H)
        page.request.post(f"{BASE}/api/issues/{ok}/decision", data={"verdict": "approve", "plan_version": 1}, headers=H)
        page.goto(BASE + "/#/"); page.reload(); page.wait_for_selector("tr.row")
        assert "승인됨" in page.inner_text(f'tr.row[data-ref="{ok}"]')
        page.check("#only-approved")
        page.wait_for_function("() => document.querySelectorAll('tr.row').length === 1")
        assert page.get_attribute("tr.row", "data-ref") == ok
        page.screenshot(path=str(shots / "list_approved.png"), full_page=True)
        page.uncheck("#only-approved")

        # 선행 Task 표시(DEV-21)
        pr = page.request.post(f"{BASE}/api/issues", data={"project": "DEV", "title": "선후 시험", "status": "triage"}, headers=H).json()["ref"]
        page.request.post(f"{BASE}/api/issues/{pr}/plans", data={"body": "## Tasks\n1. 먼저\n2. 나중 | 선행: 1"}, headers=H)
        page.request.post(f"{BASE}/api/issues/{pr}/decision", data={"verdict": "approve", "plan_version": 1}, headers=H)
        page.goto(BASE + f"/#/issue/{pr}"); page.reload(); page.wait_for_selector("tr.row")
        # 승인된 부모도 변경된 요구사항으로 새 계획서 검토를 요청할 수 있다.
        for scheme in ('light', 'dark'):
            for width, tag in ((1300, 'desktop'), (390, 'mobile')):
                page.evaluate("s => { localStorage.setItem('dev.theme', s); document.documentElement.dataset.theme = s; }", scheme)
                page.set_viewport_size({'width': width, 'height': 850})
                for selector in ('#ask-review', '#ask-codex-review'):
                    assert page.locator(selector).is_visible() and page.locator(selector).is_enabled()
                assert page.evaluate('document.documentElement.scrollWidth') <= width + 1
                page.screenshot(path=str(shots / f'approved_review_{scheme}_{tag}.png'), full_page=True)
        page.set_viewport_size({'width': 1300, 'height': 850})
        page.evaluate("localStorage.setItem('dev.theme', 'light'); document.documentElement.dataset.theme = 'light'")
        row2 = page.locator("tr.row", has_text="나중")
        assert "대기 · 선행:" in row2.inner_text()
        page.screenshot(path=str(shots / "tasks_deps.png"), full_page=True)
        row2.locator("a").click(); page.wait_for_function("() => document.querySelector('h1#title').innerText === '먼저'")

        # 실행 기록(DEV-29) — 이슈 화면 옆줄에 상태·시간·토큰·로그 파일
        import sqlite3
        db = sqlite3.connect(tmp / "data" / "dev.db")
        iid = db.execute("SELECT id FROM issues WHERE title='선후 시험'").fetchone()[0]
        db.executemany("INSERT INTO runs(issue_id, mode, status, actor, started_at, ended_at, exit_code, log_file, input_tokens, output_tokens, cost_usd, note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", [
            (iid, "review", "ok", "human:admin", "2026-09-30T12:00:00+00:00", "2026-09-30T12:03:20+00:00", 0, f"{pr}-20260930-120000.log", 18500, 2300, 0.42, ""),
            (iid, "review", "failed", "human:admin", "2026-09-30T12:10:00+00:00", "2026-09-30T12:10:05+00:00", 3, f"{pr}-20260930-121000.log", None, None, None, ""),
            (iid, "review", "orphaned", "human:admin", "2026-09-30T12:20:00+00:00", "2026-09-30T12:25:00+00:00", None, "", None, None, None, "서버가 다시 떠서 끊겼어요")])
        db.commit(); db.close()
        assert len(page.request.get(f"{BASE}/api/issues/{pr}/runs").json()["runs"]) == 3
        assert "runs" not in page.request.get(f"{BASE}/api/issues/{pr}").json()
        page.goto(BASE + f"/#/issue/{pr}"); page.reload(); page.wait_for_selector(".runs li")
        txt = page.inner_text(".runs")
        assert "끊김" in txt and "실패" in txt and "3분 20초" in txt and "토큰 18.5k → 2.3k · $0.42" in txt and "서버가 다시 떠서" in txt, txt
        assert page.locator(".runs li").first.inner_text().startswith("끊김")   # 최근 것이 먼저
        for scheme in ("light", "dark"):
            for w, h, tag in ((1300, 850, "desktop"), (390, 800, "mobile")):
                page.emulate_media(color_scheme=scheme); page.set_viewport_size({"width": w, "height": h}); page.wait_for_timeout(300)
                assert page.evaluate("document.documentElement.scrollWidth") <= w + 1
                page.screenshot(path=str(shots / f"runs_{scheme}_{tag}.png"), full_page=True)
        page.set_viewport_size({"width": 1300, "height": 850})
        page.goto(BASE + "/#/issue/NS-1"); page.wait_for_selector("text=아직 실행 기록이 없어요")

        # Task 실행 맡기기(DEV-23·32) — 눌러서 실제로 돌리지는 않는다(claude 비용). 버튼 상태·이유·브랜치 요약만 본다.
        gitrepo = tmp / "gitrepo"; gitrepo.mkdir()
        g = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=gitrepo, check=True, capture_output=True)
        g("init", "-q", "-b", "main"); (gitrepo / "a.txt").write_text("a\n"); g("add", "."); g("commit", "-qm", "init")
        g("checkout", "-q", "-b", "relay/DEV-4-1"); (gitrepo / "new_feature.py").write_text("x = 1\n"); g("add", "."); g("commit", "-qm", "feat (DEV-4-1)"); g("checkout", "-q", "main")
        page.request.patch(f"{BASE}/api/projects/DEV", data={"local_path": str(gitrepo)}, headers=H)
        t5, t6 = f"{BASE}/api/issues/DEV-4-1", f"{BASE}/api/issues/DEV-4-2"
        assert page.request.get(t5).json()["execute"]["blocked"] is None and "DEV-4-1" in page.request.get(t6).json()["execute"]["blocked"]
        assert page.request.get(f"{BASE}/api/issues/{pr}").json()["execute"] is None     # Task가 아니면 없음
        for scheme in ("light", "dark"):
            for w, h, tag in ((1300, 850, "desktop"), (390, 800, "mobile")):
                page.emulate_media(color_scheme=scheme); page.set_viewport_size({"width": w, "height": h})
                page.goto(BASE + "/#/issue/DEV-4-1"); page.reload(); page.wait_for_selector("#ask-execute")
                page.evaluate("scheme => { localStorage.setItem('dev.theme', scheme); document.documentElement.dataset.theme = scheme; }", scheme)
                page.wait_for_timeout(300)
                assert page.is_enabled("#ask-execute") and "비용 상한 $2" in page.inner_text(".exec")
                assert page.is_enabled("#ask-codex-execute") and "Codex: 시간 제한 30분 · 비용 상한 없음" in page.inner_text(".exec")
                assert page.locator('#ask-execute .brand-icon use').get_attribute('href') == '#i-claude'
                assert page.locator('#ask-codex-execute .brand-icon use').get_attribute('href') == '#i-openai'
                txt = page.inner_text(".exec .branch"); assert "relay/DEV-4-1" in txt and "커밋 1개" in txt and "new_feature.py" in txt and "git merge relay/DEV-4-1" in txt, txt
                assert page.evaluate("document.documentElement.scrollWidth") <= w + 1
                page.screenshot(path=str(shots / f"execute_{scheme}_{tag}.png"), full_page=True)
        page.set_viewport_size({"width": 1300, "height": 850}); page.emulate_media(color_scheme="light")
        page.goto(BASE + "/#/issue/DEV-4-2"); page.reload(); page.wait_for_selector("#ask-execute")
        check_execute_buttons(page, shots, answers)
        page.goto(BASE + "/#/issue/DEV-4-2"); page.reload(); page.wait_for_selector("#ask-execute")
        # 선행 대기는 눌러 둘 수 있다(DEV-43) — 줄에서 기다린다
        assert page.is_enabled("#ask-execute") and "선행 Task(DEV-4-1)가 done이 되어야 해요" in page.inner_text(".exec") and "차례로" in page.inner_text(".exec")
        page.goto(BASE + f"/#/issue/{pr}"); page.reload(); page.wait_for_selector("#ask-review", state="attached"); assert page.locator("#ask-execute").count() == 0
        # 부모 화면에서 바로 실행(선행이 안 끝난 DEV-4-2도 눌러 두면 줄에 선다, 삭제는 아이콘 하나)
        page.wait_for_selector("tr.row"); n = page.locator(".run-task").count(); assert n == 4, n
        check_task_actions(page, pr, shots, answers, asked)
        assert page.locator("#delete svg").count() == 1 and not page.text_content("#delete").strip()

        # 대기열(DEV-43) — 진짜 claude를 돌리지 않게 도는 run 하나를 심어 두고 줄을 DB에 직접 넣는다
        db = sqlite3.connect(tmp / "data" / "dev.db")
        i41, i42 = (page.request.get(u).json()["id"] for u in (t5, t6))
        db.execute("INSERT INTO runs(issue_id, mode, status, actor, started_at) VALUES(?, 'review', 'running', 'human:admin', '2026-10-02T00:00:00+00:00')", (iid,))
        db.executemany("INSERT INTO jobs(issue_id, mode, actor, status, note, created_at) VALUES(?, 'execute', 'human:admin', 'queued', ?, '2026-10-02T00:00:00+00:00')",
                       [(i41, ""), (i42, "선행 Task(DEV-4-1)가 done이 되어야 해요.")])
        db.commit()
        # 이전 버전 대기 항목도 펌프에서 Waiting으로 정합화한다.
        page.request.post(f"{BASE}/api/issues/DEV-4-1/execute", headers=H)
        assert page.request.get(t5).json()["status"] == "waiting"
        assert page.request.get(t6).json()["status"] == "waiting"
        for scheme in ("light", "dark"):
            page.evaluate("s => { localStorage.setItem('dev.theme', s); document.documentElement.dataset.theme = s; }", scheme)
            for w, h, tag in ((1300, 850, "desktop"), (390, 800, "mobile")):
                page.emulate_media(color_scheme=scheme); page.set_viewport_size({"width": w, "height": h})
                page.goto(BASE + "/#/issue/DEV-4-2"); page.reload(); page.wait_for_selector(".exec .queued")
                assert page.input_value('#status') == 'waiting'
                assert 'Waiting' in page.inner_text('#stage')
                assert "대기 2번째" in page.inner_text(".exec") and "선행 Task" in page.inner_text(".exec") and page.locator("#ask-execute").count() == 0
                assert "대기 2번째" in page.inner_text("#stage")
                assert page.evaluate("document.documentElement.scrollWidth") <= w + 1
                page.screenshot(path=str(shots / f"queue_issue_{scheme}_{tag}.png"), full_page=True)
                # 부모 Tasks 표(DEV-45) — 줄에 선 Task는 비활성 "실행 대기 중"
                page.goto(BASE + f"/#/issue/{pr}"); page.reload(); page.wait_for_selector("tr.row")
                for r in ("DEV-4-1", "DEV-4-2"):
                    b = page.locator(f'.run-task[data-ref="{r}"]')
                    assert b.is_disabled() and b.get_attribute("aria-label") == "Claude 실행 대기 중", r
                    assert b.locator('.brand-icon use').get_attribute('href') == '#i-claude'
                    # 모바일은 아이콘만(DEV-46) — 글자가 표를 넓혀 가로 스크롤을 만들었다
                    assert ("실행 대기 중" in b.inner_text()) == (tag == "desktop"), r
                assert page.evaluate("document.documentElement.scrollWidth") <= w + 1
                page.screenshot(path=str(shots / f"queue_tasks_{scheme}_{tag}.png"), full_page=True)
                page.goto(BASE + "/#/"); page.reload(); page.wait_for_selector(".jobs summary")
                assert "Agent 대기 2건" in page.inner_text(".jobs summary")
                page.click(".jobs summary"); assert page.locator(".jobs li").count() == 2
                assert page.evaluate("document.documentElement.scrollWidth") <= w + 1
                page.screenshot(path=str(shots / f"queue_list_{scheme}_{tag}.png"), full_page=True)
                page.click('[data-st="waiting"]')
                page.wait_for_function("document.querySelector('#list-body').getAttribute('aria-busy') === 'false'")
                assert page.locator('tr.row').count() == 2
                assert all('Waiting' in text for text in page.locator('tr.row').all_inner_texts())
                page.screenshot(path=str(shots / f"waiting_filter_{scheme}_{tag}.png"), full_page=True)
                page.click('[data-st="waiting"]')
                page.goto(BASE + '/#/board'); page.wait_for_selector('[data-col="waiting"] .card')
                assert page.locator('[data-col="waiting"] .card').count() == 2
                assert page.evaluate("document.documentElement.scrollWidth") <= w + 1
                page.screenshot(path=str(shots / f"waiting_board_{scheme}_{tag}.png"), full_page=True)
                page.goto(BASE + '/#/'); page.wait_for_selector('.jobs summary'); page.click('.jobs summary')
        assert page.request.get(f"{BASE}/api/issues/{pr}").json()["children"][0]["job"]["mode"] == "execute"
        page.set_viewport_size({"width": 1300, "height": 850}); page.emulate_media(color_scheme="light")
        page.click(".jobs li:first-child .cancel-job"); page.wait_for_function("document.getElementById('toast').innerText.includes('대기를 취소했어요')")
        page.wait_for_function("() => document.querySelector('.jobs summary') && document.querySelector('.jobs summary').innerText.includes('1건')")
        page.goto(BASE + f"/#/issue/{pr}"); page.reload(); page.wait_for_selector("tr.row")
        b1 = page.locator('.run-task[data-ref="DEV-4-1"][data-provider="claude"]')
        b2 = page.locator('.run-task[data-ref="DEV-4-2"]')
        assert b1.is_enabled() and "Claude" in b1.inner_text() and b2.is_disabled() and "실행 대기 중" in b2.inner_text()
        page.goto(BASE + "/#/issue/DEV-4-2"); page.reload(); page.wait_for_selector(".exec .queued")
        assert "대기 1번째" in page.inner_text(".exec")
        page.click(".exec .cancel-job"); page.wait_for_selector("#ask-execute")
        assert page.request.get(f"{BASE}/api/jobs").json()["jobs"] == []
        page.goto(BASE + f"/#/issue/{pr}"); page.reload(); page.wait_for_selector("tr.row")
        assert page.locator(".run-task:not([disabled])").count() == 4 and "실행 대기 중" not in str(page.locator("tr.row").all_inner_texts())
        for provider in ('claude', 'codex'):
            page.click(f'.run-task[data-ref="DEV-4-1"][data-provider="{provider}"]')
            page.wait_for_selector('.task-queued[data-ref="DEV-4-1"]')
            queued = page.request.get(f"{BASE}/api/jobs").json()['jobs']
            assert len(queued) == 1 and queued[0]['provider'] == provider and queued[0]['ref'] == 'DEV-4-1'
            assert page.locator('.run-task[data-ref="DEV-4-1"]').count() == 1
            assert page.locator('.task-queued').get_attribute('data-provider') == provider
            page.request.delete(f"{BASE}/api/jobs/{queued[0]['id']}", headers=H)
            page.reload(); page.wait_for_selector('.run-task[data-ref="DEV-4-1"]:not([disabled])')
        # 바쁠 때도 검토 버튼은 눌러 둘 수 있다
        fresh = page.request.post(f"{BASE}/api/issues", data={"project": "DEV", "title": "바쁠 때 검토"}, headers=H).json()["ref"]
        page.goto(BASE + f"/#/issue/{fresh}"); page.reload(); page.wait_for_selector("#ask-review")
        assert page.is_enabled("#ask-review") and "차례로" in page.inner_text(".side")
        assert page.is_enabled("#ask-codex-review")
        check_review_buttons(page, fresh, shots)
        check_edit_saving(page, shots)
        page.goto(BASE + f'/#/issue/{fresh}'); page.wait_for_selector('#ask-codex-review')
        for scheme in ("light", "dark"):
            page.evaluate("scheme => { localStorage.setItem('dev.theme', scheme); document.documentElement.dataset.theme = scheme; }", scheme)
            page.emulate_media(color_scheme=scheme)
            for w, tag in ((1300, "desktop"), (390, "mobile")):
                page.set_viewport_size({"width": w, "height": 850})
                page.wait_for_timeout(300)
                assert page.evaluate("document.documentElement.scrollWidth") <= w + 1
                page.screenshot(path=str(shots / f"codex_review_{scheme}_{tag}.png"), full_page=True)
        page.click("#ask-codex-review")
        page.wait_for_selector(".side .queued")
        queued = page.request.get(f"{BASE}/api/jobs").json()["jobs"]
        assert queued[0]["provider"] == "codex" and "Codex" in page.inner_text(".side .queued")
        page.click(".side .cancel-job")
        page.wait_for_selector("#ask-codex-review")
        page.goto(BASE + "/#/issue/DEV-4-1"); page.reload(); page.wait_for_selector("#ask-codex-execute")
        page.click("#ask-codex-execute")
        page.wait_for_selector(".exec .queued")
        queued = page.request.get(f"{BASE}/api/jobs").json()["jobs"]
        assert queued[0]["provider"] == "codex" and queued[0]["mode"] == "execute"
        page.click(".exec .cancel-job")
        page.wait_for_selector("#ask-codex-execute")
        page.set_viewport_size({"width": 1300, "height": 850})
        page.emulate_media(color_scheme="light")
        page.evaluate("localStorage.setItem('dev.theme', 'light'); document.documentElement.dataset.theme = 'light'")
        db.execute("UPDATE runs SET status='ok' WHERE status='running'"); db.commit(); db.close()

        # 결과 거절 → 닫히고 사유가 남는다(DEV-16)
        rj = page.request.post(f"{BASE}/api/issues", data={"project": "DEV", "title": "결과 거절"}, headers=H).json()["ref"]
        page.request.post(f"{BASE}/api/issues/{rj}/status", data={"status": "in_review"}, headers=H)
        page.goto(BASE + f"/#/issue/{rj}"); page.wait_for_selector("#result-actions")
        page.click("#result-actions [data-to=closed]"); page.screenshot(path=str(shots / "result_form.png"), full_page=True)
        page.fill("#result-note", "방향이 달라서"); page.click("#result-send")
        page.wait_for_function("location.hash === '#/'")
        got = page.request.get(f"{BASE}/api/issues/{rj}").json()
        assert got["status"] == "closed" and "방향이 달라서" in str(got["events"]), got

        # 전체 완료(DEV-44) — Task 화면에서 눌러 상위·형제 Task까지 Done. 확인 창에 닫힐 목록과 주의할 Task가 나온다.
        top = page.request.post(f"{BASE}/api/issues", data={"project": "DEV", "title": "묶음 상위"}, headers=H).json()["ref"]
        ta, tb = (page.request.post(f"{BASE}/api/issues", data={"project": "DEV", "title": t, "parent": top}, headers=H).json()["ref"] for t in ("묶음 하나", "묶음 둘"))
        page.request.post(f"{BASE}/api/issues/{ta}/status", data={"status": "in_review"}, headers=H)
        for scheme in ("light", "dark"):
            for w, h, tag in ((1300, 850, "desktop"), (390, 800, "mobile")):
                page.emulate_media(color_scheme=scheme); page.set_viewport_size({"width": w, "height": h})
                page.goto(BASE + f"/#/issue/{ta}"); page.reload(); page.wait_for_selector(".complete-tree")
                assert page.locator(".complete-tree").count() == 2   # Result 패널 + Status 옆
                assert page.evaluate("document.documentElement.scrollWidth") <= w + 1
                page.screenshot(path=str(shots / f"complete_tree_{scheme}_{tag}.png"), full_page=True)
        page.set_viewport_size({"width": 1300, "height": 850}); page.emulate_media(color_scheme="light")
        answers.append(None); page.click(".side .complete-tree"); page.wait_for_timeout(500)   # 취소하면 그대로
        assert page.request.get(f"{BASE}/api/issues/{top}").json()["status"] != "done"
        asked.clear(); page.click(".side .complete-tree"); page.wait_for_function("location.hash === '#/'")
        assert top in asked[0] and ta in asked[0] and tb in asked[0] and "주의" in asked[0] and f"{tb} — Backlog" in asked[0], asked
        assert all(page.request.get(f"{BASE}/api/issues/{r}").json()["status"] == "done" for r in (top, ta, tb))

        check_quick_actions(page, shots)
        # 로그아웃(사용자 메뉴 안)
        page.click("#user-chip"); page.click("#logout"); page.wait_for_selector("#login:not([hidden])")
        assert page.locator('#open-jupyter').get_attribute('hidden') is not None
        assert not errs, errs
    print("OK")
finally:
    for pr in procs:
        pr.terminate()
