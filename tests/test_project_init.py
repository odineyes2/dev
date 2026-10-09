"""임시 Projects와 실제 HTTP 라우트로 생성·재시도·경로·승인 경계를 검사한다."""
import os
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))
from fastapi.testclient import TestClient
import app
import attachments
import config
import db
import issues
import project_init as pi
import project_docs

H = {'kind': 'human', 'name': 'admin'}
HEADERS = {'X-Requested-With': 'dev'}


def fixture():
    return {'description': '개인용 모델 조사 도구. API 지원은 미검증이다.',
            'files': [{'path': path, 'content': '# ' + path + '\n미정 사항은 원본 기획서를 따른다.\n'} for path in sorted(pi.REQUIRED)] +
                     [{'path': 'backend/main.py', 'content': 'def health():\n    return {"status": "ready"}\n'},
                      {'path': '.gitignore', 'content': '*.md\n'}],
            'issues': [{'title': '첫 수집기', 'body': 'API 계약을 확인한 뒤 구현한다. 미확인 상태를 보존한다.',
                        'plan': '## Tasks\n1. 수집 골격 | 파일: backend/collector.py | 확인: 모의 응답 재처리 검사 | 선행: 없음\n2. 오류 처리 | 파일: tests/test_collector.py | 확인: 재시도 검사 | 선행: 1\n\n## 정해야 할 것\n- API 지원 범위 확인'}]}


class InitializationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.projects = root / 'Projects'
        self.projects.mkdir()
        self.addCleanup(patch.stopall)
        patch.object(pi, 'ROOT', self.projects).start()
        patch.object(config, 'DATA_DIR', root / 'data').start()
        patch.object(config, 'DB_PATH', root / 'data' / 'dev.db').start()
        patch.object(pi.automation, 'provider_available', return_value=True).start()
        self.start = patch.object(pi, 'start').start()
        self.auth = patch.object(app, '_resolve_actor', AsyncMock(return_value=(H, ''))).start()
        db.init()
        self.client = TestClient(app.app)
        upload = self.client.post('/api/attachments', files={'file': ('spec.md', '# 제품\n확정: 개인 도구\n제안: Python'.encode(), 'text/markdown')}, headers=HEADERS)
        self.assertEqual(upload.status_code, 200, upload.text)
        self.body = {'request_id': str(uuid.uuid4()), 'key': 'NEW', 'name': '새 제품', 'folder': 'new-product', 'provider': 'codex', 'attachment_id': upload.json()['id']}

    def request(self):
        r = self.client.post('/api/projects/initialize', json=self.body, headers=HEADERS)
        self.assertEqual(r.status_code, 202, r.text)
        return r.json()

    def execute(self):
        with patch.object(pi, 'generate', return_value=fixture()) as generate:
            pi._run('NEW')
        return generate

    def test_complete_http_git_documents_tasks(self):
        self.request()
        self.execute()
        status = self.client.get('/api/projects/NEW/initialization').json()
        self.assertEqual(status['status'], 'complete', status)
        target = self.projects / 'new-product'
        self.assertEqual(pi._git(target, 'rev-list', '--count', 'HEAD'), '1')
        self.assertEqual(pi._git(target, 'status', '--porcelain'), '')
        for path in pi.REQUIRED | {'PROJECT_SPEC.md'}:
            self.assertTrue(pi._git(target, 'ls-files', '--', path), path)
        self.assertEqual(project_docs.read_document('NEW', 'docs/project/01_PRD.md')['status'], 'available')
        parent = issues.get_issue(status['refs'][0])
        self.assertIsNone(parent['approval'])
        self.assertEqual(len(parent['children']), 2)
        child = issues.get_issue(parent['children'][1]['ref'])
        self.assertTrue(child['blocked_by'])
        with db.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM decisions').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
        before = len(issues.list_issues(project='NEW'))
        self.request()
        self.assertEqual(len(issues.list_issues(project='NEW')), before)

    def test_duplicate_and_live_project_guards(self):
        self.request()
        self.request()
        self.assertEqual(len(issues.list_projects()), 1)
        self.assertEqual(len(issues.list_issues(project='NEW')), 1)
        self.assertEqual(self.client.patch('/api/projects/NEW', json={'local_path': '/elsewhere'}, headers=HEADERS).status_code, 409)
        self.assertFalse(issues.check_project_deletion(H, 'NEW')['can_delete'])

    def test_input_paths_permissions_and_csrf(self):
        for folder in ('../bad', 'C:/bad', 'CON', 'a/b', '.hidden'):
            response = self.client.post('/api/projects/initialize', json={**self.body, 'folder': folder}, headers=HEADERS)
            self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(issues.list_projects(), [])
        self.assertEqual(self.client.post('/api/projects/initialize', json=self.body).status_code, 403)
        self.auth.return_value = ({'kind': 'agent', 'id': 1}, '')
        self.assertEqual(self.client.post('/api/projects/initialize', json=self.body, headers=HEADERS).status_code, 403)
        self.auth.return_value = (None, 'anon')
        self.assertEqual(self.client.get('/api/projects/NEW/initialization').status_code, 401)

    def test_existing_folder_and_attachment_ownership(self):
        target = self.projects / self.body['folder']
        target.mkdir()
        (target / 'keep.txt').write_text('keep')
        self.assertEqual(self.client.post('/api/projects/initialize', json=self.body, headers=HEADERS).status_code, 409)
        self.assertEqual((target / 'keep.txt').read_text(), 'keep')
        self.auth.return_value = ({'kind': 'human', 'name': 'other'}, '')
        self.assertEqual(self.client.post('/api/projects/initialize', json={**self.body, 'folder': 'another'}, headers=HEADERS).status_code, 403)

    def test_generated_paths_and_invalid_graph(self):
        for path in ('../escape.md', 'C:/escape.md', '.git/config', '.env', 'docs/../escape.md', 'docs/CON.txt', 'README.md/child.py'):
            result = fixture()
            result['files'].append({'path': path, 'content': 'bad'})
            with self.assertRaises(ValueError, msg=path):
                pi.validate(result, self.projects / 'new-product')
        result = fixture()
        result['issues'][0]['plan'] = '## Tasks\n1. A | 파일: a.py | 확인: 검사 | 선행: 2\n2. B | 파일: b.py | 확인: 검사 | 선행: 1'
        with self.assertRaises(ValueError):
            pi.validate(result, self.projects / 'new-product')

    def test_bad_generation_retry_and_recovery(self):
        self.request()
        with patch.object(pi, 'generate', return_value={'bad': True}):
            pi._run('NEW')
        self.assertEqual(pi.status('NEW')['status'], 'failed')
        self.assertFalse((self.projects / 'new-product').exists())
        r = self.client.post('/api/projects/NEW/initialization/retry', headers=HEADERS)
        self.assertEqual(r.status_code, 202, r.text)
        self.execute()
        self.assertEqual(pi.status('NEW')['status'], 'complete')
        self.assertEqual(self.client.post('/api/projects/NEW/initialization/retry', headers=HEADERS).status_code, 409)
        pi._update('NEW', status='running')
        pi.recover()
        self.assertEqual(pi.status('NEW')['status'], 'failed')

    def test_database_failure_after_git_resumes_without_duplicates(self):
        self.request()
        with patch.object(pi, 'generate', return_value=fixture()), patch.object(pi, 'publish', side_effect=OSError('interrupted')):
            pi._run('NEW')
        self.assertEqual(pi.status('NEW')['status'], 'failed')
        self.assertTrue((self.projects / 'new-product' / '.git').is_dir())
        pi.retry(H, 'NEW')
        generate = self.execute()
        generate.assert_not_called()
        self.assertEqual(pi.status('NEW')['status'], 'complete')
        self.assertEqual(len(pi.status('NEW')['refs']), 1)

    def test_provider_adapters_use_stdin_and_no_tools_or_server_secrets(self):
        self.request()
        row = pi.get('NEW')
        for provider in ('codex', 'claude'):
            row['provider'] = provider
            proc = MagicMock(returncode=0)
            output = json.dumps({'structured_output': fixture()}) if provider == 'claude' else '\n'.join([
                json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '중간 설명'}}),
                json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(fixture())}})])
            proc.communicate.return_value = (output, '')
            with patch.dict(os.environ, {'DEV_CODEX_AGENT_KEY': 'do-not-forward', 'SECRET_TOKEN': 'do-not-forward'}), patch.object(pi.subprocess, 'Popen', return_value=proc) as launch:
                self.assertEqual(pi.generate(row), fixture())
            kwargs = launch.call_args.kwargs
            self.assertNotIn('SECRET_TOKEN', kwargs['env'])
            self.assertNotIn('DEV_CODEX_AGENT_KEY', kwargs['env'])
            self.assertIn(row['spec'], proc.communicate.call_args.args[0])
            command = launch.call_args.args[0]
            if provider == 'codex':
                self.assertIn('mcp_servers={}', command)
                self.assertIn('features.shell_tool=false', command)
                self.assertIn('--output-schema', command)
                self.assertEqual(command[-1], '-')
            else:
                self.assertEqual(command[command.index('--tools') + 1], '')
                self.assertIn('--strict-mcp-config', command)
                self.assertIn('--restricted', command)


if __name__ == '__main__':
    unittest.main()
