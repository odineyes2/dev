"""검사 설정 API 권한·영속화·검사 단계와 파일 제외·에이전트 지시를 확인한다."""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
os.environ['NTFY_TOPIC'] = ''
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import httpx
from fastapi.testclient import TestClient
import app as A
import auth, db, execute, orchestrate, regression

assert regression.values() == {}
assert regression.enabled('agent_capture')
db.init()
auth._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(
    200, json={'user': {'username': 'admin', 'role': 'admin'}})))
client = TestClient(A.app)
assert client.get('/api/settings/regression').status_code == 401
client.cookies.set('ns_session', 'test')
url = '/api/settings/regression'
headers = {'X-Requested-With': 'dev'}
assert all(row['mode'] == 'auto' for row in client.get(url).json()['checks'])
assert client.put(url, json={'checks': {'server_merge': 'off'}}).status_code == 403
for changes in ({'unknown': False}, {'server_merge': 0}, {}):
    assert client.put(url, json={'checks': changes}, headers=headers).status_code == 400
assert client.put(url, json={'checks': {'server_merge': 'off', 'file:test_ui.py': 'off'}}, headers=headers).status_code == 200
assert not regression.enabled('server_merge')
agent, key = auth.create_agent('regression', 'openai', '')
assert client.put(url, json={'checks': {'server_merge': 'on'}}, headers={'Authorization': 'Bearer ' + key}).status_code == 403

repo = Path(tempfile.mkdtemp())
(repo / 'tests').mkdir()
for name in ('test_ui.py', 'test_other.py'):
    (repo / 'tests' / name).write_text('raise Exception("must be mocked")')
with patch.object(orchestrate.subprocess, 'run') as run:
    run.return_value.returncode = 0
    assert orchestrate.run_tests(repo) is None
    assert not run.called
    assert orchestrate.run_tests(repo, stage='server_completion') is None
    assert run.call_count == 1 and run.call_args.args[0][1].endswith('test_other.py')
    assert run.call_args.kwargs['env']['DEV_TEST_RUN'] == '1'
client.put(url, json={'checks': {'agent_related': 'off', 'agent_api': 'off', 'agent_capture': 'off'}}, headers=headers).raise_for_status()
assert '실행하지 않고' in execute.test_scope('DEV-1')
assert 'test_ui.py' in execute.test_scope('DEV-1')
assert '생략' in execute.api_test_instructions()
assert '캡처를 실행하지 않고' in execute.capture_instructions('Read')
assert next(row for row in client.get(url).json()['checks'] if row['key'] == 'agent_capture')['mode'] == 'off'
print('regression settings OK')
