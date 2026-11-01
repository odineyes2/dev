"""템플릿 옵션·복사본 기동·인증·자체 검사·공용 CSS를 로컬에서 검사한다."""
import itertools
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
sys.path.insert(0, str(ROOT / 'scripts'))
# 개발 DB 대신 임시 데이터 경로만 사용한다.
_DATA = tempfile.TemporaryDirectory()
os.environ['DEV_DATA_DIR'] = _DATA.name
import project_templates as templates
import project_init as pi
import sync_family
import test_runner

VALUES = {'PROJECT_NAME': '새 관리 도구', 'PROJECT_KEY': 'TEST', 'APP_NAME': 'template-test', 'PORT': '8400'}
CAPTURE = '--capture' in sys.argv
if CAPTURE:
    sys.argv.remove('--capture')


def copy_files(path, files):
    for name, content in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')


class Nightshift(BaseHTTPRequestHandler):
    def do_GET(self):
        cookie = self.headers.get('Cookie')
        role = {'ns_session=admin': 'admin', 'ns_session=member': 'member'}.get(cookie)
        if self.path != '/api/auth/me' or not role:
            self.send_response(401)
            self.end_headers()
            return
        data = json.dumps({'user': {'username': 'tester', 'role': role}}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class TemplatesTest(unittest.TestCase):
    def test_genre_overlay_and_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            base = root / 'fixture'
            copy_files(base, {
                'template.json': json.dumps({'id': 'fixture', 'options': {'scores': {'default': True, 'drop': ['db.py']}},
                    'default_genre': 'arcade', 'genres': [{'id': 'arcade', 'options': ['scores']}, {'id': 'novel', 'options': []}]}),
                'app.txt': 'base', 'db.py': 'database',
                'genres/novel/app.txt': '{{PROJECT_KEY}}\n# [option:scores]\nscore\n# [/option:scores]\n',
                'genres/novel/new.txt': 'added', 'genres/unused/unused.txt': 'unused'})
            with patch.object(templates, 'ROOT', root):
                self.assertEqual(templates.render('fixture', {}, VALUES), {'app.txt': 'base', 'db.py': 'database'})
                self.assertEqual(templates.render('fixture', {}, VALUES, genre='novel'), {'app.txt': 'TEST\n', 'new.txt': 'added'})
                for genre, options in [('missing', {}), ('../novel', {}), (['novel'], {}), ('novel', {'scores': False})]:
                    with self.assertRaises(templates.StoreError):
                        templates.render('fixture', options, VALUES, genre=genre)
                hidden = base / 'genres/novel/.hidden'
                hidden.write_text('hidden')
                with self.assertRaises(templates.StoreError):
                    templates.render('fixture', {}, VALUES, genre='novel')
        for template in ('blank', 'admin-web'):
            with self.assertRaises(templates.StoreError):
                templates.render(template, {}, VALUES, genre='arcade')

    def test_arcade_output_unchanged(self):
        hashes = [
            '615c99da3323cb63bb6c495d30af5f8ab0a6a98cc0bf09a5ec5d5fe537f05bc9',
            'a1763e75cbb6b2f6d9d079117609bb88dd1cb28d8fd978807ae3b8f36db50ddd',
            'ea42156627e2910243cc670d3451e0c33ff244f5034a88601f30b73623baa5fa',
            '9f3a7bdbdf1200060b8eccdd8683317a4a06db50fd7c927294dedb9c596ec4c8']
        for (scores, dark), expected in zip(itertools.product((False, True), repeat=2), hashes):
            for genre in (None, 'arcade'):
                files = templates.render('web-game', {'scores': scores, 'dark_mode': dark}, VALUES, genre=genre)
                actual = hashlib.sha256(''.join(p + '\0' + files[p] for p in sorted(files)).encode()).hexdigest()
                self.assertEqual(actual, expected)

    def test_catalog_and_validation(self):
        self.assertEqual([m['id'] for m in templates.catalog()], ['blank', 'admin-web', 'service-split', 'web-game'])
        self.assertTrue(all(m['provides'] for m in templates.catalog()))
        game = next(m for m in templates.catalog() if m['id'] == 'web-game')
        self.assertEqual(game['default_genre'], 'arcade')
        self.assertEqual(game['genres'][0]['options'], ['scores', 'dark_mode'])
        self.assertEqual(templates.render('blank', {}, {}), {})
        for template_id in ('../admin-web', 'missing', None):
            with self.assertRaises(templates.StoreError):
                templates.get(template_id)
        for options in ({'unknown': True}, {'login': 1}, {'db': 'false'}, None):
            with self.assertRaises(templates.StoreError):
                templates.render('admin-web', options, VALUES)
        for changes in ({'PORT': '0'}, {'PORT': '65536'}, {'APP_NAME': "bad'code"}, {'PROJECT_NAME': '<script>'}):
            with self.assertRaises(templates.StoreError):
                templates.render('admin-web', {}, {**VALUES, **changes})
        for text in ('# [option:login]\n', '# [/option:db]\n', '# [option:unknown]\n'):
            with self.assertRaises(templates.StoreError):
                templates._blocks(text, {'login': True, 'db': True})
        self.assertEqual(templates._blocks('# [option:login]\n// [option:db]\nx\n// [/option:db]\n# [/option:login]\n', {'login': True, 'db': False}), '')

    def test_family_and_collection(self):
        for template_id in ('admin-web', 'service-split', 'web-game'):
            self.assertEqual(sync_family.check(ROOT / f'templates/{template_id}/static'), [])
            self.assertEqual((ROOT / f'templates/{template_id}/docs/DESIGN.md').read_bytes(), (ROOT / 'docs/DESIGN.md').read_bytes())
        self.assertFalse(any('templates' in p.relative_to(ROOT).parts for p in test_runner.test_files(ROOT)))

    def test_option_matrix(self):
        self.check_option_matrix('admin-web')

    def test_service_split(self):
        self.check_option_matrix('service-split')

    def test_web_game(self):
        for genre in (g['id'] for g in templates.get('web-game')['genres']):
            for scores, dark in itertools.product((False, True), repeat=2):
                options = {'scores': scores, 'dark_mode': dark}
                with self.subTest(options=options), tempfile.TemporaryDirectory() as folder:
                    path = Path(folder)
                    files = templates.render('web-game', options, VALUES, genre=genre)
                    self.assertEqual('server/db.py' in files, scores)
                    self.assertEqual('server/migrations/001_scores.sql' in files, scores)
                    self.assertEqual('id="scores"' in files['static/index.html'], scores)
                    self.assertEqual('id="theme"' in files['static/index.html'], dark)
                    self.assertFalse(any('[option:' in text or '{{' in text for text in files.values()))
                    validation = {'description': '검사용 게임', 'runtime': templates.get('web-game')['runtime'],
                                  'files': [{'path': n, 'content': t} for n, t in files.items()], 'issues': []}
                    validation['files'] += [{'path': n, 'content': '# 문서\n'} for n in pi.REQUIRED - files.keys()]
                    with self.assertRaisesRegex(ValueError, 'Issue 개수'):
                        pi.validate(validation, path)
                    copy_files(path, files)
                    row = {'request_id': 'game-check', 'key': 'TEST', 'template': 'web-game', 'genre': genre, 'options_json': json.dumps(options), 'service_port': 8400, 'service_app': 'template-test'}
                    with patch.object(pi.config, 'DATA_DIR', path / 'verify-data'), patch.object(pi, '_checkpoint'), patch.object(pi, '_update'):
                        pi.verify_runtime(row, validation, path)
                    env = {k: v for k, v in os.environ.items() if k.upper() in {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA'}}
                    env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
                    for command in ([sys.executable, 'tests/test_app.py'], ['node', 'tests/test_game.js'],
                                    *[['node', '--check', n] for n in ('static/app.js', 'static/game.js', 'ecosystem.config.js', 'tests/test_game.js')]):
                        result = subprocess.run(command, cwd=path, env=env, capture_output=True, text=True, encoding='utf-8', timeout=30)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_genre_common_blocks(self):
        import re
        base = ROOT / 'templates/web-game'
        for genre in templates.get('web-game')['genres']:
            if genre['id'] == 'arcade':
                continue
            for name in ('app.js', 'index.html'):
                source = (base / 'static' / name).read_text(encoding='utf-8')
                overlay = (base / 'genres' / genre['id'] / 'static' / name).read_text(encoding='utf-8')
                for option in genre['options']:
                    pattern = rf'[^\n]*\[option:{option}\].*?[^\n]*\[/option:{option}\][^\n]*'
                    self.assertEqual(re.findall(pattern, source, re.S), re.findall(pattern, overlay, re.S))
                if name == 'index.html':
                    self.assertEqual(source.split('<header')[1].split('</header>')[0], overlay.split('<header')[1].split('</header>')[0])

    def check_option_matrix(self, template_id):
        fake = ThreadingHTTPServer(('127.0.0.1', 0), Nightshift)
        thread = threading.Thread(target=fake.serve_forever, daemon=True)
        thread.start()
        try:
            for switches in itertools.product((False, True), repeat=3):
                options = dict(zip(('login', 'db', 'dark_mode'), switches))
                with self.subTest(options=options), tempfile.TemporaryDirectory() as folder:
                    path = Path(folder)
                    files = templates.render(template_id, options, VALUES)
                    package = 'server' if template_id == 'admin-web' else 'backend'
                    self.assertEqual(f'{package}/auth.py' in files, options['login'])
                    self.assertEqual(f'{package}/db.py' in files, options['db'])
                    self.assertEqual('id="theme"' in files['static/index.html'], options['dark_mode'])
                    self.assertFalse(any('[option:' in text or '{{PROJECT_' in text for text in files.values()))
                    # 기존 validate의 경로·확장자 규칙을 모든 산출물에 적용한다.
                    validation = {'description': '검사용 앱', 'runtime': templates.get(template_id)['runtime'],
                                  'files': [{'path': name, 'content': text} for name, text in files.items()], 'issues': []}
                    validation['files'] += [{'path': name, 'content': '# 문서\n'} for name in pi.REQUIRED - files.keys()]
                    with self.assertRaisesRegex(ValueError, 'Issue 개수'):
                        pi.validate(validation, path)
                    copy_files(path, files)
                    row = {'request_id': 'template-check', 'key': 'TEST', 'service_port': 8400, 'service_app': 'template-test'}
                    with patch.object(pi.config, 'DATA_DIR', path / 'verify-data'), patch.object(pi, '_checkpoint'), patch.object(pi, '_update'):
                        pi.verify_runtime(row, validation, path)
                    env = {key: value for key, value in os.environ.items() if key.upper() in {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA'}}
                    env.update(NIGHTSHIFT_URL=f'http://127.0.0.1:{fake.server_port}', PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
                    result = subprocess.run([sys.executable, 'tests/test_app.py' if template_id == 'admin-web' else 'backend/tests/test_serve.py'], cwd=path, env=env, capture_output=True, text=True, encoding='utf-8', timeout=30)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    # 복사본 앱 모듈은 별도 프로세스에서 HTTP TestClient로 확인한다.
                    code = "import sys;sys.path.insert(0,'server');from app import app;from fastapi.testclient import TestClient;c=TestClient(app);"
                    if template_id == 'service-split':
                        code = "from backend.serve import app;from fastapi.testclient import TestClient;c=TestClient(app);"
                        code += "assert c.get('/api/health').json()['db']==" + repr('not_configured' if options['db'] else 'disabled') + ";assert c.get('/api/ready').status_code==200;"
                    if options['login']:
                        code += "assert c.get('/api/me').status_code==401;assert c.get('/api/me',cookies={'ns_session':'invalid'}).status_code==401;assert c.get('/api/me',cookies={'ns_session':'member'}).status_code==403;r=c.get('/api/me',cookies={'ns_session':'admin'});assert r.status_code==200;assert r.json()['user']=={'username':'tester','role':'admin'}"
                    else:
                        code += "assert c.get('/api/me').status_code==404"
                    result = subprocess.run([sys.executable, '-c', code], cwd=path, env=env, capture_output=True, text=True, encoding='utf-8', timeout=30)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    for name in ('static/app.js', 'ecosystem.config.js'):
                        # 병렬 검사 중 Node 시작 지연에도 문법 검사 결과를 기다린다.
                        result = subprocess.run(['node', '--check', name], cwd=path, capture_output=True, text=True, timeout=60)
                        self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            fake.shutdown()
            fake.server_close()
            thread.join()

    def test_service_split_postgres(self):
        url = os.environ.get('DEV_TEST_DATABASE_URL') or os.environ.get('MOAI_TEST_DATABASE_URL')
        if not url:
            print('SKIP: 격리 PostgreSQL 주소가 없어 001_core.sql 적용 검사를 건너뛴다.')
            self.skipTest('격리 PostgreSQL 없음')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            copy_files(path, templates.render('service-split', {}, VALUES))
            env = {key: value for key, value in os.environ.items() if key.upper() in {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA'}}
            env.update(DATABASE_URL=url, PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
            result = subprocess.run([sys.executable, 'backend/tests/test_serve.py'], cwd=path, env=env, capture_output=True, text=True, encoding='utf-8', timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(CAPTURE, 'R04는 --capture로 실행한다')
    def test_capture(self):
        self.capture_template('admin-web')

    @unittest.skipUnless(CAPTURE, 'R04는 --capture로 실행한다')
    def test_service_split_capture(self):
        self.capture_template('service-split')

    @unittest.skipUnless(CAPTURE, 'R04는 --capture로 실행한다')
    def test_web_game_capture(self):
        for genre in templates.get('web-game')['genres']:
            self.capture_template('web-game', genre['id'])

    def capture_template(self, template_id, genre=None):
        from playwright.sync_api import sync_playwright, expect
        # Dev 전용 capture_ui는 생성 앱을 지원하지 않아 복사본을 로컬 서버로 캡처한다.
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            copy_files(path, templates.render(template_id, {}, VALUES, genre=genre))
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            env = {key: value for key, value in os.environ.items() if key.upper() in {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA'}}
            env.update(HOST='127.0.0.1', PORT=str(port), PYTHONDONTWRITEBYTECODE='1')
            process = subprocess.Popen([sys.executable, templates.get(template_id)['runtime']['entry']], cwd=path, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                import httpx, time
                deadline = time.monotonic() + 20
                while True:
                    try:
                        if httpx.get(f'http://127.0.0.1:{port}/api/health', trust_env=False).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if time.monotonic() > deadline:
                        self.fail('캡처 서버 기동 시간 초과')
                    time.sleep(0.1)
                with sync_playwright() as pw:
                    browser = pw.chromium.launch()
                    try:
                        for theme, width in itertools.product(('light', 'dark'), (1300, 390)):
                            page = browser.new_page(viewport={'width': width, 'height': 850}, color_scheme=theme)
                            page.route('**/*', lambda route: route.continue_() if route.request.url.startswith(f'http://127.0.0.1:{port}/') else route.abort())
                            page.goto(f'http://127.0.0.1:{port}/')
                            if template_id == 'web-game':
                                expect(page.locator('#game')).to_be_visible()
                                expect(page.locator('#status')).to_contain_text('플레이 중이에요.')
                                page.locator('#pause').click()
                                expect(page.locator('#status')).to_contain_text('멈췄어요.')
                                page.locator('#restart').click()
                                expect(page.locator('#status')).to_contain_text('플레이 중이에요.')
                            else:
                                expect(page.locator('#session')).to_have_text('nightshift에서 로그인한 뒤 새로고침해 주세요.')
                            if genre == 'puzzle':
                                original = page.locator('#game').get_attribute('data-board')
                                page.locator('#game button:enabled').first.click()
                                expect(page.locator('#status')).to_contain_text('이동 1')
                                self.assertNotEqual(page.locator('#game').get_attribute('data-board'), original)
                                page.locator('#undo').click()
                                expect(page.locator('#status')).to_contain_text('이동 0')
                                expect(page.locator('#game')).to_have_attribute('data-board', original)
                                expect(page.locator('#undo')).to_be_disabled()
                            if genre == 'card':
                                expect(page.locator('#game')).to_have_attribute('data-phase', 'player')
                                expect(page.locator('#player .card')).to_have_count(2)
                                page.locator('#hit').click()
                                expect(page.locator('#player .card')).to_have_count(3)
                                if page.locator('#stand').is_enabled():
                                    page.locator('#stand').click()
                                expect(page.locator('#game')).to_have_attribute('data-phase', 'result')
                                expect(page.locator('#hit')).to_be_disabled()
                                expect(page.locator('#stand')).to_be_disabled()
                                page.locator('#next').click()
                                expect(page.locator('#game')).to_have_attribute('data-phase', 'player')
                                page.locator('#stand').click()
                                expect(page.locator('#game')).to_have_attribute('data-phase', 'result')
                            if genre == 'platformer':
                                page.locator('#game').focus()
                                page.wait_for_function("Number(document.querySelector('#game').dataset.playerY) === 408")
                                page.locator('[data-control=jump]').dispatch_event('pointerdown', {'pointerId': 1})
                                page.wait_for_function("Number(document.querySelector('#game').dataset.playerY) < 400")
                                page.locator('[data-control=jump]').dispatch_event('pointerup', {'pointerId': 1})
                                page.locator('#pause').click()
                            expect(page.locator('html')).to_have_attribute('data-theme', theme)
                            self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                            output = ROOT / '.ui-captures' / (f'{template_id}-{genre}' if genre else template_id)
                            output.mkdir(parents=True, exist_ok=True)
                            page.screenshot(path=str(output / f'{theme}-{width}.png'), full_page=True)
                            page.close()
                    finally:
                        browser.close()
            finally:
                process.terminate()
                process.wait(timeout=10)


if __name__ == '__main__':
    unittest.main()
