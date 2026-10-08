"""캡처 격리·실제 PNG·실패 기록·자식 종료를 검사한다."""
import ast
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('capture_ui', ROOT / 'scripts/capture_ui.py')
capture_ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture_ui)


class CaptureTests(unittest.TestCase):
    def test_environment_and_routes(self):
        with patch.dict(os.environ, {'DEV_DB_PATH': 'production.db', 'RUNPOD_API_KEY': 'secret',
                                    'DEV_AGENT_KEY': 'secret', 'PYTHONPATH': 'unsafe', 'NTFY_TOKEN': 'secret'}):
            env = capture_ui.child_environment(ROOT / '.ui-captures', 1, 2)
        self.assertEqual(env['DEV_DB_PATH'], str(ROOT / '.ui-captures/data/dev.db'))
        for key in ('RUNPOD_API_KEY', 'DEV_AGENT_KEY', 'PYTHONPATH', 'NTFY_TOKEN'):
            self.assertNotIn(key, env)
        with patch.dict(os.environ, {'RUNPOD_API_KEY': 'secret'}):
            with capture_ui.isolated_environment(env):
                self.assertNotIn('RUNPOD_API_KEY', os.environ)
            self.assertEqual(os.environ['RUNPOD_API_KEY'], 'secret')
        for route in ('https://example.com', '#//example.com', '#/unknown', '#/../data', '#/issue/DEV-1/extra'):
            with self.assertRaises(ValueError):
                capture_ui.validate_route(route)
        self.assertTrue(capture_ui.local_request('http://127.0.0.1:12/static/a.js', 'http://127.0.0.1:12'))
        for url in ('https://example.com', 'http://127.0.0.1:13', 'file:///data', 'http://127.0.0.1:12@example.com'):
            self.assertFalse(capture_ui.local_request(url, 'http://127.0.0.1:12'))

    def test_fixture_separation(self):
        tree = ast.parse((ROOT / 'tests/test_ui.py').read_text(encoding='utf-8'))
        self.assertTrue(any(isinstance(node, ast.ImportFrom) and node.module == 'ui_fixture' for node in tree.body))
        self.assertNotIn('jobs.start_timer', capture_ui.BOOTSTRAP)
        self.assertIn("lifespan='off'", capture_ui.BOOTSTRAP)
        self.assertNotIn('dotenv', capture_ui.BOOTSTRAP)
        fixture = ast.parse((ROOT / 'tests/ui_fixture.py').read_text(encoding='utf-8'))
        self.assertFalse(any(isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) for n in fixture.body))

    def test_external_connections_blocked(self):
        # 주소 접속 전에 차단하므로 실제 외부 네트워크를 사용하지 않는다.
        prefix = capture_ui.BOOTSTRAP.split('import db')[0]
        script = prefix + "\nfor action in (lambda: socket.socket().connect(('203.0.113.1', 443)), lambda: socket.socket().connect_ex(('203.0.113.1', 443)), lambda: socket.getaddrinfo('example.com', 443)):\n    try:\n        action()\n    except OSError as exc:\n        assert str(exc) == 'capture: external connection blocked'\n    else:\n        raise AssertionError('external connection allowed')\n"
        result = capture_ui.subprocess.run([sys.executable, '-c', script, str(ROOT / 'server'), '1', '2'], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        from playwright.sync_api import sync_playwright
        env = capture_ui.child_environment(ROOT / '.ui-captures', 1, 2)
        with capture_ui.isolated_environment(env), sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=['--enable-logging=stderr'])
            try:
                context = browser.new_context(service_workers='block')
                capture_ui.guard_browser(context, 'http://127.0.0.1:12')
                page = context.new_page()
                failed = []
                page.on('requestfailed', lambda request: failed.append(request.failure))
                page.set_content('<html></html>')
                self.assertFalse(page.evaluate("async () => {try {await fetch('https://example.com/');return true} catch {return false}}"))
                self.assertIn('net::ERR_FAILED', failed)
            finally:
                browser.close()

    def test_startup_failure_cleanup(self):
        processes = []
        original = capture_ui.subprocess.Popen
        def start(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process
        with patch.object(capture_ui.subprocess, 'Popen', side_effect=start), patch.object(capture_ui, 'wait_port', side_effect=RuntimeError('startup timeout')):
            directory, manifest = capture_ui.capture()
        self.assertEqual(manifest['status'], 'failed')
        self.assertEqual(manifest['errors'][0]['stage'], 'startup')
        self.assertTrue(all(p.poll() is not None for p in processes))
        self.assertEqual(json.loads((directory / 'manifest.json').read_text(encoding='utf-8')), manifest)

    def test_real_captures_and_partial_failure(self):
        from playwright.sync_api import Page
        directory, manifest = capture_ui.capture('#/issue/DEV-1', 'detail')
        self.assertEqual(manifest['status'], 'ok', manifest['errors'])
        self.assertEqual({(c['theme'], c['width'], c['height']) for c in manifest['captures']},
                         {(t, w, h) for t in ('light', 'dark') for w, h in ((1300, 850), (390, 800))})
        for item in manifest['captures']:
            self.assertEqual((ROOT / item['path']).read_bytes()[:8], b'\x89PNG\r\n\x1a\n')
        original = Page.screenshot
        def screenshot(page, *args, **kwargs):
            if kwargs['path'].endswith('dark-390.png'):
                raise RuntimeError('screenshot failure')
            return original(page, *args, **kwargs)
        with patch.object(Page, 'screenshot', screenshot):
            _, partial = capture_ui.capture()
        self.assertEqual(partial['status'], 'partial')
        self.assertEqual(len(partial['captures']), 3)
        self.assertEqual(partial['errors'][0]['stage'], 'route/dark/390')

    def test_login_failure(self):
        bad_ns = capture_ui.FAKE_NS.replace('!= "pw"', '!= "different"')
        with patch.object(capture_ui, 'FAKE_NS', bad_ns):
            _, manifest = capture_ui.capture()
        self.assertEqual(manifest['status'], 'failed')
        self.assertEqual(manifest['errors'][0]['stage'], 'login')

    def test_missing_issue_route(self):
        _, manifest = capture_ui.capture('#/issue/DEV-999')
        self.assertEqual(manifest['status'], 'failed')
        self.assertEqual(len(manifest['errors']), 4)
        self.assertTrue(all(e['stage'].startswith('route/') for e in manifest['errors']))


if __name__ == '__main__':
    unittest.main()
