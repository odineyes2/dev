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
import prompts  # noqa: E402
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
        r = client.get('/api/harness', params={'project': key})
        assert r.status_code == 200, (key, r.text)
        d = r.json()
        L = d['layers']
        assert list(L) == ['common', 'project', 'file_map', 'prompts', 'settings'], list(L)
        assert 'prompt' not in L and 'enforcement' not in L
        assert 'app.py — ' + key + ' 앱 본체.' in L['file_map']['content']
        assert L['file_map']['source'].startswith('server/filemap.py') and not L['file_map']['editable']
        assert L['common']['editable'] and L['common']['source'] == 'harness/COMMON.md'
        names = [f['label'] for f in L['project']]
        assert 'AGENTS.md' in names and not any('.env' in n or 'mcp' in n or 'auth' in n for n in names), names
        agents = next(f for f in L['project'] if f['label'] == 'AGENTS.md')
        assert agents['content'] == f'# {key} 규칙\n' and 'Codex' in agents['readers'] and 'CLAUDE.md' in agents['readers']
        # 4층 — 프롬프트 파일 7개, 자리표시 안내
        files = L['prompts']['files']
        assert sorted(f['name'] for f in files) == sorted(prompts.SLOTS), [f['name'] for f in files]
        for f in files:
            assert f['exists'] and f['content'] and f['slots'] == sorted(prompts.SLOTS[f['name']]) and f['editable']
            assert f['path'] == 'harness/prompts/' + f['name'] and 'base_sha' in f
        assert next(f for f in files if f['name'] == 'execute_claude.md')['required'] == ['ref', 'test_scope']
        assert L['prompts']['code_filled']
        # 5층 — 네 조합
        items = L['settings']['items']
        assert [(i['stage'], i['provider']) for i in items] == [('review', 'claude'), ('review', 'codex'), ('execute', 'claude'), ('execute', 'codex')]
        for e in items:
            assert not e['editable'] and any(t.startswith('mcp__dev__') for t in e['allowed_tools'])
            if e['provider'] == 'claude':
                assert e['blocked_tools']
            if e['stage'] == 'execute':
                assert e['limits']['budget_usd'] and e['merge_rules'] and 'GH_TOKEN' in e['secret_env_removed']
            assert 'DEV_CODEX_AGENT_KEY' not in str(e['args'])
        body = r.text
        assert str(review.MCP_CONFIG) not in body and '--mcp-config' not in body, key
        assert str(review.MCP_CONFIG).replace('\\', '/') not in body
        assert 'SECRET=zzz' not in body and 'bearer_token_env_var' not in body
    # 입력 검증
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
# 화면(DEV-132-3) — 탭·라우트가 있고 편집 버튼은 파일 카드(1·2층)에만 붙는다
_root = Path(__file__).resolve().parent.parent
_js = (_root / 'static' / 'app.js').read_text(encoding='utf-8')
assert 'href="#/harness"' in (_root / 'static' / 'index.html').read_text(encoding='utf-8') and "name === 'harness'" in _js
_fn = _js[_js.index('async function renderHarness'):_js.index('async function renderAgents')]
assert _fn.count('class="harness-edit"') == 1 and "fileCard(L.common" in _fn
assert "layer(3, 'dev 프롬프트', L.prompt.source, segs || pre(L.prompt.text), false)" in _fn and "'dev 강제 장치'" in _fn
assert 'server/filemap.py' in (_root / 'server' / 'harness_view.py').read_text(encoding='utf-8')
print('harness view API OK')
