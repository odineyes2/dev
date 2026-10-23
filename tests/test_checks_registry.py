"""검사 등록부(DEV-161-1) — R 번호 고유·고정 항목 거절·T 파일 번호(py·JS)·프로젝트별 파일 설정·JS 실행·API를 임시 저장소로 검사한다."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
os.environ['NTFY_TOPIC'] = ''
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import httpx
from fastapi.testclient import TestClient
import app as A
import auth, checks, db, issues, orchestrate, regression

db.init()
me = {'kind': 'human', 'name': 'admin'}

# 1. 번호는 R01.. 순서로 중복 없이, 고정 항목은 Settings 키가 아니다
numbers = [r['number'] for r in checks.REGISTRY]
assert len(numbers) == len(set(numbers)) and numbers == [f'R{i:02d}' for i in range(1, 24)], numbers
assert len({r['key'] for r in checks.REGISTRY}) == len(checks.REGISTRY)
assert all(r['mode'] == 'always' for r in checks.REGISTRY if r['number'] in ('R16', 'R19', 'R20', 'R21', 'R22', 'R23'))
assert {c['key']: c['number'] for c in regression.catalog() if not c['key'].startswith('file:')}['server_merge'] == 'R07'


def make_repo(files):
    repo = Path(tempfile.mkdtemp())
    (repo / 'tests').mkdir()
    for name, text in files.items():
        (repo / 'tests' / name).write_text(text, encoding='utf-8')
    subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
    return repo


js_ok = "const test = require('node:test');\ntest('ok', () => {});\n"
js_bad = "const test = require('node:test');\ntest('bad', () => { throw new Error('x'); });\n"
repo_a = make_repo({'test_x.py': '"""x"""\n', 'test_page.js': js_ok})
repo_b = make_repo({'test_x.py': '"""x"""\n'})
issues.create_project(me, 'AA', 'a', '', str(repo_a))
issues.create_project(me, 'BB', 'b', '', str(repo_b))

# 2. 파일 번호: py·JS 모두 T-<프로젝트>-### 부여, 재탐색해도 유지, 삭제 뒤 새 파일은 새 번호
rows = {c['name']: c for c in regression.catalog('AA') if c['key'].startswith('file:')}
assert set(rows) == {'test_x.py', 'test_page.js'}, rows
assert rows['test_page.js']['kind'] == 'js' and rows['test_x.py']['kind'] == 'py'
first = {n: r['number'] for n, r in rows.items()}
assert sorted(first.values()) == ['T-AA-001', 'T-AA-002'], first
assert {n: r['number'] for n, r in ((c['name'], c) for c in regression.catalog('AA') if c['key'].startswith('file:'))} == first
(repo_a / 'tests' / 'test_page.js').unlink()
regression.catalog('AA')
(repo_a / 'tests' / 'test_new.py').write_text('"""n"""\n', encoding='utf-8')
new = {c['name']: c['number'] for c in regression.catalog('AA') if c['key'].startswith('file:')}
assert new['test_new.py'] == 'T-AA-003' and 'test_page.js' not in new, new
assert regression.catalog('BB')[-1]['number'] == 'T-BB-001'

# 3. 프로젝트별 파일 설정: AA의 test_x.py를 Off해도 BB의 같은 이름은 그대로
regression.update(me, {'file:test_x.py': 'off'}, 'AA')
mode = lambda p: {c['name']: c['mode'] for c in regression.settings(p)['checks']}['test_x.py']
assert mode('AA') == 'off' and mode('BB') == 'auto'
assert regression.resolve(None, str(repo_b))['checks']['file:test_x.py']['mode'] == 'auto'
assert regression.resolve(None, str(repo_a))['checks']['file:test_x.py']['mode'] == 'off'

# 4. JS 검사는 node --test로 실행되고, 실패하면 R07 실패 메시지에 번호가 붙는다
(repo_a / 'tests' / 'test_bad.js').write_text(js_bad, encoding='utf-8')
regression.update(me, {'file:test_x.py': 'auto', 'server_merge': 'on'}, 'AA')
regression.update(me, {'file:test_new.py': 'off'}, 'AA')
out = orchestrate.run_tests(str(repo_a), 'server_merge', changed=['server/x.py'])
assert out and 'R07' in out and 'test_bad.js' in out and 'T-AA-' in out, out
assert orchestrate.run_test(repo_a, repo_a / 'tests' / 'test_bad.js').returncode != 0

# 5. API: 로그인 필요, project 지정, 없는 프로젝트 404, 고정·관리형 항목은 PUT 400
auth._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(
    200, json={'user': {'username': 'admin', 'role': 'admin'}})))
client = TestClient(A.app)
url = '/api/settings/regression'
assert client.get(url).status_code == 401
client.cookies.set('ns_session', 'test')
headers = {'X-Requested-With': 'dev'}
body = client.get(url + '?project=AA').json()
assert body['project'] == 'AA' and [r['number'] for r in body['registry']][:3] == ['R01', 'R02', 'R03']
assert any(r['number'] == 'R19' and r['mode'] == 'always' for r in body['registry'])
assert any(c['number'].startswith('T-AA-') for c in body['checks'])
assert client.get(url + '?project=NOPE').status_code == 404
for key in ('fixed_auth', 'reproduce', 'agent_js_syntax'):
    assert client.put(url, json={'checks': {key: 'off'}}, headers=headers).status_code == 400, key
assert client.put(url + '?project=BB', json={'checks': {'file:test_x.py': 'on'}}, headers=headers).status_code == 200
assert mode('BB') == 'on' and mode('AA') == 'auto'
print('checks registry OK')
