"""템플릿 옵션·복사본 기동·인증·자체 검사·공용 CSS를 로컬에서 검사한다."""
import itertools
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
    def test_catalog_and_validation(self):
        self.assertEqual(templates.catalog()[0]['id'], 'blank')
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
        self.assertEqual(sync_family.check(ROOT / 'templates/admin-web/static'), [])
        self.assertFalse(any('templates' in p.relative_to(ROOT).parts for p in test_runner.test_files(ROOT)))
        self.assertEqual((ROOT / 'templates/admin-web/docs/DESIGN.md').read_bytes(), (ROOT / 'docs/DESIGN.md').read_bytes())

    def test_option_matrix(self):
        fake = ThreadingHTTPServer(('127.0.0.1', 0), Nightshift)
        thread = threading.Thread(target=fake.serve_forever, daemon=True)
        thread.start()
        try:
            for switches in itertools.product((False, True), repeat=3):
                options = dict(zip(('login', 'db', 'dark_mode'), switches))
                with self.subTest(options=options), tempfile.TemporaryDirectory() as folder:
                    path = Path(folder)
                    files = templates.render('admin-web', options, VALUES)
                    self.assertEqual('server/auth.py' in files, options['login'])
                    self.assertEqual('server/db.py' in files, options['db'])
                    self.assertEqual('id="theme"' in files['static/index.html'], options['dark_mode'])
                    self.assertFalse(any('[option:' in text or '{{PROJECT_' in text for text in files.values()))
                    # 기존 validate의 경로·확장자 규칙을 모든 산출물에 적용한다.
                    validation = {'description': '검사용 앱', 'runtime': templates.get('admin-web')['runtime'],
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
                    result = subprocess.run([sys.executable, 'tests/test_app.py'], cwd=path, env=env, capture_output=True, text=True, encoding='utf-8', timeout=30)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    # 복사본 앱 모듈은 별도 프로세스에서 HTTP TestClient로 확인한다.
                    code = "import sys;sys.path.insert(0,'server');from app import app;from fastapi.testclient import TestClient;c=TestClient(app);"
                    if options['login']:
                        code += "assert c.get('/api/me').status_code==401;assert c.get('/api/me',cookies={'ns_session':'invalid'}).status_code==401;assert c.get('/api/me',cookies={'ns_session':'member'}).status_code==403;r=c.get('/api/me',cookies={'ns_session':'admin'});assert r.status_code==200;assert r.json()['user']=={'username':'tester','role':'admin'}"
                    else:
                        code += "assert c.get('/api/me').status_code==404"
                    result = subprocess.run([sys.executable, '-c', code], cwd=path, env=env, capture_output=True, text=True, encoding='utf-8', timeout=30)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    for name in ('static/app.js', 'ecosystem.config.js'):
                        result = subprocess.run(['node', '--check', name], cwd=path, capture_output=True, text=True, timeout=10)
                        self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            fake.shutdown()
            fake.server_close()
            thread.join()

    @unittest.skipUnless(CAPTURE, 'R04는 --capture로 실행한다')
    def test_capture(self):
        from playwright.sync_api import sync_playwright, expect
        # Dev 전용 capture_ui는 생성 앱을 지원하지 않아 복사본을 로컬 서버로 캡처한다.
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            copy_files(path, templates.render('admin-web', {}, VALUES))
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            env = {key: value for key, value in os.environ.items() if key.upper() in {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA'}}
            env.update(HOST='127.0.0.1', PORT=str(port), PYTHONDONTWRITEBYTECODE='1')
            process = subprocess.Popen([sys.executable, 'server/app.py'], cwd=path, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
                            expect(page.locator('#session')).to_have_text('nightshift에서 로그인한 뒤 새로고침해 주세요.')
                            expect(page.locator('html')).to_have_attribute('data-theme', theme)
                            self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                            output = ROOT / '.ui-captures' / 'admin-web'
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
