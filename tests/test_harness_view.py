"""Harness 조회 API(DEV-132-1) — NS·DEV·MO × claude/codex × review/execute의 층별 구성, 비밀 비노출, 인증을 실제 앱 요청으로 검사한다."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as A  # noqa: E402
import auth  # noqa: E402
import harness  # noqa: E402
import harness_view  # noqa: E402
import issues  # noqa: E402
import review  # noqa: E402

root = Path(tempfile.mkdtemp())
for key in ('NS', 'DEV', 'MO'):
    repo = root / key
    repo.mkdir()
    (repo / 'AGENTS.md').write_text(f'# {key} 규칙\n', encoding='utf-8')
    (repo / 'app.py').write_text(f'"""{key} 앱 본체."""\n', encoding='utf-8')
    (repo / '.env').write_text('SECRET=zzz\n', encoding='utf-8')
    subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
    subprocess.run(['git', 'add', 'AGENTS.md', 'app.py'], cwd=repo, check=True)


def fake_nightshift(req):
    token = req.headers.get('cookie', '').split('=', 1)[-1]
    user = {'adm': {'id': 1, 'username': 'admin', 'role': 'admin'}}.get(token)
    return httpx.Response(200, json={'user': user})


auth._client = httpx.AsyncClient(transport=httpx.MockTransport(fake_nightshift))
H = {'X-Requested-With': 'dev'}
with TestClient(A.app) as client:
    client.cookies.set('ns_session', 'adm')
    for key in ('NS', 'DEV', 'MO'):
        issues.create_project({'kind': 'human', 'name': 'admin'}, key, key, local_path=str(root / key))
    common = harness.text()
    assert common
    for key in ('NS', 'DEV', 'MO'):
        for provider in ('claude', 'codex'):
            for stage in ('review', 'execute'):
                r = client.get('/api/harness', params={'project': key, 'provider': provider, 'stage': stage})
                assert r.status_code == 200, (key, provider, stage, r.text)
                d = r.json()
                prompt = d['layers']['prompt']['text']
                assert prompt.startswith(common[:200]), (key, provider, stage)
                segs = d['layers']['prompt']['segments']
                assert [s['layer'] for s in segs][:2] == [1, 'map'], [s['layer'] for s in segs]
                assert 'app.py — ' + key + ' 앱 본체.' in d['layers']['file_map']['content']
                assert d['layers']['file_map']['source'].startswith('server/filemap.py') and not d['layers']['file_map']['editable']
                assert d['layers']['common']['editable'] and d['layers']['common']['source'] == 'harness/COMMON.md'
                names = [f['label'] for f in d['layers']['project']]
                assert 'AGENTS.md' in names and not any('.env' in n or 'mcp' in n or 'auth' in n for n in names), names
                assert next(f for f in d['layers']['project'] if f['label'] == 'AGENTS.md')['content'] == f'# {key} 규칙\n'
                e = d['layers']['enforcement']
                assert not e['editable'] and any(t.startswith('mcp__dev__') for t in e['allowed_tools'])
                if provider == 'claude':
                    assert e['blocked_tools']
                if stage == 'execute':
                    assert e['limits']['budget_usd'] and e['merge_rules'] and 'GH_TOKEN' in e['secret_env_removed']
                body = r.text
                assert str(review.MCP_CONFIG) not in body and '--mcp-config' not in body, (key, provider, stage)
                assert str(review.MCP_CONFIG).replace('\\', '/') not in body
                assert 'SECRET=zzz' not in body and 'bearer_token_env_var' not in body and 'DEV_CODEX_AGENT_KEY' not in str(e['args'])
    # 입력 검증
    assert client.get('/api/harness', params={'project': 'NS', 'provider': 'x'}).status_code == 400
    assert client.get('/api/harness', params={'project': 'NOPE'}).status_code == 404
    assert client.get('/api/harness').status_code == 422
    # 비로그인·에이전트 키 거절
    agent = client.post('/api/agents', json={'name': 'reader', 'kind': 'chat'}, headers=H).json()['key']
    client.cookies.clear()
    assert client.get('/api/harness', params={'project': 'NS'}).status_code == 401
    bearer = {'Authorization': f'Bearer {agent}'}
    assert client.get('/api/harness', params={'project': 'NS'}, headers=bearer).status_code == 403
# 헬퍼 고정
assert harness_view.split_command(['c', '-p', 'P', '--x'], 'claude') == ('P', ['c', '-p', '--x'])
assert harness_view.split_command(['c', 'exec', '--model', 'm', 'P'], 'codex') == ('P', ['c', 'exec', '--model', 'm'])
assert harness_view.public_args(['claude', '-p', '--mcp-config', '/x/.mcp.json', '--a']) == ['-p', '--a']
print('harness view API OK')
