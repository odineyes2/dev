"""실행 파일 자동 생성의 신규 파일 반영·검사·변경 경합·CLI 비밀값 경계를 검사한다."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))
import config
import db
import execute
import issues
import service_prepare as prepare

H = {'kind': 'human', 'name': 'admin'}


def result():
    return {'status': 'ready', 'reason': '기존 모듈을 사용하는 실행 파일을 준비했어요.',
            'files': [
                {'path': 'ecosystem.config.js', 'content': "module.exports={apps:[{name:'myapp',script:'backend/bootstrap.py',cwd:__dirname}]};\n"},
                {'path': 'backend/bootstrap.py', 'content': 'from backend.main import service_port\nPORT = service_port()\n'},
                {'path': 'tests/test_bootstrap.py', 'content': "import sys\nfrom pathlib import Path\nsys.path.insert(0,str(Path(__file__).resolve().parent.parent))\nfrom backend.bootstrap import PORT\nassert PORT == 8400\n"}],
            'checks': ['tests/test_bootstrap.py']}


class PrepareTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        patch.object(config, 'DATA_DIR', self.root / 'data').start()
        patch.object(config, 'DB_PATH', self.root / 'data/dev.db').start()
        db.init()
        subprocess.run(['git', 'init', '-b', 'main', str(self.repo)], capture_output=True, check=True)
        (self.repo / 'backend').mkdir()
        (self.repo / 'backend/main.py').write_text('def service_port():\n    return 8400\n')
        (self.repo / 'README.md').write_text('# 기존 서비스\n', encoding='utf-8')
        (self.repo / '.gitignore').write_text('.env\n__pycache__/\n')
        assert execute._server_git(str(self.repo), 'add', '.').returncode == 0
        assert execute._server_git(str(self.repo), 'commit', '-m', 'initial').returncode == 0
        (self.repo / '.env').write_text('TEST_SECRET=do-not-forward\n')
        self.project = issues.create_project(H, 'APP', '생성 검사', local_path=str(self.repo),
                                            service_domain='app.example.com', service_port=8400, service_app='myapp')

    def test_generated_files_are_checked_committed_and_source_is_preserved(self):
        with patch.object(prepare, '_generate', return_value=result()) as generate:
            paths = prepare.prepare(self.project)
        self.assertEqual(set(paths), {f['path'] for f in result()['files']})
        self.assertEqual((self.repo / 'backend/main.py').read_text(), 'def service_port():\n    return 8400\n')
        self.assertEqual((self.repo / '.env').read_text(), 'TEST_SECRET=do-not-forward\n')
        self.assertEqual(prepare.audit._git(str(self.repo), 'status', '--porcelain'), b'')
        snapshot = generate.call_args.args[1]
        self.assertNotIn('do-not-forward', json.dumps(snapshot))
        self.assertEqual(prepare.audit._git(str(self.repo), 'rev-list', '--count', 'HEAD').strip(), b'2')
        self.assertFalse(list(self.repo.glob('data')))

    def test_blocked_generation_does_not_write_files(self):
        with patch.object(prepare, '_generate', return_value={'status': 'blocked', 'reason': '실제 인증 연결이 필요해요.', 'files': [], 'checks': []}):
            with self.assertRaisesRegex(issues.StoreError, '실제 인증'):
                prepare.prepare(self.project)
        self.assertFalse((self.repo / 'ecosystem.config.js').exists())

    def test_model_cannot_overwrite_or_escape_the_project(self):
        for path in ('../outside.py', 'C:/outside.py', '.env', 'backend/main.py', 'CON.py'):
            payload = result()
            payload['files'][1]['path'] = path
            with patch.object(prepare, '_generate', return_value=payload):
                with self.assertRaises(issues.StoreError):
                    prepare.prepare(self.project)
            self.assertFalse((self.repo / 'ecosystem.config.js').exists())
        self.assertEqual((self.repo / '.env').read_text(), 'TEST_SECRET=do-not-forward\n')

    def test_syntax_or_self_check_failure_keeps_original_untouched(self):
        for index, content in ((1, 'def bad(:\n'), (2, 'raise AssertionError("not ready")\n')):
            payload = result()
            payload['files'][index]['content'] = content
            with patch.object(prepare, '_generate', return_value=payload):
                with self.assertRaises(issues.StoreError):
                    prepare.prepare(self.project)
            self.assertFalse((self.repo / 'ecosystem.config.js').exists())

    def test_user_changes_during_generation_are_preserved(self):
        def generated(*args):
            (self.repo / 'README.md').write_text('사람이 수정했어요.', encoding='utf-8')
            return result()
        with patch.object(prepare, '_generate', side_effect=generated):
            with self.assertRaisesRegex(issues.StoreError, '변경'):
                prepare.prepare(self.project)
        self.assertEqual((self.repo / 'README.md').read_text(encoding='utf-8'), '사람이 수정했어요.')
        self.assertFalse((self.repo / 'ecosystem.config.js').exists())

    def test_existing_dirty_project_does_not_call_paid_agent(self):
        (self.repo / 'new.py').write_text('x=1\n')
        with patch.object(prepare, '_generate') as generate:
            with self.assertRaises(issues.StoreError):
                prepare.prepare(self.project)
        generate.assert_not_called()

    def test_generated_checks_do_not_receive_database_or_agent_credentials(self):
        payload = result()
        payload['files'][2]['content'] += "import os\nassert 'DATABASE_URL' not in os.environ\nassert 'DEV_CODEX_AGENT_KEY' not in os.environ\n"
        with patch.dict(os.environ, {'DATABASE_URL': 'private-database', 'DEV_CODEX_AGENT_KEY': 'private-agent'}), patch.object(prepare, '_generate', return_value=payload):
            prepare.prepare(self.project)

    def test_credentials_cannot_be_written_into_generated_json(self):
        payload = result()
        payload['files'].append({'path': 'service-config.json', 'content': '{"password":"unsafe-password"}'})
        with patch.object(prepare, '_generate', return_value=payload):
            with self.assertRaises(issues.StoreError):
                prepare.prepare(self.project)
        self.assertFalse((self.repo / 'ecosystem.config.js').exists())

    def test_generation_timeout_stops_the_agent(self):
        directory = self.root / 'timeout'
        directory.mkdir()
        settings = {'provider_order': ['codex'], 'agent_orders': {'provider_order': []}}
        proc = MagicMock()
        proc.poll.return_value = None
        proc.communicate.side_effect = [subprocess.TimeoutExpired('agent', 1), ('', '')]
        snapshot = prepare.audit.collect_snapshot('APP')
        with patch.object(prepare.auto_settings, 'get_settings', return_value=settings), patch.object(prepare.automation, 'provider_available', return_value=True), patch.object(prepare.subprocess, 'Popen', return_value=proc), patch.object(prepare.review, '_kill_tree') as stop:
            with self.assertRaisesRegex(issues.StoreError, '시간이 초과'):
                prepare._generate(self.project, snapshot, directory)
        stop.assert_called_once_with(proc)
        self.assertFalse((self.repo / 'ecosystem.config.js').exists())

    def test_provider_commands_use_stdin_schema_and_do_not_inherit_secrets(self):
        snapshot = prepare.audit.collect_snapshot('APP')
        directory = self.root / 'generation'
        directory.mkdir()
        for provider in ('codex', 'claude'):
            proc = MagicMock(returncode=0)
            proc.poll.return_value = 0
            out = json.dumps({'structured_output': result()}) if provider == 'claude' else json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(result())}})
            proc.communicate.return_value = (out, '')
            settings = {'provider_order': [provider], 'agent_orders': {'provider_order': [{'provider': provider, 'model': ''}]}}
            with patch.object(prepare.auto_settings, 'get_settings', return_value=settings), patch.object(prepare.automation, 'provider_available', return_value=True), patch.object(prepare.subprocess, 'Popen', return_value=proc) as launch, patch.dict(os.environ, {'DEV_CODEX_AGENT_KEY': 'do-not-forward', 'SECRET_TOKEN': 'do-not-forward'}):
                self.assertEqual(prepare._generate(self.project, snapshot, directory), result())
            command = launch.call_args.args[0]
            self.assertNotIn('DEV_CODEX_AGENT_KEY', launch.call_args.kwargs['env'])
            self.assertNotIn('SECRET_TOKEN', launch.call_args.kwargs['env'])
            self.assertIn('127.0.0.1:8400', proc.communicate.call_args.args[0])
            self.assertNotIn('do-not-forward', proc.communicate.call_args.args[0])
            if provider == 'codex':
                self.assertIn('mcp_servers={}', command)
                self.assertIn('--output-schema', command)
            else:
                self.assertEqual(command[command.index('--tools') + 1], '')

    def test_cli_failure_details_distinguish_budget_auth_and_private_output(self):
        budget = json.dumps({'subtype': 'error_max_budget_usd', 'is_error': True, 'result': 'Budget exceeded'})
        self.assertIn('비용 상한', prepare._failure_detail(budget, '', 'claude'))
        codex = json.dumps({'type': 'turn.failed', 'error': {'message': 'Authentication failed'}})
        self.assertIn('Authentication failed', prepare._failure_detail(codex, '', 'codex'))
        self.assertIn('unknown option', prepare._failure_detail('', 'unknown option --restricted', 'claude'))
        self.assertNotIn('private-password', prepare._failure_detail('', 'password=private-password', 'claude'))
        self.assertNotIn('backend/bootstrap.py', prepare._failure_detail(json.dumps({'structured_output': result()}), '', 'claude'))

    def test_nonzero_exit_reports_the_actual_error_instead_of_guessing_login(self):
        snapshot = prepare.audit.collect_snapshot('APP')
        directory = self.root / 'failed-agent'
        directory.mkdir()
        settings = {'provider_order': ['claude'], 'agent_orders': {'provider_order': []}}
        proc = MagicMock(returncode=1)
        proc.poll.return_value = 1
        proc.communicate.return_value = (json.dumps({'subtype': 'error_max_budget_usd', 'is_error': True}), '')
        with patch.object(prepare.auto_settings, 'get_settings', return_value=settings), patch.object(prepare.automation, 'provider_available', return_value=True), patch.object(prepare.subprocess, 'Popen', return_value=proc):
            with self.assertRaisesRegex(issues.StoreError, 'claude, 종료 코드 1.*비용 상한'):
                prepare._generate(self.project, snapshot, directory)


if __name__ == '__main__':
    unittest.main()
