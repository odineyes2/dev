"""서비스 연결의 권한·저장·충돌·터널 보존·실패 복구를 실제 임시 DB와 파일로 검사한다."""
import os
import errno
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
        self.reachable = patch.object(service, '_reachable', return_value=False).start()

    def queue(self, action='connect'):
        with patch.object(service.threading, 'Thread'):
            service.request(H, 'APP', action)

    def connect(self):
        self.queue()
        service._run(self.project, 'connect')
        self.assertEqual(service.status('APP')['status'], 'connected')

    def test_validation_and_storage(self):
        self.assertEqual(self.project['service_prepare_provider'], 'auto')
        self.assertEqual(issues.update_project(H, 'APP', {'service_prepare_provider': 'codex'})['service_prepare_provider'], 'codex')
        with self.assertRaises(issues.StoreError):
            issues.update_project(H, 'APP', {'service_prepare_provider': 'unknown'})
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

    def test_free_port_reservations_and_socket_cleanup(self):
        issues.create_project(H, 'OLD', '보관', service_port=8401)
        issues.update_project(H, 'OLD', {'archived': True})
        with db.connect() as c:
            c.execute("INSERT INTO project_services(project_id, port, status, actor, action, updated_at) VALUES (?, 8402, 'idle', 'test', 'connect', '')", (self.project['id'],))
            before = [tuple(r) for r in c.execute('SELECT * FROM project_services')]
        sockets = []
        def factory(*args):
            candidate = MagicMock()
            candidate.__enter__.return_value = candidate
            def bind(address):
                if address[1] == 8405: raise OSError(errno.EADDRINUSE, 'occupied')
                if address[1] == 8406: raise OSError(errno.EACCES, 'denied')
            candidate.bind.side_effect = bind
            sockets.append(candidate)
            return candidate
        with patch.object(config, 'PORT', 8403), patch.object(config, 'NIGHTSHIFT_URL', 'http://localhost:8404'), patch.object(service.socket, 'socket', side_effect=factory):
            self.assertEqual(service.find_free_port(), 8407)
        self.assertEqual([s.bind.call_args.args[0] for s in sockets], [('127.0.0.1', p) for p in (8405, 8406, 8407)])
        for candidate in sockets:
            candidate.__exit__.assert_called_once()
            for call in candidate.setsockopt.call_args_list:
                self.assertNotEqual(call.args[1], socket.SO_REUSEADDR)
            if os.name == 'nt':
                candidate.setsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        with db.connect() as c:
            self.assertEqual(before, [tuple(r) for r in c.execute('SELECT * FROM project_services')])
        self.assertEqual(self.path.read_bytes(), RAW)
        self.commands.assert_not_called(); self.restart.assert_not_called()

    def test_free_port_exhaustion_and_unexpected_error(self):
        candidate = MagicMock(); candidate.__enter__.return_value = candidate
        candidate.bind.side_effect = OSError(errno.EADDRINUSE, 'occupied')
        with patch.object(service.socket, 'socket', return_value=candidate):
            with self.assertRaises(issues.StoreError) as raised: service.find_free_port()
        self.assertEqual(raised.exception.status, 503)
        ports = [call.args[0][1] for call in candidate.bind.call_args_list]
        self.assertEqual(ports, list(range(8401, 9000)))
        self.assertEqual(candidate.__exit__.call_count, 599)
        candidate.reset_mock(); candidate.bind.side_effect = OSError(errno.EMFILE, 'unexpected')
        with patch.object(service.socket, 'socket', return_value=candidate):
            with self.assertRaises(issues.StoreError) as raised: service.find_free_port()
        self.assertEqual(raised.exception.status, 503)
        candidate.__exit__.assert_called_once()
        with patch.object(service.socket, 'socket', side_effect=OSError(errno.EMFILE, 'creation')):
            with self.assertRaises(issues.StoreError): service.find_free_port()

    def test_free_port_real_loopback_and_release(self):
        # 실제 빈 후보를 찾고 점유한 동안 다음 후보를 추천하는지 확인한다.
        port = service.find_free_port()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            if os.name == 'nt': occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            occupied.bind(('127.0.0.1', port))
            self.assertGreater(service.find_free_port(), port)
        self.assertEqual(service.find_free_port(), port)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as released:
            released.bind(('127.0.0.1', port))

    def test_free_port_http_auth_routing_and_errors(self):
        client = TestClient(app.app)
        with patch.object(service, 'find_free_port', return_value=8401) as find:
            for user, status in ((None, 401), ({'role': 'member'}, 403)):
                with patch.object(auth, 'nightshift_user', AsyncMock(return_value=user)):
                    self.assertEqual(client.get('/api/projects/free-port').status_code, status)
            _, token = auth.create_agent('port-test')
            self.assertEqual(client.get('/api/projects/free-port', headers={'Authorization': 'Bearer ' + token}).status_code, 403)
            find.assert_not_called()
            with patch.object(auth, 'nightshift_user', AsyncMock(return_value={'role': 'admin'})):
                response = client.get('/api/projects/free-port')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {'port': 8401})
                self.assertEqual(response.headers['cache-control'], 'no-store')
                for message in ('후보 고갈', '탐색 오류'):
                    find.side_effect = issues.StoreError(message, 503)
                    response = client.get('/api/projects/free-port')
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.json(), {'detail': message})
                    self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_existing_service_is_reported_without_taking_ownership(self):
        self.reachable.return_value = True
        state = service.status('APP')
        self.assertEqual(state['status'], 'external')
        self.assertTrue(state['online'])
        self.assertFalse(state['managed'])
        self.assertEqual(state['port'], 8400)
        with db.connect() as c:
            self.assertFalse(c.execute('SELECT 1 FROM project_services').fetchone())
        with self.assertRaises(issues.StoreError):
            service.request(H, 'APP', 'disconnect')

    def test_builtin_ports_are_detected_when_saved_port_is_empty(self):
        issues.update_project(H, 'APP', {'local_path': str(config.REPO_ROOT), 'service_domain': '', 'service_port': 0})
        self.reachable.return_value = True
        state = service.status('APP')
        self.assertEqual(state['observed_port'], config.PORT)
        self.assertEqual(state['url'], config.PUBLIC_URL)
        issues.update_project(H, 'APP', {'local_path': str(config.REPO_ROOT.parent / 'nightshift')})
        self.assertEqual(service.status('APP')['observed_port'], 8000)

    def test_configured_domain_with_failed_health_does_not_claim_service_is_down(self):
        state = service.status('APP')
        self.assertFalse(state['online'])
        self.assertEqual(state['status'], 'idle')
        self.assertEqual(state['url'], 'https://app.example.com')

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
        self.assertEqual([s['status'] for s in state['steps']], ['done'] * 5 + ['failed'])

    def test_missing_pm2_config_marks_the_exact_failed_step(self):
        self.project = issues.update_project(H, 'APP', {'service_app': 'myapp'})
        self.queue()
        with patch('service_prepare.prepare', side_effect=issues.StoreError('실행 파일 자동 생성이 중단됐어요: 실행 진입점이 미정이에요.')) as prepare:
            service._run(self.project, 'connect')
            prepare.assert_called_once_with(self.project)
        steps = service.status('APP')['steps']
        self.assertEqual([s['status'] for s in steps], ['done', 'done', 'done', 'failed', 'pending', 'pending'])
        self.assertIn('자동 생성이 중단', steps[3]['error'])
        self.health.assert_not_called()
        self.assertFalse(any('route' in c.args[0] for c in self.commands.call_args_list))

    def test_old_cli_error_is_enriched_in_diagnostic_without_changing_the_record(self):
        self.queue()
        original = '실행 파일 생성에 실패했어요. 에이전트 CLI 로그인과 설정을 확인해 주세요.'
        service._save(self.project['id'], status='failed', step=4, error=original)
        with patch('service_prepare.previous_failure', return_value='생성 비용 상한에 도달했어요.') as lookup:
            state = service.status('APP')
        self.assertIn('비용 상한', state['error'])
        self.assertEqual(state['steps'][3]['error'], state['error'])
        lookup.assert_called_once()
        with db.connect() as c:
            self.assertEqual(c.execute('SELECT error FROM project_services WHERE project_id=?', (self.project['id'],)).fetchone()[0], original)

    def test_missing_config_is_generated_before_pm2_and_dns(self):
        self.project = issues.update_project(H, 'APP', {'service_app': 'myapp'})
        def generated(project):
            self.assertEqual(service.status('APP')['step'], 4)
            self.assertIn('생성', service.status('APP')['phase'])
            (self.root / 'ecosystem.config.js').write_text('module.exports = {};')
            self.assertFalse(any('route' in c.args[0] for c in self.commands.call_args_list))
            return ['ecosystem.config.js']
        with patch('service_prepare.prepare', side_effect=generated) as prepare:
            self.connect()
            prepare.assert_called_once_with(self.project)
        self.assertTrue(any(c.args[1] == 'PM2 앱 시작' for c in self.commands.call_args_list))

    def test_code_only_mode_never_calls_agent_when_files_are_missing(self):
        self.project = issues.update_project(H, 'APP', {'service_app': 'myapp', 'service_prepare_provider': 'none'})
        self.queue()
        with patch('service_prepare.prepare') as prepare:
            service._run(self.project, 'connect')
            prepare.assert_not_called()
        state = service.status('APP')
        self.assertEqual(state['step'], 4)
        self.assertIn('기존 파일 사용', state['error'])

    def test_running_and_successful_steps(self):
        self.queue()
        state = service.status('APP')
        self.assertEqual([s['status'] for s in state['steps']], ['done', 'running', 'pending', 'pending', 'pending', 'pending'])
        service._run(self.project, 'connect')
        self.assertEqual([s['status'] for s in service.status('APP')['steps']], ['done'] * 6)
        self.queue('disconnect')
        self.assertEqual(service.status('APP')['steps'], [])

    def test_legacy_missing_config_failure_keeps_its_step_after_migration(self):
        import sqlite3
        legacy = self.root / 'legacy.db'
        with sqlite3.connect(legacy) as c:
            step_version = next(i for i, sql in enumerate(db.MIGRATIONS) if 'ALTER TABLE project_services ADD COLUMN step' in sql)
            for version, sql in enumerate(db.MIGRATIONS[:step_version], 1):
                c.executescript(sql + f'PRAGMA user_version={version};')
            c.execute("INSERT INTO projects(key,name,service_domain,service_port,created_at) VALUES('OLD','기존','app.example.com',8400,?)", (db.now_iso(),))
            c.execute("INSERT INTO project_services(project_id,status,error,actor,action,updated_at) VALUES(1,'failed',?,'human:admin','connect',?)",
                      ('Local path의 ecosystem.config.js에서 지정 PM2 앱을 준비해 주세요.', db.now_iso()))
        c.close()
        with patch.object(config, 'DB_PATH', legacy):
            db.init()
            self.assertEqual([s['status'] for s in service.status('OLD')['steps']], ['done', 'done', 'done', 'failed', 'pending', 'pending'])

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
        self.project = issues.update_project(H, 'APP', {'service_app': 'myapp', 'service_prepare_provider': 'none'})
        (self.root / 'ecosystem.config.js').write_text('module.exports = {};')
        cli = self.root / 'node_modules/npm/bin/npx-cli.js'
        cli.parent.mkdir(parents=True)
        cli.write_text('')
        paths = {'pm2': None, 'npx': str(self.root / 'npx.cmd'), 'node': str(self.root / 'node.exe')}
        with patch.object(service.shutil, 'which', side_effect=paths.get), patch('service_prepare.prepare') as prepare:
            self.connect()
            prepare.assert_not_called()
        pm2 = next(c for c in self.commands.call_args_list if c.args[1] == 'PM2 앱 시작')
        self.assertEqual(pm2.args[0][:4], [paths['node'], str(cli), '--no-install', 'pm2'])
        self.assertEqual(pm2.args[2], str(self.root))
        self.assertEqual(pm2.kwargs['environment']['PORT'], '8400')
        self.assertEqual(pm2.kwargs['environment']['HOST'], '127.0.0.1')

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
