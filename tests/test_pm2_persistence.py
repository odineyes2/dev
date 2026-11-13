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


class RestartPersistenceTests(PersistenceTests):
    def test_restart_helper_health_and_save_warning(self):
        import project_restart as restart
        from types import SimpleNamespace
        for own, mode, failure in ((0, 'restart', ''), (1, 'restart', ''),
                                   (0, 'deploy', ''), (0, 'deploy', 'release'),
                                   (0, 'restart', 'health')):
            with self.subTest(own=own, mode=mode, failure=failure), ExitStack() as stack:
                now = db.now_iso()
                with db.connect() as c:
                    aid = c.execute("""INSERT INTO project_app_restarts
                        (project_id,actor,app,repo,health_url,self_restart,process_id,status,phase,created_at,updated_at,mode,build_json)
                        VALUES(?,'human:admin','test',?,'http://127.0.0.1:8401/api/health',?,'old','waiting','',?,?,?,'[]')""",
                        (self.pid, str(self.repo), own, now, now, mode)).lastrowid
                folder = ROOT / 'build'
                folder.mkdir(exist_ok=True)
                (folder / 'manifest.json').write_text('{"outputs":[],"log":""}')
                events = []
                stack.enter_context(patch.object(restart, '_busy', return_value=False))
                stack.enter_context(patch.object(restart, '_restart', side_effect=lambda row: events.append('restart')))
                stack.enter_context(patch.object(restart.time, 'sleep'))
                stack.enter_context(patch.object(restart, 'HEALTH_SECONDS', 0 if failure == 'health' else 60))
                client = stack.enter_context(patch.object(restart.httpx, 'Client')).return_value.__enter__.return_value
                client.get.return_value = SimpleNamespace(status_code=200, json=lambda: {'process_id': 'new'})
                stack.enter_context(patch.object(restart.project_build, '_git', return_value='sha'))
                stack.enter_context(patch.object(restart.project_build, 'build', return_value=folder))
                stack.enter_context(patch.object(restart.project_build, 'deploy'))
                restore = stack.enter_context(patch.object(restart.project_build, 'restore'))
                def verify(*args):
                    events.append('release')
                    if failure == 'release':
                        raise restart.project_build.BuildError('release', 'mismatch')
                    return 'existing warning'
                stack.enter_context(patch.object(restart, '_verify_release', side_effect=verify))
                save = stack.enter_context(patch.object(service, 'save_pm2', side_effect=lambda: events.append('save') or service.PM2_SAVE_WARNING))
                restart.run(aid)
                with db.connect() as c:
                    row = dict(c.execute('SELECT * FROM project_app_restarts WHERE id=?', (aid,)).fetchone())
                self.assertEqual(row['status'], 'failed' if failure else 'complete')
                self.assertEqual(save.call_count, 0 if failure else 1)
                if not failure:
                    self.assertIn(service.PM2_SAVE_WARNING, row['error'])
                    self.assertEqual(events[-1], 'save')
                    if mode == 'deploy':
                        self.assertIn('existing warning', row['error'])
                if failure == 'release':
                    restore.assert_called_once()

    def test_deploy_health_failure_recovery_never_saves(self):
        import orchestrate as o
        from types import SimpleNamespace
        for healthy in (True, False):
            with self.subTest(healthy=healthy), ExitStack() as stack:
                stack.enter_context(patch.object(o, '_git', return_value=SimpleNamespace(stdout='server/app.py', stderr='', returncode=0)))
                stack.enter_context(patch.object(o, '_restart_guard', return_value=('', ())))
                stack.enter_context(patch.object(o, '_busy', return_value=False))
                stack.enter_context(patch.object(o, '_healthy', return_value=healthy))
                stack.enter_context(patch.object(o, '_restart', return_value=None))
                save = stack.enter_context(patch.object(service, 'save_pm2', return_value=service.PM2_SAVE_WARNING))
                state, note = o.deploy(str(self.repo), {'pm2_app': 'test'}, 'sha')
                self.assertEqual(state, 'merged' if healthy else 'changes_requested')
                self.assertEqual(save.call_count, int(healthy))
                if healthy:
                    self.assertIn(service.PM2_SAVE_WARNING, note)

    def test_worker_only_restarts_registered_apps_without_save(self):
        import orchestrate as o
        with patch.object(service, 'pm2_app_registered', side_effect=lambda app: app == 'known'), \
             patch.object(o, '_restart', return_value=None) as restart, \
             patch.object(service, 'save_pm2') as save:
            note = o._restart_workers(str(self.repo), {}, ['known', 'missing'])
            self.assertIn('서비스 연결로 먼저 등록해 주세요', note)
            self.assertEqual(restart.call_count, 1)
            self.assertEqual(restart.call_args.args[1]['pm2_app'], 'known')
            save.assert_not_called()

    def test_pinned_identity_ignores_project_home(self):
        env = service.pin_pm2_environment({'PM2_HOME': 'wrong', 'HOME': 'wrong', 'APP_SECRET': 'private'})
        self.assertEqual(env['PM2_HOME'], os.environ['PM2_HOME'])
        self.assertEqual(env['HOME'], os.environ['HOME'])

    def test_post_deploy_saves_after_verified_health_and_release(self):
        import orchestrate as o
        from types import SimpleNamespace
        for good, phase in ((True, 'restart_requested'), (False, 'rollback_applied')):
            with self.subTest(good=good), ExitStack() as stack:
                events = []
                stack.enter_context(patch.object(o, '_git', return_value=SimpleNamespace(stdout='server/app.py')))
                stack.enter_context(patch.object(o.restart_guard, 'worker_apps', return_value=()))
                stack.enter_context(patch.object(o, '_verified_health', side_effect=lambda *a: events.append('health') or good))
                stack.enter_context(patch.object(o, 'release_health', side_effect=lambda *a: (events.append('release') or True, 'release warning')))
                stack.enter_context(patch.object(o, '_finish', side_effect=lambda actor, record, status, note: (status, note)))
                save = stack.enter_context(patch.object(service, 'save_pm2', side_effect=lambda: events.append('save') or service.PM2_SAVE_WARNING))
                record = dict(merge_sha='sha', phase=phase, build_dir='build', note='')
                state, note = o._post_deploy({}, str(self.repo), {**o.DEFAULTS, 'pm2_app': 'dev'}, record)
                self.assertEqual(save.call_count, int(good))
                if good:
                    self.assertEqual(events, ['health', 'release', 'save'])
                    self.assertEqual(state, 'merged')
                    self.assertIn('release warning', note)
                    self.assertIn(service.PM2_SAVE_WARNING, note)

    def test_explicit_rollback_saves_only_after_verification(self):
        import rollback
        from types import SimpleNamespace
        for good in (True, False):
            with self.subTest(good=good), ExitStack() as stack:
                record = dict(phase='restarting', repo=str(self.repo), head='sha', restart=True,
                              project_key='SAVE', actor={}, ref='SAVE-1', id='r', note='', build_dir='build')
                stack.enter_context(patch.object(rollback, '_records', return_value=[record]))
                stack.enter_context(patch.object(rollback, '_recovery_rows', return_value=[]))
                stack.enter_context(patch.object(rollback, '_save'))
                stack.enter_context(patch.object(rollback.orchestrate, '_git', return_value=SimpleNamespace(returncode=0)))
                stack.enter_context(patch.object(rollback.orchestrate, '_busy', return_value=False))
                stack.enter_context(patch.object(rollback.orchestrate, '_healthy', return_value=True))
                stack.enter_context(patch.object(rollback.orchestrate, '_restart', return_value=None))
                stack.enter_context(patch.object(rollback.orchestrate, 'settings', return_value={}))
                stack.enter_context(patch.object(rollback.orchestrate, 'release_health', return_value=(good, '')))
                stack.enter_context(patch.object(rollback.project_build, 'confirm'))
                stack.enter_context(patch.object(rollback.project_build, 'restore'))
                comments = stack.enter_context(patch.object(rollback.issues, 'add_comment'))
                save = stack.enter_context(patch.object(service, 'save_pm2', return_value=service.PM2_SAVE_WARNING))
                rollback.recover()
                self.assertEqual(save.call_count, int(good))
                self.assertEqual(record['phase'], 'complete' if good else 'restart_failed')
                if good:
                    comments.assert_called_with({}, 'SAVE-1', service.PM2_SAVE_WARNING)


if __name__ == '__main__':
    try:
        unittest.main()
    finally:
        TEMP.cleanup()
