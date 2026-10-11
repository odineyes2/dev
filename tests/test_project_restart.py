"""재시작은 모의 PM2·health와 임시 DB에서 확인한다. 운영 앱은 실행하지 않는다."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import config
import db
import issues
import jobs
import project_restart as restart
import project_service
import orchestrate

H = {'kind': 'human', 'name': 'admin'}


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        self.root = Path(self.temp.name)
        patch.object(config, 'DB_PATH', self.root / 'test.db').start()
        patch.object(config, 'DATA_DIR', self.root / 'data').start()
        db.init()
        self.repo = self.root / 'moai'
        self.repo.mkdir()
        (self.repo / 'ecosystem.config.js').write_text('module.exports={};')
        self.project = issues.create_project(H, 'MO', 'Moai', local_path=str(self.repo), service_app='moai', service_port=8400)
        self.real_launch = restart.launch
        self.launch = patch.object(restart, 'launch').start()
        self.command = patch.object(project_service, '_command').start()
        patch.object(project_service, 'pm2_launcher', return_value=['node', 'pm2-cli']).start()
        patch.object(restart.time, 'sleep').start()

    def health(self, rows):
        response = MagicMock(status_code=200)
        response.json.side_effect = rows
        client = MagicMock()
        client.__enter__.return_value.get.return_value = response
        return patch.object(restart.httpx, 'Client', return_value=client)

    def own(self):
        patch.object(config, 'REPO_ROOT', self.repo).start()

    def test_restart_uses_saved_target_only_and_is_idempotent(self):
        first = restart.request(H, 'MO')
        self.assertEqual(restart.request(H, 'MO')['id'], first['id'])
        self.launch.assert_called_once_with(first['id'])
        with self.health([]):
            restart.run(first['id'])
        state = restart.status('MO')
        self.assertEqual(state['status'], 'complete')
        args, label, cwd = self.command.call_args.args
        self.assertEqual(args, ['node', 'pm2-cli', 'restart', 'ecosystem.config.js', '--only', 'moai', '--update-env'])
        self.assertTrue(Path(cwd).samefile(self.repo))
        self.assertEqual(self.command.call_args.kwargs['environment']['PORT'], '8400')
        self.assertNotIn('repo', state)
        self.assertNotIn('process_id', state)

    def test_dev_waits_for_work_and_checks_new_process_identity(self):
        self.own()
        row = restart.request(H, 'MO')
        with db.connect() as c:
            self.assertIsNotNone(restart.block(c, 'OTHER'))
        with patch.object(restart, '_busy', side_effect=[True, False]), self.health([
                {'process_id': orchestrate.PROCESS_ID}, {'process_id': 'new-process'}]):
            restart.run(row['id'])
        self.assertEqual(restart.status('MO')['status'], 'complete')
        self.assertEqual(self.command.call_args.args[0][-2:], ['dev', '--update-env'])

    def test_pending_target_is_frozen_and_new_work_is_blocked(self):
        restart.request(H, 'MO')
        with db.connect() as c:
            self.assertIsNotNone(jobs.start_block(c, 'MO', 'review'))
            self.assertIsNone(restart.block(c, 'OTHER'))
        with self.assertRaises(issues.StoreError):
            issues.update_project(H, 'MO', {'service_port': 8401})
        self.assertFalse(issues.check_project_deletion(H, 'MO')['can_delete'])

    def test_failure_and_dead_helper_have_safe_diagnostics(self):
        row = restart.request(H, 'MO')
        self.command.side_effect = issues.StoreError('api_key=supersecretvalue', 409)
        restart.run(row['id'])
        self.assertEqual(restart.status('MO')['status'], 'failed')
        self.assertNotIn('supersecretvalue', restart.status('MO')['error'])
        row = restart.request(H, 'MO')
        with db.connect() as c:
            c.execute("UPDATE project_app_restarts SET updated_at='2000-01-01T00:00:00+00:00' WHERE id=?", (row['id'],))
        self.assertEqual(restart.status('MO')['status'], 'failed')

    def test_self_health_timeout_rejects_old_process_and_wait_timeout_never_restarts(self):
        self.own()
        row = restart.request(H, 'MO')
        with self.health([{'process_id': orchestrate.PROCESS_ID}]), patch.object(restart, 'HEALTH_SECONDS', 1), patch.object(
                restart.time, 'monotonic', side_effect=[0, 0, .5, 2]):
            restart.run(row['id'])
        self.assertEqual(restart.status('MO')['status'], 'failed')
        self.command.reset_mock()
        row = restart.request(H, 'MO')
        with patch.object(restart, '_busy', return_value=True), patch.object(restart, 'WAIT_SECONDS', 0):
            restart.run(row['id'])
        self.command.assert_not_called()
        self.assertEqual(restart.status('MO')['status'], 'failed')

    def test_target_validation_and_nightshift_saved_health_fallback(self):
        with self.assertRaises(issues.StoreError):
            restart.request({'kind': 'agent'}, 'MO')
        issues.update_project(H, 'MO', {'service_app': 'dev'})
        with self.assertRaises(issues.StoreError):
            restart.request(H, 'MO')
        issues.update_project(H, 'MO', {'service_app': 'nightshift', 'service_port': 0})
        with patch.object(orchestrate, 'settings', return_value={'health_url': 'http://127.0.0.1:8000/api/health'}):
            self.assertTrue(restart.status('MO')['available'])
        with patch.object(orchestrate, 'settings', return_value={'health_url': 'https://external.example.com/api/health'}):
            self.assertFalse(restart.status('MO')['available'])

    def test_detached_helper_inherits_paths_without_app_secrets(self):
        with patch.dict(os.environ, {'DEV_CODEX_AGENT_KEY': 'private-key', 'PM2_HOME': 'test-pm2-home'}), patch.object(
                restart.subprocess, 'Popen') as popen:
            self.real_launch(123)
        args, options = popen.call_args.args[0], popen.call_args.kwargs
        self.assertEqual(args[0], sys.executable)
        self.assertEqual(args[-1], '123')
        self.assertEqual(options['env']['DEV_DB_PATH'], str(config.DB_PATH.resolve()))
        self.assertEqual(options['env']['PM2_HOME'], 'test-pm2-home')
        self.assertNotIn('DEV_CODEX_AGENT_KEY', options['env'])
        self.assertEqual(options['stdin'], restart.subprocess.DEVNULL)
        self.assertTrue(options['creationflags'] if os.name == 'nt' else options['start_new_session'])

    def test_admin_auth_and_csrf_routes(self):
        import app
        import auth
        from fastapi.testclient import TestClient
        user = AsyncMock(return_value={'id': 1, 'username': 'admin', 'role': 'admin'})
        with patch.object(auth, 'nightshift_user', user), patch.object(jobs, 'start_timer'), TestClient(app.app) as client:
            self.assertEqual(client.post('/api/projects/MO/restart').status_code, 403)
            self.assertEqual(client.post('/api/projects/MO/restart', headers={'X-Requested-With': 'dev'}).status_code, 202)
            self.assertEqual(client.get('/api/projects/MO/restart').status_code, 200)
            with patch.object(auth, 'agent_for_key', return_value={'id': 1, 'name': 'agent', 'model': ''}):
                self.assertEqual(client.post('/api/projects/MO/restart', headers={'Authorization': 'Bearer fake'}).status_code, 403)
            user.return_value = {'id': 2, 'username': 'member', 'role': 'user'}
            self.assertEqual(client.get('/api/projects/MO/restart').status_code, 403)


if __name__ == '__main__':
    unittest.main()
