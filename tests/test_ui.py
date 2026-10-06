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


def check_attachments(page, shots):
    """혼합 첨부·실패 재시도·취소·미리보기와 네 화면을 확인한다."""
    import base64, io, wave
    H = {'X-Requested-With': 'dev'}
    assert page.request.post(BASE + '/api/projects', headers=H, data={'key':'FILES','name':'첨부 검사'}).ok
    page.reload(); page.wait_for_selector('#project-filter'); page.select_option('#project-filter', 'FILES')
    uploads, posts = [], []
    def record(r):
        if r.method == 'POST' and r.url == BASE + '/api/attachments': uploads.append(r)
        if r.method == 'POST' and r.url == BASE + '/api/issues': posts.append(r)
    page.on('request', record)
    def new():
        page.goto(BASE + '/#/new'); page.wait_for_selector('#n-files')
        page.fill('#n-title', '혼합 첨부'); page.fill('#n-body', '기존 Description https://example.com/body')
    def ready(n):
        page.wait_for_function('(n) => document.querySelectorAll("#n-attachments [data-remove]").length === n && !document.querySelector("#new-form [type=submit]").disabled', arg=n)
    png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(b'\x00\x00' * 800)
    # 브라우저가 지원하는 실제 영상으로 재생을 확인한다.
    video = bytes(page.evaluate('''async () => {
        const canvas = document.createElement('canvas'); canvas.width = canvas.height = 32;
        const stream = canvas.captureStream(10), recorder = new MediaRecorder(stream, {mimeType:'video/webm'});
        const chunks = [];
        recorder.ondataavailable = e => chunks.push(e.data);
        const done = new Promise(resolve => recorder.onstop = resolve);
        recorder.start();
        const timer = setInterval(() => {
            const ctx = canvas.getContext('2d'); ctx.fillStyle = `rgb(${Math.floor(Math.random()*255)},0,0)`; ctx.fillRect(0,0,32,32);
        },50);
        await new Promise(resolve => setTimeout(resolve,600)); clearInterval(timer); recorder.stop(); await done;
        stream.getTracks().forEach(t => t.stop());
        return Array.from(new Uint8Array(await new Blob(chunks).arrayBuffer()));
    }'''))
    payloads = [{'name':name, 'mimeType':mime, 'buffer':data} for name,mime,data in [
        ('pixel.png','image/png',png), ('movie.webm','video/webm',video),
        ('sound.wav','audio/wav',buf.getvalue()), ('note.md','text/markdown',b'<script>window.attachmentXSS=1</script>\n' + b'x' * 70000),
        ('data.json','application/json',b'{"ok":true}')]]
    pixel = tmp / 'pixel.png'; pixel.write_bytes(png)
    new()
    def fail_upload(route): route.fulfill(status=500,json={'detail':'업로드 검사 실패'})
    page.route('**/api/attachments',fail_upload)
    page.set_input_files('#n-files',str(pixel)); page.wait_for_selector('[data-retry]')
    assert page.locator('#new-form [type=submit]').is_disabled()
    page.unroute('**/api/attachments',fail_upload)
    page.locator('[data-retry]').focus(); page.keyboard.press('Enter'); ready(1)
    page.set_input_files('#n-files',str(pixel)); ready(1)
    assert len(uploads) == 2
    page.set_input_files('#n-files',payloads[1:]); ready(5)
    page.fill('#n-url','https://example.com/reference'); page.locator('#add-url').focus(); page.keyboard.press('Enter'); ready(6)
    page.fill('#n-url','https://example.com/reference'); page.click('#add-url'); ready(6)
    def fail_publish(route): route.fulfill(status=500,json={'detail':'발행 검사 실패'})
    page.route('**/api/issues',fail_publish)
    page.click('#new-form [type=submit]'); page.wait_for_function('document.querySelector("#attachment-status").textContent.includes("입력과 첨부")')
    assert page.input_value('#n-body').startswith('기존 Description')
    ready(6); assert len(uploads) == 6
    page.unroute('**/api/issues',fail_publish)
    before = len(posts)
    page.locator('#new-form [type=submit]').evaluate('e => {e.click();e.click()}')
    page.wait_for_selector('.attachments'); assert len(posts) == before + 1
    ref = page.url.split('/issue/')[-1]
    assert len(page.request.get(BASE + '/api/issues/' + ref).json()['attachments']) == 6
    page.reload(); page.wait_for_selector('.attachments')
    assert page.locator('.attachment').count() == 6
    assert page.locator('#body a').get_attribute('href') == 'https://example.com/body'
    page.locator('.attachment').filter(has_text='note.md').locator('summary').click()
    page.wait_for_function("Array.from(document.querySelectorAll('[data-text-url] pre')).some(e => e.textContent.includes('<script>'))")
    assert page.evaluate('window.attachmentXSS') is None
    assert len(page.locator('.attachment').filter(has_text='note.md').locator('pre').inner_text()) < 66000
    page.locator('.attachment').filter(has_text='data.json').locator('summary').click()
    page.wait_for_function('Array.from(document.querySelectorAll("[data-text-url] pre")).some(e => e.textContent.includes("ok"))')
    assert page.locator('video[controls]').count() == page.locator('audio[controls]').count() == 1
    page.wait_for_function('document.querySelector(".attachment img").naturalWidth === 1')
    page.wait_for_function('document.querySelector("audio").readyState >= 1')
    page.locator('audio').evaluate('e => e.play()'); page.wait_for_function('!document.querySelector("audio").paused')
    page.wait_for_function('document.querySelector("video").readyState >= 1')
    page.locator('video').evaluate('e => {e.loop = true; return e.play()}'); page.wait_for_function('!document.querySelector("video").paused')
    with page.expect_download() as dl: page.locator('.attachment').filter(has_text='pixel.png').locator('a[download]').click()
    assert dl.value.suggested_filename == 'pixel.png'
    for scheme in ('light','dark'):
        page.evaluate('s => {localStorage.setItem("dev.theme",s);document.documentElement.dataset.theme=s}',scheme)
        page.emulate_media(color_scheme=scheme)
        for width in (1300,390):
            page.set_viewport_size({'width':width,'height':850})
            assert page.evaluate('document.documentElement.scrollWidth') <= width + 1
            page.screenshot(path=str(shots / f'attachments_detail_{scheme}_{width}.png'),full_page=True)
            new()
            page.fill('#n-url','https://example.com/' + 'long' * 50); page.click('#add-url'); ready(1)
            assert page.evaluate('document.documentElement.scrollWidth') <= width + 1
            page.screenshot(path=str(shots / f'attachments_new_{scheme}_{width}.png'),full_page=True)
            # 취소하면 임시 URL도 삭제한다.
            with page.expect_request(lambda r: r.method == 'DELETE' and '/api/attachments/' in r.url) as deleted:
                page.click('#cancel-new')
            aid = deleted.value.url.split('/')[-1]
            page.wait_for_function('location.hash === "#/"')
            for _ in range(30):
                if page.request.get(BASE + '/api/attachments/' + aid + '/content').status == 404: break
                page.wait_for_timeout(100)
            else: raise AssertionError('취소한 임시 첨부가 남았어요')
            page.goto(BASE + '/#/issue/' + ref); page.wait_for_selector('.attachments')
    page.remove_listener('request',record)
    token = page.request.get(BASE + '/api/projects/FILES/delete-check').json()['confirmation_token']
    assert page.request.delete(BASE + '/api/projects/FILES',headers=H,data={'confirmation_token':token}).ok
    page.set_viewport_size({'width':1300,'height':850}); page.emulate_media(color_scheme='light')
    page.evaluate('localStorage.setItem("dev.theme","light");document.documentElement.dataset.theme="light"')
    page.goto(BASE + '/#/projects'); page.reload(); page.wait_for_selector('#project-filter'); page.select_option('#project-filter','')


def check_published_notice(page, shots):
    """발행 프로젝트의 최신 설정과 실패·지연을 유료 작업 없이 확인한다."""
    H = {'X-Requested-With': 'dev'}
    assert page.request.post(BASE + '/api/projects', headers=H, data={'key':'NOTICE', 'name':'발행 안내 검사'}).ok
    assert page.request.post(BASE + '/api/projects', headers=H, data={'key':'SELECTED', 'name':'목록 선택 검사'}).ok
    page.reload(); page.wait_for_selector('#project-filter')
    page.select_option('#project-filter', 'SELECTED')
    manual = '이슈를 발행했어요 — Claude나 Codex에게 검토를 맡길 수 있어요.'
    auto = '이슈를 발행했어요 — 잠시 후 에이전트가 계획서를 작성해요.'
    neutral = '이슈를 발행했어요.'
    requests, posts = [], []
    mode = 'on'
    pending = []
    def settings(route):
        requests.append(route.request.url)
        if mode == 'delay':
            pending.append(route); return
        if mode == 'fail':
            route.fulfill(status=500, json={'detail':'설정 조회 실패'}); return
        route.fulfill(json={'auto_review': mode == 'on'})
    def record(request):
        if request.method == 'POST' and request.url == BASE + '/api/issues': posts.append(request)
    page.route('**/api/projects/*/auto-settings', settings)
    page.on('request', record)
    def publish(status='backlog', labels='', parent=''):
        before = len(posts)
        page.goto(BASE + '/#/new' + ('?parent=' + parent if parent else ''))
        page.wait_for_selector('#new-form')
        if not parent: page.select_option('#n-project', 'NOTICE')
        page.select_option('#n-status', status)
        page.fill('#n-labels', labels); page.fill('#n-title', '발행 안내 검사')
        page.click('#new-form [type=submit]')
        page.wait_for_selector('h1#title')
        assert '/#/issue/NOTICE-' in page.url
        assert len(posts) == before + 1
        return page.url.split('/issue/')[-1]
    def notice(text):
        page.wait_for_function('text => !document.querySelector("#toast").hidden && document.querySelector("#toast").textContent === text', arg=text)
    try:
        parent = publish(); notice(auto)
        assert requests[-1].endswith('/NOTICE/auto-settings')
        mode = 'off'; publish(); notice(manual)
        mode = 'on'
        for status, labels, task in [('triage','',''), ('backlog','goal',''), ('backlog','',parent)]:
            before = len(requests)
            publish(status, labels, task); notice(manual)
            assert len(requests) == before
        mode = 'fail'; publish(); notice(neutral)
        mode = 'delay'; publish(); notice(neutral)
        page.wait_for_timeout(1000)
        assert page.inner_text('#toast') == neutral and len(pending) == 1
        pending.pop().fulfill(json={'auto_review':True})
        page.wait_for_timeout(100)
        assert page.inner_text('#toast') == neutral
        mode = 'on'
        for scheme in ('light','dark'):
            page.evaluate('s => {localStorage.setItem("dev.theme",s);document.documentElement.dataset.theme=s}', scheme)
            page.emulate_media(color_scheme=scheme)
            for width in (1300,390):
                page.set_viewport_size({'width':width,'height':850})
                publish(); notice(auto)
                assert page.evaluate('document.documentElement.scrollWidth') <= width + 1
                assert page.locator('#toast').evaluate('e => e.scrollWidth <= e.clientWidth && e.scrollHeight <= e.clientHeight')
                page.screenshot(path=str(shots / f'published_notice_{scheme}_{width}.png'), full_page=True)
    finally:
        page.unroute('**/api/projects/*/auto-settings', settings)
        page.remove_listener('request', record)
        page.set_viewport_size({'width':1300,'height':850})
        page.evaluate('localStorage.setItem("dev.theme","light");document.documentElement.dataset.theme="light"')
        page.emulate_media(color_scheme='light')
        token = page.request.get(BASE + '/api/projects/NOTICE/delete-check').json()['confirmation_token']
        assert page.request.delete(BASE + '/api/projects/NOTICE', headers=H, data={'confirmation_token':token}).ok
        assert page.request.delete(BASE + '/api/projects/SELECTED', headers=H).ok
        page.goto(BASE + '/#/projects')
        page.reload(); page.wait_for_selector('#project-filter')
        page.select_option('#project-filter', '')


def check_issue_types(page, shots):
    """복수 선택·관리·실패 재시도와 네 화면의 배치를 확인한다."""
    H = {'X-Requested-With': 'dev'}
    assert page.request.post(BASE + '/api/projects', headers=H, data={'key':'TYPES', 'name':'종류 검사'}).ok
    page.reload(); page.wait_for_selector('#project-filter'); page.select_option('#project-filter', 'TYPES')
    catalog = page.request.get(BASE + '/api/issue-types').json()['types']
    a, b = catalog[:2]
    page.goto(BASE + '/#/new'); page.wait_for_selector('[data-type]')
    page.fill('#n-title', '복수 종류 UI 검사')
    page.locator(f'[data-type="{a["id"]}"]').focus(); page.keyboard.press('Space')
    page.locator(f'[data-type="{b["id"]}"]').focus(); page.keyboard.press('Enter')
    assert page.locator('[data-type][aria-pressed="true"]').count() == 2
    page.click('#new-form [type=submit]'); page.wait_for_selector('#save-types')
    ref = page.locator('.detail > div > .ref').inner_text().strip()
    get = lambda: page.request.get(BASE + '/api/issues/' + ref).json()
    assert get()['type_ids'] == [a['id'], b['id']]
    page.click(f'[data-type="{b["id"]}"]'); page.click('#save-types')
    page.wait_for_function("document.querySelectorAll('[data-type][aria-pressed=true]').length === 1 && !document.querySelector('#save-types').disabled")
    assert get()['type_ids'] == [a['id']]
    def fail(route):
        route.fulfill(status=500, content_type='application/json', body='{"detail":"저장 실패"}')
    page.route('**/api/issues/' + ref, fail)
    page.click(f'[data-type="{b["id"]}"]'); page.click('#save-types')
    page.wait_for_selector('#types-status:text("다시 시도")')
    assert page.locator(f'[data-type="{b["id"]}"]').get_attribute('aria-pressed') == 'true'
    assert page.locator('#save-types').is_enabled()
    page.unroute('**/api/issues/' + ref, fail)
    page.click('#save-types'); page.wait_for_function("document.querySelector('#types-status').textContent === ''")
    assert len(get()['types']) == 2
    # 유료 검토 없이 자동 분류된 API 응답을 모의한다.
    automatic = get()
    for t in automatic['types']: t['source'] = 'agent'
    page.route('**/api/issues/' + ref, lambda route: route.fulfill(json=automatic))
    page.reload(); page.wait_for_selector('text=자동 분류 결과예요.')
    page.unroute('**/api/issues/' + ref)
    page.goto(BASE + '/#/new'); page.wait_for_selector('#new-form')
    page.fill('#n-title', '빈 종류 검사'); page.click('#new-form [type=submit]'); page.wait_for_selector('#save-types')
    empty_ref = page.locator('.detail > div > .ref').inner_text().strip()
    empty = page.request.get(BASE + '/api/issues/' + empty_ref).json()
    assert empty['type_ids'] == [] and empty['plan'] is None and not empty['job']
    assert '미분류' in page.locator('.byline').inner_text()
    page.goto(BASE + '/#/settings'); page.wait_for_selector('a[href="#/settings/types"]'); page.click('a[href="#/settings/types"]')
    page.wait_for_selector('#type-create'); page.fill('#type-create input', '추가 종류 <검사>'); page.click('#type-create button')
    page.wait_for_selector('.type-row input[value="추가 종류 <검사>"]')
    added = page.request.get(BASE + '/api/issue-types').json()['types'][-1]
    row = page.locator(f'.type-row[data-id="{added["id"]}"]')
    row.locator('input').fill('긴 종류 이름 ' + '테스트 ' * 12); row.locator('[type=submit]').click()
    page.wait_for_selector('#type-status:text("저장했어요")')
    row = page.locator(f'.type-row[data-id="{a["id"]}"]'); row.locator('input').fill('이름 수정 확인'); row.locator('[type=submit]').click()
    page.wait_for_function("document.querySelector('.type-row input').value === '이름 수정 확인' && document.querySelector('.type-row').getAttribute('aria-busy') !== 'true'")
    row = page.locator(f'.type-row[data-id="{a["id"]}"]'); row.locator('[role=switch]').focus(); page.keyboard.press('Space')
    page.wait_for_selector(f'.type-row[data-id="{a["id"]}"] [aria-checked=false]')
    assert get()['type_ids'] == [a['id'], b['id']] and get()['types'][0]['active'] is False
    # 중복 이름 실패 후 입력과 재시도 동작을 보존한다.
    page.fill('#type-create input', '이름 수정 확인'); page.click('#type-create button')
    page.wait_for_selector('#type-status:text("다시 시도")')
    assert page.input_value('#type-create input') == '이름 수정 확인'
    assert page.locator('#type-create button').is_enabled()
    page.fill('#type-create input', '')
    for scheme in ('light','dark'):
        page.evaluate("s => {localStorage.setItem('dev.theme',s);document.documentElement.dataset.theme=s}", scheme)
        page.emulate_media(color_scheme=scheme)
        for width in (1300,390):
            page.set_viewport_size({'width':width,'height':850})
            for name, url, selector in [('manage','types','.type-row'), ('new','new','#new-form'), ('detail','issue/'+ref,'#save-types'), ('list','','tr.row'), ('board','board','.card')]:
                page.goto(BASE + '/#/' + url); page.wait_for_selector(selector)
                assert page.evaluate('document.documentElement.scrollWidth') <= width + 1, (name,scheme,width)
                if name == 'new': assert page.locator(f'[data-type="{a["id"]}"]').count() == 0
                if name == 'detail':
                    assert page.locator(f'[data-type="{a["id"]}"]').get_attribute('aria-pressed') == 'true'
                    assert '비활성' in page.locator('.byline').inner_text()
                if name in ('list','board'):
                    assert '이름 수정 확인' in page.locator('#view').inner_text()
                page.locator('#toast').evaluate('e => e.hidden = true')
                page.screenshot(path=str(shots / f'types_{name}_{scheme}_{width}.png'), full_page=True)
    page.goto(BASE + '/#/issue/' + ref); page.wait_for_selector('#save-types')
    page.click(f'[data-type="{a["id"]}"]'); page.click(f'[data-type="{b["id"]}"]'); page.click('#save-types')
    page.wait_for_function("document.querySelector('#types-status').textContent === '' && document.querySelectorAll('[data-type][aria-pressed=true]').length === 0")
    assert get()['type_ids'] == []
    assert page.locator(f'[data-type="{a["id"]}"]').count() == 0
    page.set_viewport_size({'width':1300,'height':850})
    page.evaluate("localStorage.setItem('dev.theme','light');document.documentElement.dataset.theme='light'")
    page.emulate_media(color_scheme='light')


def check_auto_settings(page, shots):
    """프로젝트 격리·실패 복원·키보드와 네 화면을 확인한다."""
    H = {'X-Requested-With': 'dev'}
    for key in ('AUTOA', 'AUTOB'):
        assert page.request.post(BASE + '/api/projects', headers=H, data={'key': key, 'name': key}).ok
    page.reload(); page.wait_for_selector('#project-filter')
    page.select_option('#project-filter', '')
    page.goto(BASE + '/#/settings')
    page.wait_for_selector('#settings-body .empty')
    page.select_option('#project-filter', 'AUTOA')
    review = page.locator('#auto_review')
    page.wait_for_selector('#auto_review')
    assert review.get_attribute('aria-checked') == 'false'
    approve = page.locator('#auto_approve')
    assert approve.is_enabled() and approve.get_attribute('aria-checked') == 'false'
    assert page.locator('.setting-row .auto-switch').evaluate_all('(els) => els.map(e => e.id)') == ['auto_review', 'auto_plan_approve', 'auto_execute', 'auto_approve', 'token_exhaustion_fallback']
    # 세 역할 카드 — 준비 중 역할 표시, 지목된 안내문 삭제, 이관 스위치의 비용 안내
    assert page.locator('.role-card h4').evaluate_all('(els) => els.map(e => e.textContent)') == ['오케스트레이터 준비 중', '작업 에이전트', '트러블슈터 준비 중', 'Description 작성']
    assert '신규 자동 등록 순서' not in page.locator('#settings-body').inner_text()
    assert '아직 실행되지 않아요' in page.locator('.role-card').first.inner_text()
    assert '금액 상한' in page.locator('#token_exhaustion_fallback-help').inner_text()
    fallback = page.locator('#token_exhaustion_fallback')
    assert fallback.get_attribute('aria-checked') == 'false' and fallback.get_attribute('aria-describedby') == 'token_exhaustion_fallback-help'
    fallback.focus(); page.keyboard.press('Space')
    page.wait_for_function("document.querySelector('#token_exhaustion_fallback').getAttribute('aria-checked') === 'true' && !document.querySelector('#token_exhaustion_fallback').disabled")
    assert fallback.evaluate('e => e === document.activeElement')
    page.click('#troubleshooter-order [data-move="0"][data-direction="1"]')
    page.wait_for_function("document.querySelector('#troubleshooter-order li span').textContent === 'Codex' && document.querySelector('#settings-body').getAttribute('aria-busy') === 'false'")
    assert page.locator('#troubleshooter-order [data-move="1"][data-direction="-1"]').evaluate('e => e === document.activeElement')
    saved = page.request.get(BASE + '/api/projects/AUTOA/auto-settings').json()
    assert saved['troubleshooter_provider_order'] == ['codex', 'claude'] and saved['provider_order'] == ['claude', 'codex']
    assert saved['orchestrator_provider_order'] == ['claude', 'codex'] and saved['token_exhaustion_fallback'] is True
    page.reload(); page.wait_for_selector('#troubleshooter-order li')
    assert page.locator('#troubleshooter-order li span').first.inner_text() == 'Codex' and page.locator('#orchestrator-order li span').first.inner_text() == 'Claude'
    assert fallback.get_attribute('aria-checked') == 'true'
    page.click('#description-order [data-move="0"][data-direction="1"]')
    page.wait_for_function("document.querySelector('#description-order li span').textContent === 'Codex' && document.querySelector('#settings-body').getAttribute('aria-busy') === 'false'")
    saved = page.request.get(BASE + '/api/projects/AUTOA/auto-settings').json()
    assert saved['description_provider_order'] == ['codex', 'claude'] and saved['troubleshooter_provider_order'] == ['codex', 'claude']
    page.reload(); page.wait_for_selector('#description-order li')
    assert page.locator('#description-order li span').first.inner_text() == 'Codex'
    assert page.locator('#auto_approve-label').inner_text() == 'Auto 태스크 승인'
    assert approve.get_attribute('role') == 'switch'
    assert approve.get_attribute('aria-labelledby') == 'auto_approve-label'
    assert approve.get_attribute('aria-describedby') == 'auto_approve-help'
    assert '결과를 자동 승인' in page.locator('#auto_approve-help').inner_text()
    plan_approve = page.locator('#auto_plan_approve')
    assert plan_approve.is_enabled() and plan_approve.get_attribute('aria-checked') == 'false'
    plan_approve.click()
    page.wait_for_function("document.querySelector('#auto_plan_approve').getAttribute('aria-checked') === 'true' && !document.querySelector('#auto_plan_approve').disabled")
    saved = page.request.get(BASE + '/api/projects/AUTOA/auto-settings').json()
    assert saved['auto_plan_approve'] is True and saved['auto_approve'] is False
    page.reload(); page.wait_for_selector('#auto_plan_approve')
    assert plan_approve.get_attribute('aria-checked') == 'true'
    plan_approve.click()
    page.wait_for_function("document.querySelector('#auto_plan_approve').getAttribute('aria-checked') === 'false' && !document.querySelector('#auto_plan_approve').disabled")
    approve.focus(); page.keyboard.press('Space')
    page.wait_for_function("document.querySelector('#auto_approve').getAttribute('aria-checked') === 'true' && !document.querySelector('#auto_approve').disabled")
    assert page.request.get(BASE + '/api/projects/AUTOA/auto-settings').json()['auto_approve'] is True
    assert page.locator('#auto_execute').get_attribute('aria-checked') == 'false'
    page.reload(); page.wait_for_selector('#auto_approve')
    assert approve.get_attribute('aria-checked') == 'true'
    approve.focus(); page.keyboard.press('Enter')
    page.wait_for_function("document.querySelector('#auto_approve').getAttribute('aria-checked') === 'false' && !document.querySelector('#auto_approve').disabled")
    approve.click()
    page.wait_for_function("document.querySelector('#auto_approve').getAttribute('aria-checked') === 'true' && !document.querySelector('#auto_approve').disabled")
    review.focus(); page.keyboard.press('Space')
    page.wait_for_function("document.querySelector('#settings-status').textContent === '저장했어요.'")
    assert review.get_attribute('aria-checked') == 'true'
    page.locator('#auto_execute').focus(); page.keyboard.press('Enter')
    page.wait_for_function("document.querySelector('#auto_execute').getAttribute('aria-checked') === 'true' && !document.querySelector('#auto_execute').disabled")
    page.click('#provider-order [data-move="0"][data-direction="1"]')
    page.wait_for_function("document.querySelector('#provider-order li span').textContent === 'Codex' && document.querySelector('#settings-body').getAttribute('aria-busy') === 'false'")
    def fail(route):
        if route.request.method == 'PATCH':
            route.fulfill(status=500, content_type='application/json', body='{"detail":"저장 실패"}')
        else:
            route.continue_()
    page.route('**/api/projects/AUTOA/auto-settings', fail)
    approve.click()
    page.wait_for_function("document.querySelector('#settings-status').textContent.includes('복원')")
    assert approve.get_attribute('aria-checked') == 'true' and approve.is_enabled()
    assert approve.evaluate('e => e === document.activeElement')
    review.click()
    page.wait_for_function("document.querySelector('#settings-status').textContent.includes('복원')")
    assert review.get_attribute('aria-checked') == 'true' and review.is_enabled()
    page.click('#provider-order [data-move="0"][data-direction="1"]')
    page.wait_for_function("document.querySelector('#settings-status').textContent.includes('복원') && document.querySelector('#provider-order li span').textContent === 'Codex'")
    page.unroute('**/api/projects/AUTOA/auto-settings', fail)
    page.select_option('#project-filter', 'AUTOB')
    page.wait_for_function("document.querySelector('#auto_review')?.getAttribute('aria-checked') === 'false'")
    assert approve.get_attribute('aria-checked') == 'false'
    page.select_option('#project-filter', 'AUTOA')
    page.wait_for_function("document.querySelector('#auto_review')?.getAttribute('aria-checked') === 'true'")
    assert approve.get_attribute('aria-checked') == 'true'
    held = []
    def hold(route):
        held.append(route)
    page.route('**/api/projects/AUTOA/auto-settings', hold)
    approve.click()
    page.wait_for_function("document.querySelector('#settings-body').getAttribute('aria-busy') === 'true'")
    assert review.is_disabled() and approve.is_disabled() and page.locator('#auto_execute').is_disabled()
    page.select_option('#project-filter', 'AUTOB')
    page.wait_for_function("document.querySelector('#auto_review')?.getAttribute('aria-checked') === 'false'")
    assert len(held) == 1
    held.pop().fulfill(status=500, content_type='application/json', body='{"detail":"late error"}')
    page.wait_for_timeout(100)
    assert page.locator('#settings-status').inner_text() == ''
    assert approve.get_attribute('aria-checked') == 'false' and approve.is_enabled()
    # 성공 응답도 프로젝트를 떠난 뒤에는 새 화면의 상태·포커스를 바꾸지 않는다.
    page.route('**/api/projects/AUTOB/auto-settings', hold)
    approve.click()
    page.wait_for_function("document.querySelector('#settings-body').getAttribute('aria-busy') === 'true'")
    assert len(held) == 1
    assert held[0].request.post_data_json == {'auto_approve': True}
    page.select_option('#project-filter', 'AUTOA')
    page.wait_for_selector('.settings-skeleton')
    assert len(held) == 2
    held.pop().fulfill(json=page.request.get(BASE + '/api/projects/AUTOA/auto-settings').json())
    page.wait_for_selector('#auto_approve')
    held.pop().fulfill(json={'auto_review': False, 'auto_execute': False, 'auto_approve': True, 'provider_order': ['claude', 'codex']})
    page.wait_for_timeout(100)
    assert approve.get_attribute('aria-checked') == 'true'
    assert review.get_attribute('aria-checked') == 'true'
    assert page.locator('#settings-status').inner_text() == ''
    page.unroute('**/api/projects/AUTOB/auto-settings', hold)
    page.select_option('#project-filter', 'AUTOB')
    page.wait_for_function("document.querySelector('#auto_approve')?.getAttribute('aria-checked') === 'false'")
    page.select_option('#project-filter', 'AUTOA')
    page.wait_for_selector('.settings-skeleton')
    page.goto(BASE + '/#/projects'); page.wait_for_selector('#project-create')
    assert len(held) == 1
    held.pop().fulfill(status=500, content_type='application/json', body='{"detail":"late load"}')
    page.wait_for_timeout(100)
    assert page.locator('#project-create').is_visible()
    page.unroute('**/api/projects/AUTOA/auto-settings', hold)
    page.goto(BASE + '/#/settings'); page.wait_for_selector('#auto_review')
    page.wait_for_timeout(3200)  # 실패 토스트가 캡처의 도움말을 가리지 않도록 기다린다.
    for scheme in ('light', 'dark'):
        page.evaluate("s => { localStorage.setItem('dev.theme', s); document.documentElement.dataset.theme = s; }", scheme)
        for width, tag in ((1300, 'desktop'), (390, 'mobile')):
            page.set_viewport_size({'width': width, 'height': 850})
            assert page.evaluate('document.documentElement.scrollWidth') <= width + 1
            assert page.locator('.auto-settings').evaluate('e => e.scrollWidth <= e.clientWidth + 1')
            for row in page.locator('.setting-row').all():
                assert row.evaluate('e => { const text=e.firstElementChild.getBoundingClientRect(), button=e.lastElementChild.getBoundingClientRect(), box=e.getBoundingClientRect(); return text.right <= button.left && button.right <= box.right + 1 && e.scrollWidth <= e.clientWidth + 1; }')
            page.screenshot(path=str(shots / f'auto_settings_{scheme}_{tag}.png'), full_page=True)
    page.emulate_media(reduced_motion='reduce')
    assert review.locator('.switch-track').evaluate("e => getComputedStyle(e, '::before').transitionDuration") == '0s'
    page.emulate_media(reduced_motion='no-preference')
    page.reload(); page.wait_for_selector('#auto_review')
    assert review.get_attribute('aria-checked') == 'true'
    assert approve.get_attribute('aria-checked') == 'true'
    page.set_viewport_size({'width': 1300, 'height': 850})
    page.evaluate("localStorage.setItem('dev.theme', 'light'); document.documentElement.dataset.theme = 'light'")
    for key in ('AUTOA', 'AUTOB'):
        assert page.request.delete(BASE + '/api/projects/' + key, headers=H).ok
    page.evaluate("localStorage.removeItem('dev.project')")
    page.goto(BASE + '/#/projects'); page.reload(); page.wait_for_selector('#project-create')


def check_settings_navigation(page, shots):
    """설정 하위 주소·이력·범위와 조회/저장 응답 격리를 확인한다."""
    H = {'X-Requested-With': 'dev'}
    for key in ('NAVA', 'NAVB'):
        assert page.request.post(BASE + '/api/projects', headers=H, data={'key': key, 'name': key}).ok
    page.reload(); page.wait_for_selector('#project-filter')
    page.select_option('#project-filter', '')

    def selected(tab, selector):
        page.wait_for_selector(selector)
        assert page.locator('[data-nav=settings]').get_attribute('class').endswith('active')
        assert page.locator('#settings-nav [aria-current=page]').get_attribute('data-settings') == tab
        assert page.locator('#settings-nav a').count() == 2
        assert page.locator('#view > .panel #settings-nav').is_visible()
        assert page.locator('#shell > #settings-nav').count() == 0
        assert page.locator('#view > .panel > h2').text_content() == 'Settings'

    page.goto(BASE + '/#/settings'); selected('auto', '#settings-body .empty')
    assert page.url.endswith('/#/settings/auto')
    page.locator('[data-settings=types]').focus(); page.keyboard.press('Enter')
    selected('types', '#type-create')
    assert '모든 프로젝트' in page.locator('.type-management').inner_text()
    page.go_back(); selected('auto', '#settings-body .empty')
    page.go_forward(); selected('types', '#type-create')
    page.reload(); selected('types', '#type-create')
    page.goto(BASE + '/#/types'); selected('types', '#type-create')
    assert page.url.endswith('/#/settings/types')
    page.select_option('#project-filter', 'NAVA'); selected('types', '#type-create')
    page.select_option('#project-filter', 'NAVB'); selected('types', '#type-create')
    page.goto(BASE + '/#/projects'); page.wait_for_selector('#project-create')
    assert page.locator('#settings-nav').count() == 0

    # 같은 주소로 돌아와도 앞선 조회가 최신 화면을 덮지 않아야 한다.
    held = []
    pattern = '**/api/issue-types?include_inactive=true'
    catalog = page.request.get(BASE + '/api/issue-types?include_inactive=true').json()
    page.route(pattern, lambda route: held.append(route))
    page.goto(BASE + '/#/settings/types'); page.wait_for_selector('.settings-skeleton')
    page.goto(BASE + '/#/settings/auto'); selected('auto', '#auto_review')
    page.goto(BASE + '/#/settings/types'); page.wait_for_selector('.settings-skeleton')
    page.wait_for_function('true'); assert len(held) == 2
    held.pop().fulfill(json=catalog); selected('types', '#type-create')
    held.pop().fulfill(status=401, json={'detail': 'late load'})
    page.wait_for_timeout(100); selected('types', '#type-create')
    assert page.locator('#shell').is_visible()
    page.unroute(pattern)

    # 저장 완료 뒤 재조회 중 전환하는 경우까지 분리한다.
    page.route(pattern, lambda route: held.append(route))
    page.fill('#type-create input', '응답 격리 검사'); page.click('#type-create button')
    page.wait_for_selector('.settings-skeleton')
    page.goto(BASE + '/#/settings/auto'); selected('auto', '#auto_review')
    assert len(held) == 1
    held.pop().fulfill(json=catalog)
    page.wait_for_timeout(100); selected('auto', '#auto_review')
    page.unroute(pattern)

    page.goto(BASE + '/#/settings/types'); selected('types', '#type-create')
    page.route('**/api/issue-types', lambda route: held.append(route))
    page.fill('#type-create input', '늦은 저장 검사'); page.click('#type-create button')
    page.wait_for_selector('#type-create[aria-busy=true]')
    page.select_option('#project-filter', 'NAVA'); selected('types', '#type-create')
    held.pop().fulfill(status=401, json={'detail': 'late save'})
    page.wait_for_timeout(100); selected('types', '#type-create')
    assert page.locator('#type-status').inner_text() == ''
    assert page.locator('#shell').is_visible()
    page.unroute('**/api/issue-types')

    auto_pattern = '**/api/projects/NAVA/auto-settings'
    page.route(auto_pattern, lambda route: held.append(route))
    page.goto(BASE + '/#/settings/auto'); page.wait_for_selector('.settings-skeleton')
    page.goto(BASE + '/#/settings/types'); selected('types', '#type-create')
    held.pop().fulfill(status=401, json={'detail': 'late auto load'})
    page.wait_for_timeout(100); selected('types', '#type-create')
    page.unroute(auto_pattern)
    page.goto(BASE + '/#/settings/auto'); selected('auto', '#auto_review')
    page.route(auto_pattern, lambda route: held.append(route))
    page.click('#auto_review'); page.wait_for_selector('#settings-body[aria-busy=true]')
    page.goto(BASE + '/#/settings/types'); selected('types', '#type-create')
    held.pop().fulfill(status=401, json={'detail': 'late auto save'})
    page.wait_for_timeout(100); selected('types', '#type-create')
    assert page.locator('#shell').is_visible()
    page.unroute(auto_pattern)

    for scheme in ('light', 'dark'):
        page.evaluate("s => {localStorage.setItem('dev.theme',s);document.documentElement.dataset.theme=s}", scheme)
        page.emulate_media(color_scheme=scheme)
        for width in (1300, 390):
            page.set_viewport_size({'width': width, 'height': 850})
            for tab, selector in (('auto', '#auto_review'), ('types', '#type-create')):
                page.goto(BASE + '/#/settings/' + tab); selected(tab, selector)
                assert page.evaluate('document.documentElement.scrollWidth') <= width + 1
                assert page.locator('#settings-nav').evaluate('e => e.scrollWidth <= e.clientWidth + 1')
                page.keyboard.press('Tab')
                page.locator('[data-settings=' + tab + ']').focus()
                assert page.locator('[data-settings=' + tab + ']').evaluate('e => getComputedStyle(e).outlineStyle') != 'none'
                page.locator('#toast').evaluate('e => e.hidden = true')
                page.screenshot(path=str(shots / f'settings_{tab}_{scheme}_{width}.png'), full_page=True)
    page.set_viewport_size({'width': 1300, 'height': 850})
    page.evaluate("localStorage.setItem('dev.theme','light');document.documentElement.dataset.theme='light'")
    page.emulate_media(color_scheme='light')
    for key in ('NAVA', 'NAVB'):
        assert page.request.delete(BASE + '/api/projects/' + key, headers=H).ok
    page.evaluate("localStorage.removeItem('dev.project')")
    page.goto(BASE + '/#/projects'); page.reload(); page.wait_for_selector('#project-create')


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


def check_progress_refresh(page, shots):
    """가짜 단계 응답으로 후처리 갱신과 초안 보존을 확인한다."""
    headers = {'X-Requested-With': 'dev'}
    page.request.post(BASE + '/api/projects', data={'key': 'LIVE', 'name': '진행 검사'}, headers=headers)
    parent = page.request.post(BASE + '/api/issues', data={'project': 'LIVE', 'title': '진행 부모'}, headers=headers).json()['ref']
    ref = page.request.post(BASE + '/api/issues', data={'project': 'LIVE', 'title': '진행 Task', 'parent': parent}, headers=headers).json()['ref']
    state = {'phase': '병합 검사 중', 'status': 'in_progress', 'queued': True}
    # 모의 단계 갱신은 고정 응답을 복사한다. 라우트 안의 반복 서버 요청으로 인한 일시적인 연결 끊김을 피한다.
    snapshots = {r: page.request.get(BASE + '/api/issues/' + r).json() for r in (ref, parent)}
    listing = page.request.get(BASE + '/api/issues?project=LIVE').json()
    def mock(route):
        url = route.request.url
        original = {} if '/api/jobs' in url else listing if 'issues?' in url else snapshots[url.rsplit('/', 1)[-1]]
        response = json.loads(json.dumps(original))
        def patch(i):
            if i['ref'] == ref:
                i.update(status=state['status'], merge_state=state['phase'])
            return i
        if '/api/jobs' in url:
            response = {'jobs': [{'id': 99999, 'ref': ref, 'position': 1, 'mode': 'execute', 'provider': 'codex', 'note': '앞 Task의 병합·운영 반영 완료를 기다려요.'}] if state['queued'] else []}
        elif 'issues?' in url:
            response['issues'] = [patch(i) for i in response['issues']]
        else:
            patch(response)
            response['children'] = [patch(i) for i in response['children']]
        route.fulfill(json=response)
    patterns = [f'**/api/issues/{ref}', f'**/api/issues/{parent}', '**/api/issues?*', '**/api/jobs']
    for pattern in patterns:
        page.route(pattern, mock)
    try:
        for scheme in ('light', 'dark'):
            for width in (1300, 390):
                state.update(phase='병합 검사 중', status='in_progress')
                page.set_viewport_size({'width': width, 'height': 850})
                page.emulate_media(color_scheme=scheme, reduced_motion='reduce')
                page.evaluate("s => {localStorage.setItem('dev.theme',s);document.documentElement.dataset.theme=s}", scheme)
                page.goto(BASE + f'/#/issue/{ref}')
                page.reload()
                page.wait_for_selector('#stage')
                page.fill('#comment', '갱신 중인 댓글 초안')
                for phase in ('운영 반영 중', '재시작 확인 중', '복구 중', '복구 확인 중'):
                    state['phase'] = phase
                    page.wait_for_function("phase => document.querySelector('#stage')?.textContent.includes(phase)", arg=phase, timeout=12000)
                    assert page.locator('#result-actions').count() == 0
                    assert page.input_value('#comment') == '갱신 중인 댓글 초안'
                assert page.input_value('#comment') == '갱신 중인 댓글 초안'
                assert page.evaluate('document.documentElement.scrollWidth') <= width + 1
                page.screenshot(path=str(shots / f'progress_{scheme}_{width}.png'), full_page=True)
                state.update(phase='병합됨', status='in_review')
                page.wait_for_selector('#result-actions', timeout=12000)
        state.update(phase='운영 반영 중', status='in_progress')
        page.goto(BASE + f'/#/issue/{parent}')
        page.wait_for_function("document.querySelector('.progress-note')?.textContent.includes('운영 반영 중')")
        state.update(phase='병합됨', status='in_review')
        page.wait_for_function("document.querySelector('.progress-note')?.textContent.includes('병합됨')", timeout=12000)
        state.update(phase='병합 검사 중', status='in_progress')
        page.evaluate("() => {localStorage.setItem('dev.project','LIVE');projectSel.value='LIVE';localStorage.setItem('dev.list',JSON.stringify({statuses:[],closed:false,q:''}))}")
        page.goto(BASE + '/#/')
        page.wait_for_selector('.jobs summary'); page.click('.jobs summary')
        assert '병합·운영 반영 완료' in page.inner_text('.jobs')
        state.update(phase='병합됨', status='in_review', queued=False)
        page.wait_for_function("!document.querySelector('.jobs') && [...document.querySelectorAll('.progress-note')].some(el => el.textContent.includes('병합됨'))", timeout=12000)
    finally:
        for pattern in patterns:
            page.unroute(pattern, mock)
        page.goto(BASE + '/#/agents')
        page.wait_for_selector('#a-name')
        page.request.delete(BASE + f'/api/issues/{ref}', headers=headers)
        page.request.delete(BASE + f'/api/issues/{parent}', headers=headers)


def check_rollback_waiting(page, shots):
    """거절 요청이 On Hold를 반환하면 상세와 보류 상태를 유지한다."""
    for scheme in ('light', 'dark'):
        for width, height, tag in ((1300, 850, 'desktop'), (390, 800, 'mobile')):
            ref = page.request.post(BASE + '/api/issues', data={'project': 'NS', 'title': '롤백 운영 확인'}, headers=H).json()['ref']
            page.request.post(f'{BASE}/api/issues/{ref}/status', data={'status': 'in_review'}, headers=H)
            page.emulate_media(color_scheme=scheme); page.set_viewport_size({'width': width, 'height': height})
            page.evaluate("scheme => {localStorage.setItem('dev.theme', scheme); document.documentElement.dataset.theme = scheme}", scheme)
            page.goto(BASE + f'/#/issue/{ref}')
            page.wait_for_function("ref => document.querySelector('.detail > div > .ref')?.textContent.trim() === ref", arg=ref)
            url = f'**/api/issues/{ref}/status'
            def pending(route):
                response = page.request.post(f'{BASE}/api/issues/{ref}/status', data={'status': 'on_hold', 'note': '운영 확인 대기'}, headers=H)
                route.fulfill(status=200, json=response.json())
            page.route(url, pending)
            page.click('#result-actions [data-to=closed]')
            page.fill('#result-note', '방향이 달라요'); page.click('#result-send')
            page.wait_for_function("document.querySelector('#status')?.value === 'on_hold'")
            assert page.evaluate('location.hash') == f'#/issue/{ref}'
            assert '운영 반영' in page.locator('#toast').inner_text()
            page.screenshot(path=str(shots / f'rollback_waiting_{scheme}_{tag}.png'), full_page=True)
            page.unroute(url, pending)


def check_sticky_tabbar(page, shots):
    """긴 이슈를 끝까지 내려도 전역 탭 줄이 맨 위에 붙어 있고 오른쪽 칸·사용자 메뉴와 겹치지 않는다(DEV-88)."""
    body = '\n\n'.join(f'{i}번째 문단 — 긴 본문을 만들기 위한 줄이에요.' for i in range(120))
    ref = page.request.post(BASE + '/api/issues', data={'project': 'NS', 'title': '긴 이슈', 'body': body}, headers=H).json()['ref']
    for scheme in ('light', 'dark'):
        page.emulate_media(color_scheme=scheme)
        page.evaluate("s => { localStorage.setItem('dev.theme', s); document.documentElement.dataset.theme = s; }", scheme)
        for width, tag in ((1300, 'desktop'), (390, 'mobile')):
            page.set_viewport_size({'width': width, 'height': 850})
            page.goto(BASE + f'/#/issue/{ref}')
            page.wait_for_function("ref => document.querySelector('.detail > div > .ref')?.textContent.trim() === ref", arg=ref)
            page.evaluate('window.scrollTo(0, 0)')
            page.click('#user-chip')
            box = page.locator('#user-menu').bounding_box()
            assert page.evaluate("([x, y]) => !!document.elementFromPoint(x, y)?.closest('#user-menu')",
                                 [box['x'] + box['width'] / 2, box['y'] + box['height'] - 4])
            page.keyboard.press('Escape')
            if tag == 'desktop':
                page.evaluate('window.scrollTo(0, 400)'); page.wait_for_timeout(100)
                assert page.evaluate("document.querySelector('.side').getBoundingClientRect().top >= document.querySelector('.tab-bar').getBoundingClientRect().bottom")
            page.evaluate('window.scrollTo(0, document.body.scrollHeight)'); page.wait_for_timeout(100)
            assert page.evaluate('scrollY') > 500
            assert page.evaluate("document.querySelector('.tab-bar').getBoundingClientRect().top") == 0
            assert page.locator('[data-nav=issues]').is_visible()
            page.screenshot(path=str(shots / f'issue_sticky_{scheme}_{tag}.png'))
            page.click('[data-nav=issues]')
            page.wait_for_function("location.hash === '#/'")
    page.set_viewport_size({'width': 1300, 'height': 850})
    page.emulate_media(color_scheme='light')
    page.evaluate("localStorage.setItem('dev.theme', 'light'); document.documentElement.dataset.theme = 'light'")
    page.request.delete(BASE + f'/api/issues/{ref}', headers=H)


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



def check_board_actions(page, shots, answers, asked):
    """Action의 판단과 요청 차단을 모의 응답으로 검사한다."""
    settings = {'auto_review': False, 'auto_execute': False}
    context = dict(plan_version=None, approval=None, has_children=False, has_execution=False, has_active_job=False, parent=None)
    def item(n, status='backlog', **patch):
        return dict(ref=f'ACT-{n}', title='Action 검사', project_key='ACT', status=status, parent_id=None,
                    labels=[], types=[], priority='none', action_context={**context, **patch})
    valid = {'verdict':'approve_notes','plan_version':2,'stale':False}
    rows = [item(1), item(2,'triage',plan_version=2), item(3,'in_review',has_children=True),
            {**item(4, parent={'plan_version':2,'approval':valid}), 'parent_id':100,'parent_ref':'ACT-100'},
            item(5,'in_review',plan_version=2), item(6,'triage',plan_version=2,approval=valid),
            {**item(7,has_execution=True,parent={'plan_version':2,'approval':valid}),'parent_id':100},
            item(8,has_active_job=True), {**item(9),'parent_id':101},
            *[item(n,status,plan_version=2) for n,status in enumerate(
                ('waiting','in_progress','done','closed','on_hold','changes_requested'),10)],
            {**item(20,parent={'plan_version':3,'approval':{**valid,'stale':True}}),'parent_id':102},
            item(21,'triage',has_children=True,plan_version=2,approval=valid),
            {**item(22,'in_review'),'parent_id':100,'parent_ref':'ACT-100'},
            {**item(23,'in_review'),'parent_id':101,'parent_ref':'ACT-101'},
            {**item(24,'done'),'parent_id':100,'parent_ref':'ACT-100'}]
    posts, pending = [], []
    delay = False
    conflict = False
    fail_settings = False
    delay_settings = False
    pending_settings = []
    parent_status = 'triage'
    fail_parent = False
    def issues(route): route.fulfill(json={'issues':rows,'has_more':False})
    def auto(route):
        if delay_settings:
            pending_settings.append(route); return
        route.fulfill(status=500 if fail_settings else 200, json={'detail':'병합 대기'} if fail_settings else settings)
    def action(route):
        if route.request.method == 'GET' and route.request.url.endswith(('/ACT-100', '/ACT-101')):
            route.fulfill(status=500 if fail_parent else 200,
                          json={'status':parent_status if route.request.url.endswith('/ACT-100') else 'in_review'})
            return
        if route.request.method == 'POST':
            posts.append((route.request.url, route.request.post_data_json))
            if delay: pending.append(route); return
            route.fulfill(status=409 if conflict else 200, json={'detail':'? 묶음을 끝냈어요.'} if conflict else {})
        else:
            route.fulfill(json={**rows[2], 'children':[{'ref':'ACT-3-1','title':'?미완료 Task','status':'backlog','merge_state':'병합 대기'}],
                                'execute':{'timeout_sec':1800,'budget_usd':2}})
    page.route('**/api/issues?*', issues)
    page.route('**/api/projects/*/auto-settings', auto)
    page.route('**/api/issues/ACT-**', action)
    def ready(board=False):
        page.goto(BASE + ('/#/board' if board else '/#/'))
        page.reload(); page.wait_for_selector('[data-action]')
    def btn(ref, kind, provider=''):
        return page.locator(f'[data-action="{kind}"][data-ref="ACT-{ref}"][data-provider="{provider}"]')
    try:
        for board in (False,True):
            ready(board)
            assert page.locator('[data-action]').count() == 7
            for kind, ref in [('review',1),('execute',4)]:
                for provider in ('claude','codex'):
                    btn(ref,kind,provider).click()
                    page.wait_for_function('pendingActions.size === 0 && !!document.querySelector("[data-action]") && !document.querySelector("[data-action]").disabled')
                    assert posts[-1][0].endswith('/'+kind) and posts[-1][1] == {'provider':provider}
                    assert page.url.endswith('/#/board' if board else '/#/')
            # 아이콘의 기하학적 중심이 버튼 중심과 일치한다.
            for provider in ('claude','codex'):
                assert btn(1,'review',provider).evaluate('''b => {
                    const r=b.getBoundingClientRect(), s=b.querySelector('svg').getBoundingClientRect();
                    return Math.abs(r.x+r.width/2-s.x-s.width/2)<1 && Math.abs(r.y+r.height/2-s.y-s.height/2)<1;
                }''')
            before = len(posts); asked.clear()
            btn(22,'task-approve').click()
            page.wait_for_function('pendingActions.size === 0 && !!document.querySelector("[data-action]") && !document.querySelector("[data-action]").disabled')
            assert len(posts) == before + 1 and posts[-1][0].endswith('/ACT-22/status')
            assert posts[-1][1] == {'status':'done'} and not asked
            settings.update(auto_approve=True)
            ready(board); before = len(posts); asked.clear()
            assert btn(22,'task-approve').get_attribute('aria-disabled') == 'true'
            for key in ('Enter','Space'):
                btn(22,'task-approve').focus(); page.keyboard.press(key)
                assert page.inner_text('#toast') == 'Auto 모드에서는 해당 버튼이 비활성화됩니다.'
            btn(22,'task-approve').click(force=True)
            assert len(posts) == before and not asked
            settings.update(auto_approve=False)
            for parent_status in ('backlog','in_review','done','closed','on_hold'):
                ready(board)
                assert btn(22,'task-approve').count() == 0
            parent_status = 'triage'; fail_parent = True
            ready(board); assert btn(22,'task-approve').count() == 0
            fail_parent = False
            settings.update(auto_review=True,auto_execute=True)
            ready(board); before = len(posts); asked.clear()
            for kind,ref in [('review',1),('execute',4)]:
                for key in ('Enter','Space'):
                    btn(ref,kind,'claude').focus(); page.keyboard.press(key)
                    assert page.inner_text('#toast') == 'Auto 모드에서는 해당 버튼이 비활성화됩니다.'
                btn(ref,kind,'codex').click(force=True)
            assert len(posts) == before and not asked
            settings.update(auto_review=False,auto_execute=False)
            ready(board)
            settings.update(auto_plan_approve=True,auto_plan_approve_available=True)
            ready(board); before = len(posts); asked.clear()
            btn(2,'decision').click(force=True)
            assert len(posts) == before and not asked
            assert page.inner_text('#toast') == 'Auto 모드에서는 해당 버튼이 비활성화됩니다.'
            settings.update(auto_plan_approve=False)
            ready(board)
            conflict = True; btn(2,'decision').click()
            page.wait_for_function('pendingActions.size === 0 && !!document.querySelector("[data-action]") && !document.querySelector("[data-action]").disabled')
            assert posts[-1][1]['plan_version'] == 2
            conflict = False
            before = len(posts); answers.append(None); btn(3,'complete-tree').click()
            page.wait_for_function('pendingActions.size === 0 && !!document.querySelector("[data-action]") && !document.querySelector("[data-action]").disabled')
            assert len(posts) == before
            btn(3,'complete-tree').click(); page.wait_for_function('pendingActions.size === 0 && !!document.querySelector("[data-action]") && !document.querySelector("[data-action]").disabled')
            assert posts[-1][0].endswith('/complete-tree') and '?미완료 Task' in asked[-1] and '병합 대기' in asked[-1]
            for scheme in ('light','dark'):
                page.evaluate('s => {localStorage.setItem("dev.theme",s);document.documentElement.dataset.theme=s}',scheme)
                page.emulate_media(color_scheme=scheme)
                for width in (1300,390):
                    page.set_viewport_size({'width':width,'height':850})
                    page.wait_for_timeout(200)
                    assert page.evaluate('document.documentElement.scrollWidth') <= width + 1
                    page.screenshot(path=str(shots / f'action_{"board" if board else "list"}_{scheme}_{width}.png'),full_page=True)
                    if board:
                        page.locator('.kanban').evaluate('e => { const col=e.querySelector("[data-col=in_review]"); e.scrollLeft += col.getBoundingClientRect().left-e.getBoundingClientRect().left; }')
                        page.wait_for_timeout(200)
                        page.screenshot(path=str(shots / f'action_board_review_{scheme}_{width}.png'),full_page=True)
            page.set_viewport_size({'width':1300,'height':850})
        for ref, kind, provider in ((1,'review','claude'), (22,'task-approve','')):
            ready(); delay = True; before = len(posts)
            btn(ref,kind,provider).evaluate('e => {e.click();e.click()}')
            page.wait_for_timeout(200); assert len(posts) == before + 1
            page.goto(BASE + '/#/projects'); page.wait_for_selector('.projects') if page.locator('.projects').count() else page.wait_for_timeout(200)
            pending.pop().fulfill(json={}); delay = False
            page.wait_for_timeout(200); assert page.url.endswith('/#/projects')
        fail_settings = True; ready(); before = len(posts); btn(1,'review','claude').click(force=True)
        assert len(posts) == before and '새로고침' in page.inner_text('#toast')
        fail_settings = False; delay_settings = True
        page.evaluate('void loadList()')
        page.wait_for_timeout(100)
        page.evaluate('localStorage.setItem("dev.project","OTHER");void loadList()')
        page.wait_for_timeout(100)
        assert len(pending_settings) == 2
        pending_settings.pop().fulfill(json=settings)
        page.wait_for_selector('[data-action]')
        pending_settings.pop().fulfill(status=500,json={'detail':'오래된 오류'})
        page.wait_for_timeout(100)
        assert page.locator('[data-action]').count() == 7
        assert page.inner_text('#toast') != '오래된 오류'
        page.evaluate('localStorage.removeItem("dev.project")')
    finally:
        page.unroute('**/api/issues?*',issues); page.unroute('**/api/projects/*/auto-settings',auto); page.unroute('**/api/issues/ACT-**',action)
        page.set_viewport_size({'width':1300,'height':850})
        page.evaluate('localStorage.setItem("dev.theme","light");document.documentElement.dataset.theme="light"')
        page.goto(BASE + '/#/projects'); page.reload(); page.wait_for_selector('#project-create')


tmp = Path(tempfile.mkdtemp())
(tmp / "fake_ns.py").write_text(FAKE_NS, "utf-8")
ns_port, dev_port = free_port(), free_port()
env = {**os.environ, "DEV_DATA_DIR": str(tmp / "data"), "DEV_NIGHTSHIFT_URL": f"http://127.0.0.1:{ns_port}",
       "DEV_CLAUDE_BIN": str(tmp / 'disabled-claude'), "DEV_CODEX_AGENT_KEY": "",
       "DEV_REVIEW_MCP_CONFIG": str(tmp / 'no-mcp.json'), "NTFY_TOPIC": ""}
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
        if '--attachments-only' in sys.argv:
            check_attachments(page, shots)
            assert not errs, errs
            print('OK: attachments'); sys.exit(0)
        if '--progress-only' in sys.argv:
            check_progress_refresh(page, shots)
            assert not errs, errs
            print('OK: progress refresh'); sys.exit(0)
        if '--board-actions-only' in sys.argv:
            check_board_actions(page, shots, answers, asked)
            assert not errs, errs
            print('OK: board actions'); sys.exit(0)
        check_account_menu(page, shots)
        check_settings_navigation(page, shots)
        page.goto(BASE + '/#/'); page.wait_for_selector('#list-body[aria-busy="false"]')
        if '--issue-types-only' in sys.argv:
            check_issue_types(page, shots)
            assert not errs, errs
            print('OK: issue types')
            sys.exit(0)
        if '--edit-saving-only' in sys.argv or '--rollback-only' in sys.argv:
            H = {'X-Requested-With': 'dev'}
            page.wait_for_selector('#list-body[aria-busy="false"]')
            assert page.request.post(BASE + '/api/projects', data={'key': 'NS', 'name': '저장 검사'}, headers=H).ok
            previous = page.request.post(BASE + '/api/issues', data={'project': 'NS', 'title': '이전 화면'}, headers=H).json()['ref']
            page.goto(BASE + f'/#/issue/{previous}')
            page.wait_for_function("ref => document.querySelector('.detail > div > .ref')?.textContent.trim() === ref", arg=previous)
            if '--rollback-only' in sys.argv:
                check_rollback_waiting(page, shots)
            else:
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
        check_auto_settings(page, shots)
        check_attachments(page, shots)
        check_published_notice(page, shots)
        check_board_actions(page, shots, answers, asked)
        if '--project-flows-only' in sys.argv:
            assert not errs, errs
            print('OK: project flows')
            sys.exit(0)
        page.click("#project-create")
        page.fill("#p-key", "NS"); page.fill("#p-name", "nightshift"); page.click("#project-form button[type=submit]")
        page.wait_for_selector("td.ref:text('NS')")
        assert page.get_attribute("#p-path", "placeholder").startswith("C:\\Users\\")
        # Modify — 키는 잠기고, 이름·경로가 바뀐다
        assert page.inner_text('[data-edit="NS"]').strip() == "Modify"
        # 연필 아이콘 + 보조(테두리) 버튼
        assert page.locator('[data-edit="NS"] use[href="#i-pencil"]').count() == 1
        edit_btn = page.eval_on_selector('[data-edit="NS"]', """b => { const s = getComputedStyle(b);
            return {ghost: b.classList.contains('ghost'), style: s.borderTopStyle, color: s.borderTopColor}; }""")
        assert not edit_btn["ghost"] and edit_btn["style"] == "solid", edit_btn
        assert edit_btn["color"] not in ("transparent", "rgba(0, 0, 0, 0)"), edit_btn
        page.click('[data-edit="NS"]')
        assert page.is_disabled("#p-key") and page.input_value("#p-name") == "nightshift"
        page.fill("#p-path", r"C:\Users\Simon Lomebrote\Projects\nightshift")
        page.click("#project-form button[type=submit]")
        page.wait_for_function("p => [...document.querySelectorAll('td')].some(td => td.textContent === p)",
                               arg=r"C:\Users\Simon Lomebrote\Projects\nightshift")
        assert page.is_disabled("#p-key")   # 저장 후에도 현재 프로젝트 관리 범위를 유지한다.
        # Description 자동 작성 — POST 요청, 저장 안 한 변경이면 확인 창, 결과 문구·Issue 링크(모의 응답)
        desc_posts = []
        page.route("**/api/projects/NS/description/request", lambda route: (desc_posts.append(route.request.method), route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"ref": "NS-99", "provider": "codex", "reused": False}))))
        page.click("#p-description-request")
        page.wait_for_selector("#p-description-status a[href='#/issue/NS-99']")
        assert desc_posts == ["POST"] and "선택된 에이전트: Codex" in page.inner_text("#p-description-status")
        page.fill("#p-description", "저장 안 한 설명")
        asked.clear(); answers[:] = [None]  # 전역 on_dialog가 첫 확인 창을 취소한다.
        page.click("#p-description-request")
        page.wait_for_function("() => !document.querySelector('#p-description-request').disabled")
        assert len(asked) == 1 and "저장하지 않은 설명" in asked[0] and desc_posts == ["POST"]
        with page.expect_response("**/api/projects/NS/description/request"):
            page.click("#p-description-request")
        assert len(asked) == 2 and desc_posts == ["POST", "POST"]
        page.unroute("**/api/projects/NS/description/request")
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
        page.wait_for_selector("#a-vendor option", state="attached")   # option은 visible로 잡히지 않는다
        page.select_option("#a-vendor", "openai")   # Vendor를 고르면 Model 목록이 그 vendor 것으로 바뀐다
        assert "gpt-6.1-sol" in page.eval_on_selector_all("#a-model option", "os => os.map(o => o.value)")
        page.select_option("#a-vendor", "anthropic")
        models = page.eval_on_selector_all("#a-model option", "os => os.map(o => o.value)")
        assert "claude-opus-5-5" in models and "gpt-6.1-sol" not in models, models
        page.fill("#a-name", "claude"); page.select_option("#a-model", "claude-opus-5-5")
        page.click("#agent-form button")
        page.wait_for_selector(".keybox code")
        key = page.inner_text("#key")
        assert key.startswith("dev_")
        page.click("[data-nav=issues]"); page.click("[data-nav=agents]")
        page.wait_for_selector("select[data-model]")
        assert page.eval_on_selector("select[data-model]", "s => s.value") == "claude-opus-5-5"
        assert page.locator(".keybox").count() == 0 and key not in page.content()
        # 목록 밖 Agent는 "목록 밖"으로 보이고 드롭다운으로 다시 고르면 목록 안으로 들어간다
        r = page.request.post(f"{BASE}/api/agents", data={"name": "legacy", "model": "free-text"}, headers={"X-Requested-With": "dev"})
        assert r.ok, r.text()
        lid = r.json()["agent"]["id"]
        page.click("[data-nav=issues]"); page.click("[data-nav=agents]")
        page.wait_for_selector(f'tr[data-agent="{lid}"] .off-catalog')
        assert "free-text (목록 밖)" in page.inner_text(f'select[data-model="{lid}"]')
        page.select_option(f'select[data-vendor="{lid}"]', "openai")
        page.select_option(f'select[data-model="{lid}"]', "gpt-6-luna")
        page.wait_for_function(f'!document.querySelector(\'tr[data-agent="{lid}"] .off-catalog\') && document.querySelector(\'select[data-model="{lid}"]\')?.value === "gpt-6-luna"')
        page.screenshot(path=str(shots / "agents.png"))

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
        # 첫 커서는 Description, 제출 버튼은 [발행](DEV-87)
        page.wait_for_function("document.activeElement && document.activeElement.id === 'n-body'")
        assert page.inner_text("#new-form button[type=submit]").strip() == "발행"
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
                # 위·아래(DEV-85) — 맨 끝이라 아래는 꺼지고 위만 켜진다
                assert page.locator('.exec .move-job[data-dir="up"]').is_enabled() and page.locator('.exec .move-job[data-dir="down"]').is_disabled()
                assert page.locator('.exec .move-job[data-dir="up"]').get_attribute("aria-label") == "DEV-4-2 대기 순서 앞으로 한 칸"
                if tag == "mobile":
                    box = page.locator('.exec .move-job[data-dir="up"]').bounding_box()
                    assert box["width"] >= 40 and box["height"] >= 40, box
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
        # 목록에서 위·아래(DEV-85) — 첫 항목의 위·끝 항목의 아래는 꺼짐, 옮긴 뒤에도 펼침과 포커스 유지
        order = lambda: [j["issue_id"] for j in page.request.get(f"{BASE}/api/jobs").json()["jobs"]]
        assert page.locator('.jobs li:first-child .move-job[data-dir="up"]').is_disabled()
        assert page.locator('.jobs li:last-child .move-job[data-dir="down"]').is_disabled()
        page.click('.jobs li:first-child .move-job[data-dir="down"]')
        page.wait_for_function("document.querySelector('.jobs li:first-child a').textContent === 'DEV-4-2'")
        assert order() == [i42, i41] and page.locator('details.jobs[open]').count() == 1
        assert page.evaluate("document.activeElement.matches('.jobs li:last-child .move-job[data-dir=\"up\"]')")
        # 다른 탭이 먼저 바꿨으면 409 — 화면은 최신 순서로 다시 그린다
        j42, j41 = (j["id"] for j in page.request.get(f"{BASE}/api/jobs").json()["jobs"])
        stale = page.request.post(f"{BASE}/api/jobs/{j41}/move", headers=H, data={"direction": "up", "neighbor_id": j41 + 999})
        assert stale.status == 409 and order() == [i42, i41]
        assert page.request.post(f"{BASE}/api/jobs/{j41}/move", headers=H, data={"direction": "up", "neighbor_id": j42}).ok   # 다른 탭
        page.click('.jobs li:last-child .move-job[data-dir="up"]')   # 화면이 본 이웃(j42)이 낡아 409
        page.wait_for_function("document.getElementById('toast').innerText.includes('바뀌었어요')")
        page.wait_for_function("document.querySelector('.jobs li:first-child a').textContent === 'DEV-4-1'")
        assert order() == [i41, i42] and page.locator('details.jobs[open]').count() == 1
        page.screenshot(path=str(shots / "queue_move_light_desktop.png"), full_page=True)
        page.click(".jobs li:first-child .cancel-job"); page.wait_for_function("document.getElementById('toast').innerText.includes('대기를 취소했어요')")
        page.wait_for_function("() => document.querySelector('.jobs summary') && document.querySelector('.jobs summary').innerText.includes('1건')")
        page.goto(BASE + f"/#/issue/{pr}"); page.reload(); page.wait_for_selector("tr.row")
        b1 = page.locator('.run-task[data-ref="DEV-4-1"][data-provider="claude"]')
        b2 = page.locator('.run-task[data-ref="DEV-4-2"]')
        assert b1.is_enabled() and "Claude" in b1.inner_text() and b2.is_disabled() and "실행 대기 중" in b2.inner_text()
        page.goto(BASE + "/#/issue/DEV-4-2"); page.reload(); page.wait_for_selector(".exec .queued")
        assert "대기 1번째" in page.inner_text(".exec")
        assert page.locator(".exec .move-job:disabled").count() == 2   # 하나뿐이면 둘 다 꺼짐
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
        check_rollback_waiting(page, shots)
        check_sticky_tabbar(page, shots)
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

        check_issue_types(page, shots)

        check_progress_refresh(page, shots)
        # 로그아웃(사용자 메뉴 안)
        page.click("#user-chip"); page.click("#logout"); page.wait_for_selector("#login:not([hidden])")
        assert page.locator('#open-jupyter').get_attribute('hidden') is not None
        assert not errs, errs
    print("OK")
finally:
    for pr in procs:
        pr.terminate()
