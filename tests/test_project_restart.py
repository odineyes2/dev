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
import project_build
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
        # 바로 도우미가 아니라 중계(--detach)를 띄우고, 중계는 같은 환경으로 도우미를 띄운다(DEV-207).
        self.assertEqual(args[-2:], ['--detach', '123'])
        with patch.dict(os.environ, {'DEV_DB_PATH': 'from-relay'}), patch.object(restart.subprocess, 'Popen') as popen:
            restart.detach(123)
        args, options = popen.call_args.args[0], popen.call_args.kwargs
        self.assertEqual(args, [sys.executable, str(Path(restart.__file__).resolve()), '123'])
        self.assertEqual(options['env']['DEV_DB_PATH'], 'from-relay')
        self.assertTrue((config.DATA_DIR / 'app-restarts' / '123.log').exists())

    @unittest.skipUnless(os.name == 'nt', 'pm2(Windows)는 taskkill /T로 프로세스 트리째 끈다')
    def test_relay_helper_survives_tree_kill_of_parent(self):
        """dev가 트리째 꺼져도 중계로 띄운 도우미는 살아남고, 바로 띄운 자식은 같이 죽는다(DEV-207 원인 재현)."""
        import subprocess
        import threading
        import time
        pause = threading.Event().wait   # setUp이 time.sleep을 가로채므로 진짜로 기다릴 때는 이것을 쓴다
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        def alive(pid):
            return str(pid) in subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/NH'], capture_output=True, text=True).stdout
        pids = {}
        for mode in ('direct', 'relay'):
            out = self.root / f'{mode}.pid'
            sleeper = f"import os,time;open(r'{out}','w').write(str(os.getpid()));time.sleep(60)"
            # 실제 도우미처럼 표준 입출력을 명시한다 — 콘솔 없는 검사 실행기 아래에서 물려받을 핸들이 없으면 분리된 자식이 뜨지 못한다.
            quiet = 'stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL'
            spawn = f"import subprocess,sys;subprocess.Popen([sys.executable,'-c',{sleeper!r}],creationflags={flags},{quiet})"
            child = spawn if mode == 'direct' else f"import subprocess,sys;subprocess.Popen([sys.executable,'-c',{spawn!r}],creationflags={flags},{quiet})"
            parent = subprocess.Popen([sys.executable, '-c', child + ';import time;time.sleep(60)'], creationflags=subprocess.CREATE_NO_WINDOW,
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 20
            while not out.exists() or not out.read_text():
                self.assertLess(time.monotonic(), deadline, mode)
                pause(0.2)
            pause(0.5)   # 중계가 끝날 시간
            pids[mode] = int(out.read_text())
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(parent.pid)], capture_output=True)
            parent.wait(10)
        pause(0.5)
        try:
            self.assertFalse(alive(pids['direct']), '바로 띄운 자식은 트리째 꺼진다')
            self.assertTrue(alive(pids['relay']), '중계로 띄운 도우미는 살아남는다')
        finally:
            for pid in pids.values():
                subprocess.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True)

    def build_setup(self, command=None):
        import json
        def git(*args):
            return project_build._git(self.repo, *args)
        git('init', '-q', '-b', 'main')
        git('config', 'user.name', 'test')
        git('config', 'user.email', 'test@local')
        (self.repo / '.gitignore').write_text('out/\nout.prev/\n')
        (self.repo / 'source.txt').write_text('new release')
        (self.repo / 'data').mkdir()
        (self.repo / 'data' / 'keep.db').write_bytes(b'preserve database')
        git('add', '.')
        git('commit', '-qm', 'fixture')
        self.sha = git('rev-parse', 'HEAD')
        (self.repo / 'out').mkdir()
        (self.repo / 'out' / 'index.html').write_text('previous release')
        code = "from pathlib import Path; p=Path('out'); p.mkdir(); (p/'index.html').write_text(Path('source.txt').read_text())"
        self.specs = [{'cwd': '.', 'run': command or [sys.executable, '-c', code], 'out': 'out', 'expect': ['index.html']}]
        self.settings = patch.object(orchestrate, 'settings', return_value={'build': self.specs}).start()

    def test_deploy_builds_head_and_reports_current_release(self):
        self.build_setup()
        row = restart.request(H, 'MO', mode='deploy')
        with self.health([{'revision': self.sha, 'web_revision': self.sha}]):
            restart.run(row['id'])
        result = restart.status('MO')
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['mode'], 'deploy')
        self.assertEqual(result['release_sha'], self.sha)
        self.assertEqual(result['current_release'][0]['revision'], self.sha)
        self.assertEqual(result['reported_web_revision'], self.sha)
        self.assertTrue(Path(result['log']).is_file())
        self.assertEqual((self.repo / 'out/index.html').read_text(), 'new release')
        self.assertFalse((self.repo / 'out.prev').exists())
        self.assertEqual((self.repo / 'data/keep.db').read_bytes(), b'preserve database')
        self.assertEqual(project_build._git(self.repo, 'rev-parse', 'HEAD'), self.sha)
        # 다음 배포에서도 이전 복원본이 성공 경로를 막지 않는다.
        again = restart.request(H, 'MO', mode='deploy')
        with self.health([{}]):
            restart.run(again['id'])
        self.assertEqual(restart.status('MO')['status'], 'complete')
        self.assertIn('로컬 표식', restart.status('MO')['error'])

    def test_deploy_build_failure_keeps_existing_output(self):
        self.build_setup([sys.executable, '-c', 'print("build failure"); raise SystemExit(1)'])
        row = restart.request(H, 'MO', mode='deploy')
        restart.run(row['id'])
        result = restart.status('MO')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['phase'], '빌드 단계: build')
        self.assertIn('build failure', result['error'])
        self.assertIn('build failure', result['log_tail'])
        self.assertTrue(Path(result['log']).is_file())
        self.assertEqual((self.repo / 'out/index.html').read_text(), 'previous release')
        self.command.assert_not_called()

    def test_deploy_restart_failure_restores_previous_output(self):
        self.build_setup()
        row = restart.request(H, 'MO', mode='deploy')
        self.command.side_effect = [issues.StoreError('PM2 failed', 409), None]
        restart.run(row['id'])
        result = restart.status('MO')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['phase'], '앱 재시작 실패')
        self.assertEqual((self.repo / 'out/index.html').read_text(), 'previous release')
        self.assertFalse((self.repo / 'out.prev').exists())
        self.assertEqual(self.command.call_count, 2)

    def test_deploy_release_mismatch_and_health_failure_restore(self):
        self.build_setup()
        for mismatch in (True, False):
            row = restart.request(H, 'MO', mode='deploy')
            with self.health([{'revision': 'wrong'}]), patch.object(restart, 'HEALTH_SECONDS', 60 if mismatch else 0):
                restart.run(row['id'])
            self.assertEqual(restart.status('MO')['status'], 'failed')
            self.assertEqual((self.repo / 'out/index.html').read_text(), 'previous release')
            self.assertFalse((self.repo / 'out.prev').exists())

    def test_deploy_head_change_before_swap_is_rejected(self):
        self.build_setup()
        row = restart.request(H, 'MO', mode='deploy')
        original = project_build.build
        def changed(*args):
            result = original(*args)
            (self.repo / 'source.txt').write_text('concurrent change')
            project_build._git(self.repo, 'add', '.')
            project_build._git(self.repo, 'commit', '-qm', 'concurrent')
            return result
        with patch.object(project_build, 'build', side_effect=changed):
            restart.run(row['id'])
        self.assertEqual(restart.status('MO')['status'], 'failed')
        self.assertEqual((self.repo / 'out/index.html').read_text(), 'previous release')
        self.command.assert_not_called()

    def test_deploy_rejects_missing_declaration_and_mixed_active_mode(self):
        with self.assertRaises(issues.StoreError):
            restart.request(H, 'MO', mode='deploy')
        self.build_setup()
        restart.request(H, 'MO')
        with self.assertRaises(issues.StoreError):
            restart.request(H, 'MO', mode='deploy')

    def test_deploy_http_route_authentication(self):
        import app
        import auth
        from fastapi.testclient import TestClient
        self.build_setup()
        user = AsyncMock(return_value={'id': 1, 'username': 'admin', 'role': 'admin'})
        with patch.object(auth, 'nightshift_user', user), patch.object(jobs, 'start_timer'), TestClient(app.app) as client:
            self.assertEqual(client.post('/api/projects/MO/deploy').status_code, 403)
            response = client.post('/api/projects/MO/deploy', headers={'X-Requested-With': 'dev'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['mode'], 'deploy')
            self.assertEqual(client.get('/api/projects/MO/restart').json()['mode'], 'deploy')
            with patch.object(auth, 'agent_for_key', return_value={'id': 1, 'name': 'agent', 'model': ''}):
                self.assertEqual(client.post('/api/projects/MO/deploy', headers={'Authorization': 'Bearer fake'}).status_code, 403)

    def agent(self, name='claude'):
        import auth
        agent, _ = auth.create_agent(name, 'anthropic', 'claude-opus-5-5')
        return {'kind': 'agent', 'id': agent['id'], 'name': name, 'model': agent['model']}

    def timeline(self, ref):
        return [e['body'] for e in issues.get_issue(ref)['events'] if e['kind'] == 'comment']

    def test_agent_reservation_ownership_blocks_until_finish_and_health(self):
        # DEV-198: 담당 에이전트만 예약하고, 완료 요청 전에는 도우미가 재시작하지 않으며 새 착수를 막는다.
        self.own()
        a, b = self.agent('a'), self.agent('b')
        ref = issues.create_issue(H, 'MO', '서버 수정')['ref']
        with self.assertRaises(issues.StoreError):
            restart.reserve(H, ref)                                      # 사람은 예약 대신 기존 재시작을 쓴다
        with self.assertRaises(issues.StoreError):
            restart.reserve(a, ref)                                      # 잡지 않은 이슈
        issues.claim(a, ref, 60)
        for bad in (0, 121, '30'):
            with self.assertRaises(issues.StoreError):
                restart.reserve(a, ref, bad)
        with patch.object(restart, '_busy', return_value=True):
            first = restart.reserve(a, ref, 30)
        self.assertFalse(first['reservation']['ready'])                  # 도는 작업이 끝나야 ready
        rid = first['id']
        with db.connect() as c:
            self.assertIsNotNone(jobs.start_block(c, 'OTHER', 'execute'))  # dev 자신 예약은 모든 줄을 막는다
            self.assertEqual(jobs.self_restart_state(c)['phase'], 'reserved')
        with patch.object(restart, '_busy', return_value=False):
            again = restart.reserve(a, ref, 60)                          # 갱신은 같은 기록
        self.assertEqual(again['id'], rid)
        self.assertTrue(again['reservation']['ready'])
        self.assertGreater(again['reservation']['expires_at'], first['reservation']['expires_at'])
        other = issues.create_issue(H, 'MO', '다른 일')['ref']
        issues.claim(b, other, 60)
        with self.assertRaises(issues.StoreError) as raised:
            restart.reserve(b, other)
        self.assertIn('다른 재시작', str(raised.exception))               # 다른 예약이 진행 중
        for action in ('finish', 'release'):
            with self.assertRaises(issues.StoreError):
                restart.reservation_action(b, rid, action)               # 소유자만
        restart.run(rid)                                                 # 완료 요청 전에는 도우미가 아무것도 안 한다
        self.command.assert_not_called()
        self.launch.assert_not_called()
        restart.reservation_action(a, rid, 'finish')
        self.launch.assert_called_once_with(rid)
        with db.connect() as c:
            self.assertIsNotNone(jobs.start_block(c, 'OTHER', 'execute'))  # 재시작·확인 동안 차단 유지
        with self.assertRaises(issues.StoreError):
            restart.reservation_action(a, rid, 'release')                # 시작된 재시작은 해제 못 함
        restart.reservation_action(a, rid, 'finish')                     # 연결이 끊겨 다시 불러도 한 번만
        self.launch.assert_called_once_with(rid)
        with self.health([{'process_id': orchestrate.PROCESS_ID}, {'process_id': 'new-process'}]):
            restart.run(rid)
        self.assertEqual(restart.status('MO')['status'], 'complete')
        with db.connect() as c:
            self.assertIsNone(jobs.start_block(c, 'OTHER', 'execute'))
        notes = self.timeline(ref)
        self.assertTrue(any('예약했어요' in n for n in notes) and any('접속 확인을 마쳤어요' in n for n in notes), notes)

    def test_agent_reservation_release_and_expiry_unblock(self):
        a = self.agent()
        ref = issues.create_issue(H, 'MO', '수정')['ref']
        issues.claim(a, ref, 60)
        rid = restart.reserve(a, ref, 10)['id']
        with db.connect() as c:
            self.assertIsNotNone(jobs.start_block(c, 'MO', 'execute'))
            self.assertIsNone(restart.block(c, 'OTHER'))                 # 다른 앱 예약은 그 프로젝트만 막는다
        restart.reservation_action(a, rid, 'release')
        with db.connect() as c:
            self.assertIsNone(jobs.start_block(c, 'MO', 'execute'))
        with self.assertRaises(issues.StoreError):
            restart.reservation_action(a, rid, 'finish')                 # 해제한 예약은 완료 못 함
        rid = restart.reserve(a, ref, 10)['id']
        with db.connect() as c:
            c.execute("UPDATE project_app_restarts SET reservation_until='2000-01-01T00:00:00+00:00' WHERE id=?", (rid,))
            self.assertIsNone(jobs.start_block(c, 'MO', 'execute'))      # 만료되면 차단을 풀고
        self.assertEqual(restart.status('MO')['status'], 'failed')
        self.assertTrue(any('만료' in n for n in self.timeline(ref)))
        with self.assertRaises(issues.StoreError):
            restart.reservation_action(a, rid, 'finish')
        self.launch.assert_not_called()

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
