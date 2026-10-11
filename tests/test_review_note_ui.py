"""임시 서버에서 메모 재검토의 폼·요청·오류 보존과 네 화면을 확인한다."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import httpx
from playwright.sync_api import sync_playwright
from ui_fixture import FAKE_NS, BOOTSTRAP, child_environment, free_port, wait_port

ROOT = Path(__file__).resolve().parents[1]
directory = Path(tempfile.mkdtemp())
(directory / 'fake_ns.py').write_text(FAKE_NS, 'utf-8')
(directory / 'capture_app.py').write_text(BOOTSTRAP.replace('import app, uvicorn', 'import app, uvicorn, jobs\njobs.pump = lambda: None'), 'utf-8')
ns_port, dev_port = free_port(), free_port()
env = child_environment(directory, ns_port, dev_port)
base = f'http://127.0.0.1:{dev_port}'
processes = []
shots = ROOT / '.ui-captures' / 'review-note'
shots.mkdir(parents=True, exist_ok=True)
try:
    processes.append(subprocess.Popen([sys.executable, '-m', 'uvicorn', 'fake_ns:app', '--port', str(ns_port)], cwd=directory, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW))
    processes.append(subprocess.Popen([sys.executable, str(directory / 'capture_app.py'), str(ROOT / 'server'), str(ns_port), str(dev_port)], cwd=directory, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW))
    wait_port(ns_port); wait_port(dev_port)
    with httpx.Client(base_url=base, cookies={'ns_session': 'adm'}, headers={'X-Requested-With': 'dev'}) as client:
        client.post('/api/projects', json={'key': 'MO', 'name': 'Moai'}).raise_for_status()
        issue = client.post('/api/issues', json={'project': 'MO', 'title': '실제 모델 수집 연결', 'labels': ['goal']}).json()
        ref = issue['ref']
        client.post(f'/api/issues/{ref}/plans', json={'body': '## 방향\nSQLite 카탈로그를 웹 수집까지 확장한다.\n\n## Tasks\n1. 수집 연결 | 파일: backend/catalog/local.py | 확인: 결과 표시 | 선행: 없음'}).raise_for_status()
        approved_note = 'PostgreSQL 기준으로 계획서를 재작성해 주세요.'
        client.post(f'/api/issues/{ref}/decision', json={'verdict': 'approve_notes', 'note': approved_note, 'plan_version': 1}).raise_for_status()
        client.post(f'/api/issues/{ref}/plans', json={'body': '## Tasks\n1. PostgreSQL 연결 | 파일: backend/catalog/pg.py | 확인: 결과 표시 | 선행: 없음'}).raise_for_status()
        client.post(f'/api/issues/{ref}/decision', json={'verdict': 'approve_notes', 'note': approved_note, 'plan_version': 2}).raise_for_status()
        children = client.get(f'/api/issues/{ref}').json()['children']
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                context = browser.new_context()
                context.add_cookies([{'name': 'ns_session', 'value': 'adm', 'url': base}])
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                for theme in ('light', 'dark'):
                    for width in (1300, 390):
                        page.set_viewport_size({'width': width, 'height': 900})
                        page.goto(base)
                        page.evaluate("theme => {localStorage.setItem('dev.theme',theme);document.documentElement.dataset.theme=theme}", theme)
                        page.goto(base + '/#/issue/' + ref)
                        page.locator('[data-verdict="review"]').click()
                        assert page.input_value('#decision-note') == approved_note
                        page.locator('#superseded-tasks summary').click()
                        assert page.locator('#superseded-tasks a').count() == 1
                        page.fill('#decision-note', 'SQLite는 임시예요. PostgreSQL로 통일하고 기존 3건을 이관해 주세요.')
                        page.select_option('#revision-provider', 'codex')
                        page.locator('#decision-form').scroll_into_view_if_needed()
                        assert page.locator('#decision-send').inner_text() == '메모 붙여 재검토'
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                        page.evaluate('window.scrollTo(0,0)')
                        page.screenshot(path=str(shots / f'{theme}-{width}.png'), full_page=True)
                note = page.input_value('#decision-note')
                page.route('**/api/issues/*/review', lambda route: route.fulfill(status=503, content_type='application/json', body='{"detail":"임시 오류"}'))
                page.click('#decision-send')
                page.wait_for_function("!document.querySelector('#decision-send').disabled")
                assert page.input_value('#decision-note') == note
                assert not client.get('/api/jobs').json()['jobs']
                page.unroute('**/api/issues/*/review')
                page.click('#decision-send')
                page.locator('#decision-form').wait_for(state='detached')
                result = client.get(f'/api/issues/{ref}').json()
                assert result['plan']['revision_note'] == note
                assert result['approval']['stale'] and result['approval']['verdict'] == 'approve_notes'
                assert len(result['children']) == len(children) and result['status'] != 'closed'
                assert len(result['superseded_children']) == 1
                queued = client.get('/api/jobs').json()['jobs']
                assert len(queued) == 1 and queued[0]['provider'] == 'codex'
                assert not errors, errors
            finally:
                browser.close()
finally:
    for process in processes:
        process.terminate()
        process.wait(timeout=10)
print('review-note UI: four captures, provider, failed memo retention, queued without approval OK')
