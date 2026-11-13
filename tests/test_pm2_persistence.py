"""서비스 연결의 PM2 저장 순서·실패 경고·복구 무저장·실행 환경을 모의 명령과 임시 DB로 검사한다."""
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

TEMP = tempfile.TemporaryDirectory(prefix='dev-pm2-save-')
ROOT = Path(TEMP.name)
os.environ['DEV_DATA_DIR'] = str(ROOT / 'data')
os.environ['DEV_DB_PATH'] = str(ROOT / 'data' / 'dev.db')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import db
import issues
import project_service as service

db.init()


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {'PM2_HOME': str(ROOT / 'pm2'), 'HOME': str(ROOT / 'home')}))
        self.repo = ROOT / 'repo'
        self.repo.mkdir(exist_ok=True)
        (self.repo / 'ecosystem.config.js').write_text('module.exports={apps:[]}', encoding='utf-8')
        self.path = ROOT / 'tunnel.yml'
        self.path.write_bytes(b'original')
        with db.connect() as c:
            c.execute('DELETE FROM project_services')
            c.execute("INSERT OR IGNORE INTO projects(key,name,created_at) VALUES('SAVE','save',?)", (db.now_iso(),))
            self.pid = c.execute("SELECT id FROM projects WHERE key='SAVE'").fetchone()[0]
            c.execute("INSERT INTO project_services(project_id,status,actor,action,updated_at) VALUES(?,'running','human:admin','connect',?)", (self.pid, db.now_iso()))
        self.project = dict(id=self.pid, service_domain='test.example.com', service_port=8401,
                            service_app='test', local_path=str(self.repo))
        self.events = []
        self.calls = []
        self.fail = None
        self.save_error = None
        self.restored = []
        mocks = {
            'config_path': lambda: self.path,
            'binary': lambda: 'fake-cloudflared',
            '_restart_permission': lambda: None,
            '_candidate': lambda *args: (b'candidate', 't1'),
            'pm2_launcher': lambda: ['fake-node', 'fake-pm2'],
            'app_environment': lambda *args: {'PM2_HOME': 'project-daemon', 'HOME': 'project-home', 'PORT': '8401', 'APP_SECRET': 'private'},
            '_command': self.command,
            '_healthy': self.healthy,
            '_switch': self.switch,
            '_restore': self.restore,
        }
        for name, value in mocks.items():
            self.stack.enter_context(patch.object(service, name, value))

    def restore(self, row):
        self.restored.append(row)
        service._save(self.pid, backup_path='', applied_hash='')
        return ' 복구했어요.'

    def command(self, args, label, cwd=None, environment=None, timeout=45):
        event = 'start' if 'startOrRestart' in args else 'save' if args[-1] == 'save' else 'dns' if 'dns' in args else 'validate'
        self.events.append(event)
        self.calls.append((event, args, environment, timeout))
        if event == self.fail:
            raise issues.StoreError('모의 실패', 409)
        if event == 'save' and self.save_error:
            raise self.save_error

    def healthy(self, url):
        event = 'external' if url.startswith('https:') else 'local'
        self.events.append(event)
        if event == self.fail:
            raise issues.StoreError('모의 health 실패', 409)

    def switch(self, *args):
        self.events.append('switch')
        if self.fail == 'switch':
            raise issues.StoreError('모의 전환 실패', 409)

    def run_connection(self, action='connect'):
        service._run(self.project, action)
        return service._row(self.pid)

    def test_success_order_and_environment(self):
        self.assertEqual(self.run_connection(), {'status': 'connected', 'error': ''})
        self.assertEqual(self.events, ['validate', 'start', 'local', 'dns', 'switch', 'external', 'save'])
        start, save = [call for call in self.calls if call[0] in ('start', 'save')]
        self.assertEqual(start[1][:2], save[1][:2])
        for key in ('PM2_HOME', 'HOME', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH'):
            self.assertEqual(start[2].get(key), save[2].get(key))
        self.assertNotIn('APP_SECRET', save[2])
        self.assertEqual(save[3], 45)
        self.assertFalse(self.restored)

    def test_failures_never_save(self):
        for event in ('validate', 'start', 'local', 'dns', 'switch', 'external'):
            with self.subTest(event=event):
                self.events.clear()
                self.fail = event
                self.assertEqual(self.run_connection()['status'], 'failed')
                self.assertNotIn('save', self.events)
        self.assertEqual(len(self.restored), 6)

    def test_save_failure_keeps_success_and_retry_clears_warning(self):
        for error in (issues.StoreError('save failed', 409), OSError('missing'), subprocess.TimeoutExpired('pm2', 45)):
            with self.subTest(error=type(error).__name__):
                self.save_error = error
                self.assertEqual(self.run_connection(), {'status': 'connected', 'error': service.PM2_SAVE_WARNING})
                self.assertFalse(self.restored)
                self.save_error = None
                self.assertEqual(self.run_connection(), {'status': 'connected', 'error': ''})

    def test_skipped_apps_and_disconnect_do_not_save(self):
        for app, action in (('', 'connect'), ('dev', 'connect'), ('test', 'disconnect')):
            with self.subTest(app=app, action=action):
                self.events.clear()
                self.project['service_app'] = app
                self.assertEqual(self.run_connection(action)['status'], 'disconnected' if action == 'disconnect' else 'connected')
                self.assertNotIn('save', self.events)
                self.assertNotIn('start', self.events)

    def test_unset_pm2_home_ignores_project_override(self):
        os.environ.pop('PM2_HOME', None)
        self.assertEqual(self.run_connection()['status'], 'connected')
        for event, args, env, timeout in self.calls:
            if event in ('start', 'save'):
                self.assertNotIn('PM2_HOME', env)
                self.assertEqual(env['HOME'], str(ROOT / 'home'))

    def test_save_helper_handles_launcher_failure(self):
        with patch.object(service, 'pm2_launcher', side_effect=OSError('missing')):
            self.assertEqual(service.save_pm2(), service.PM2_SAVE_WARNING)


if __name__ == '__main__':
    try:
        unittest.main()
    finally:
        TEMP.cleanup()
