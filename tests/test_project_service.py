"""서비스 연결의 권한·저장·충돌·터널 보존·실패 복구를 실제 임시 DB와 파일로 검사한다."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))
import app
import auth
import config
import db
import issues
import project_service as service
from fastapi.testclient import TestClient
import yaml

H = {'kind': 'human', 'name': 'admin'}
RAW = b'tunnel: test-tunnel\ningress:\n  - hostname: existing.example.com\n    service: http://localhost:8000\n    originRequest:\n      connectTimeout: 10s\n  - service: http_status:404\n'


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        self.root = Path(self.temp.name)
        patch.object(config, 'DATA_DIR', self.root).start()
        patch.object(config, 'DB_PATH', self.root / 'test.db').start()
        db.init()
        self.project = issues.create_project(H, 'APP', '테스트', local_path=str(self.root), service_domain='app.example.com', service_port=8400)
        self.path = self.root / 'config.yml'
        self.path.write_bytes(RAW)
        patch.object(service, 'config_path', return_value=self.path).start()
        patch.object(service, 'binary', return_value='cloudflared').start()
        patch.object(service, '_restart_permission').start()
        self.commands = patch.object(service, '_command').start()
        self.restart = patch.object(service, '_restart').start()
        self.health = patch.object(service, '_healthy').start()

    def queue(self, action='connect'):
        with patch.object(service.threading, 'Thread'):
            service.request(H, 'APP', action)

    def connect(self):
        self.queue()
        service._run(self.project, 'connect')
        self.assertEqual(service.status('APP')['status'], 'connected')

    def test_validation_and_storage(self):
        for domain in ('https://app.example.com', 'app.example.com/path', '*.example.com', '-bad.example.com', '127.0.0.1', 'a..com'):
            with self.assertRaises(issues.StoreError):
                service.validate_fields(domain, 8400)
        for port in (True, -1, 65536, '1.2', '1;cmd'):
            with self.assertRaises(issues.StoreError):
                service.validate_fields('app.example.com', port)
        with self.assertRaises(issues.StoreError):
            service.validate_fields('', 0, '--only x')
        self.assertEqual(service.validate_fields('APP.Example.com.', '8400')['service_domain'], 'app.example.com')
        updated = issues.update_project(H, 'APP', {'service_port': 8500})
        self.assertEqual(updated['service_port'], 8500)
        with self.assertRaises(issues.StoreError):
            issues.create_project(H, 'DUP', '중복', service_domain='app.example.com')
        issues.create_project(H, 'OTHER', '다른 프로젝트')
        with self.assertRaises(issues.StoreError):
            issues.update_project(H, 'OTHER', {'service_domain': 'app.example.com'})
        self.assertEqual(next(p for p in issues.list_projects() if p['key'] == 'OTHER')['service_domain'], '')

    def test_connect_preserves_existing_rules_and_disconnect(self):
        self.connect()
        data = yaml.safe_load(self.path.read_bytes())
        self.assertEqual(data['ingress'][0], {'hostname': 'app.example.com', 'service': 'http://127.0.0.1:8400'})
        self.assertEqual(data['ingress'][1:], yaml.safe_load(RAW)['ingress'])
        self.assertEqual(self.health.call_args_list[0].args[0], 'http://127.0.0.1:8400/api/health')
        self.assertEqual(self.health.call_args_list[1].args[0], 'https://app.example.com/api/health')
        self.assertTrue(any('route' in call.args[0] for call in self.commands.call_args_list))
        self.queue('disconnect')
        service._run(self.project, 'disconnect')
        self.assertEqual(service.status('APP')['status'], 'disconnected')
        self.assertEqual(yaml.safe_load(self.path.read_bytes()), yaml.safe_load(RAW))

    def test_external_failure_restores_original(self):
        self.health.side_effect = [None, issues.StoreError('외부 접속 실패')]
        self.queue()
        service._run(self.project, 'connect')
        state = service.status('APP')
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(self.path.read_bytes(), RAW)
        self.assertIn('복구', state['error'])
        self.assertIn('DNS', state['error'])
        self.assertEqual(self.restart.call_count, 2)
        self.assertEqual(state['domain'], '')

    def test_conflict_and_unhealthy_app_do_not_publish(self):
        self.project = issues.update_project(H, 'APP', {'service_domain': 'existing.example.com'})
        self.queue()
        service._run(self.project, 'connect')
        self.commands.assert_not_called()
        self.assertEqual(self.path.read_bytes(), RAW)
        self.project = issues.update_project(H, 'APP', {'service_domain': 'app.example.com'})
        self.health.side_effect = issues.StoreError('로컬 접속 실패')
        self.queue()
        service._run(self.project, 'connect')
        self.assertFalse(any('route' in call.args[0] for call in self.commands.call_args_list))
        self.restart.assert_not_called()

    def test_concurrent_changes_and_delete_are_blocked(self):
        self.queue()
        import jobs
        with db.connect() as c:
            self.assertIn('터널', jobs.start_block(c, 'APP', 'review'))
        with self.assertRaises(issues.StoreError):
            service.request(H, 'APP')
        with self.assertRaises(issues.StoreError):
            issues.update_project(H, 'APP', {'service_port': 8500})
        self.assertFalse(issues.check_project_deletion(H, 'APP')['can_delete'])
        service._run(self.project, 'connect')
        self.assertFalse(issues.check_project_deletion(H, 'APP')['can_delete'])
        with self.assertRaises(issues.StoreError):
            issues.update_project(H, 'APP', {'archived': True})

    def test_agent_activity_blocks_shared_tunnel_changes(self):
        import jobs
        with patch.object(jobs, '_active_lanes', return_value={('OTHER', 'execute')}):
            with self.assertRaises(issues.StoreError):
                service.request(H, 'APP')
        self.assertEqual(service.status('APP')['status'], 'idle')

    def test_change_removes_owned_route_and_reports_pending_settings(self):
        self.connect()
        self.project = issues.update_project(H, 'APP', {'service_domain': 'new.example.com', 'service_port': 8500})
        self.assertTrue(service.status('APP')['changed'])
        self.connect()
        hosts = [r.get('hostname') for r in yaml.safe_load(self.path.read_bytes())['ingress']]
        self.assertIn('new.example.com', hosts)
        self.assertNotIn('app.example.com', hosts)
        self.assertFalse(service.status('APP')['changed'])

    def test_external_edit_is_not_overwritten_on_failure(self):
        modified = RAW + b'# external edit\n'
        def health(url):
            if url.startswith('https:'):
                self.path.write_bytes(modified)
                raise issues.StoreError('실패')
        self.health.side_effect = health
        self.queue()
        service._run(self.project, 'connect')
        self.assertEqual(self.path.read_bytes(), modified)
        self.assertIn('복구를 완료하지 못', service.status('APP')['error'])
        self.assertFalse(issues.check_project_deletion(H, 'APP')['can_delete'])

    def test_invalid_catchall_and_changed_owned_rule(self):
        with self.assertRaises(issues.StoreError):
            service._candidate(b'tunnel: test\ningress: []', {'domain': '', 'port': 0}, 'app.example.com', 8400)
        self.connect()
        self.path.write_bytes(self.path.read_bytes().replace(b'8400', b'8401'))
        self.queue('disconnect')
        service._run(self.project, 'disconnect')
        self.assertEqual(service.status('APP')['status'], 'failed')
        self.assertIn('바뀌었어요', service.status('APP')['error'])

    def test_http_human_only_and_csrf(self):
        agent, token = auth.create_agent('test')
        client = TestClient(app.app)
        with patch.object(auth, 'nightshift_user', AsyncMock(return_value={'role': 'admin', 'username': 'admin'})), patch.object(service, '_run'):
            self.assertEqual(client.post('/api/projects/APP/service', headers={'Authorization': 'Bearer ' + token}, json={}).status_code, 403)
            self.assertEqual(client.post('/api/projects/APP/service', json={}).status_code, 403)
            self.assertEqual(client.post('/api/projects/APP/service', headers={'X-Requested-With': 'dev'}, json={}).status_code, 202)
            self.assertEqual(client.get('/api/projects/APP/service').json()['status'], 'running')

    def test_restart_permission_denial_precedes_dns_and_pm2(self):
        with patch.object(service, '_restart_permission', side_effect=issues.StoreError('권한 없음')):
            self.queue()
            service._run(self.project, 'connect')
        self.commands.assert_not_called()
        self.health.assert_not_called()
        self.assertEqual(self.path.read_bytes(), RAW)
        self.assertIn('권한 없음', service.status('APP')['error'])

    @unittest.skipUnless(os.name == 'nt', 'Windows npx 실행 경로')
    def test_pm2_uses_installed_npx_without_shell_or_package_install(self):
        self.project = issues.update_project(H, 'APP', {'service_app': 'myapp'})
        (self.root / 'ecosystem.config.js').write_text('module.exports = {};')
        cli = self.root / 'node_modules/npm/bin/npx-cli.js'
        cli.parent.mkdir(parents=True)
        cli.write_text('')
        paths = {'pm2': None, 'npx': str(self.root / 'npx.cmd'), 'node': str(self.root / 'node.exe')}
        with patch.object(service.shutil, 'which', side_effect=paths.get):
            self.connect()
        pm2 = next(c for c in self.commands.call_args_list if c.args[1] == 'PM2 앱 시작')
        self.assertEqual(pm2.args[0][:4], [paths['node'], str(cli), '--no-install', 'pm2'])
        self.assertEqual(pm2.args[2], str(self.root))

    def test_startup_recovers_interrupted_config_change(self):
        self.queue()
        candidate, _ = service._candidate(RAW, {'domain': '', 'port': 0}, 'app.example.com', 8400)
        backup = self.root / 'backup.yml'
        backup.write_bytes(RAW)
        self.path.write_bytes(candidate)
        service._save(self.project['id'], backup_path=str(backup), config_path=str(self.path), applied_hash=service._hash(candidate))
        with patch.object(service.threading, 'Thread') as worker:
            service.recover()
            worker.call_args.kwargs['target']()
        self.assertEqual(self.path.read_bytes(), RAW)
        self.assertEqual(service.status('APP')['status'], 'failed')
        self.assertIn('복구', service.status('APP')['error'])


if __name__ == '__main__':
    unittest.main()
