"""Harness 편집 저장·이력 API(DEV-132-2) — 임시 git 저장소로 커밋·diff·경로 차단·실행 중 차단·base_sha 충돌·권한을 실제 앱 요청으로 검사한다."""
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
import config  # noqa: E402
import db  # noqa: E402
import execute  # noqa: E402
import issues  # noqa: E402


def git(cwd, *args):
    return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def make_repo(path: Path, files: dict):
    path.mkdir(parents=True)
    git(path, 'init', '-q', '-b', execute.BASE_BRANCH)
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text, encoding='utf-8')
    (path / '.env').write_text('SECRET=zzz\n', encoding='utf-8')
    git(path, 'add', '-A', ':!.env')
    git(path, 'commit', '-qm', 'init')


root = Path(tempfile.mkdtemp())
make_repo(root / 'NS', {'AGENTS.md': '# NS\n', 'docs/project/00_a.md': 'a\n'})
make_repo(root / 'devrepo', {'harness/COMMON.md': '공통\n', 'docs/DESIGN.md': '디자인\n',
                           'harness/prompts/review_claude.md': '{ref} 검토 {web_domains}\n'})
config.REPO_ROOT = root / 'devrepo'   # 1층은 임시 저장소에 쓴다


def fake_nightshift(req):
    token = req.headers.get('cookie', '').split('=', 1)[-1]
    user = {'adm': {'id': 1, 'username': 'admin', 'role': 'admin'}}.get(token)
    return httpx.Response(200, json={'user': user})


auth._client = httpx.AsyncClient(transport=httpx.MockTransport(fake_nightshift))
H = {'X-Requested-With': 'dev'}


def put(client, scope, path, content, base):
    return client.put('/api/harness/file', json={'scope': scope, 'path': path, 'content': content, 'base_sha': base}, headers=H)


with TestClient(A.app) as client:
    client.cookies.set('ns_session', 'adm')
    actor = {'kind': 'human', 'name': 'admin'}
    issues.create_project(actor, 'NS', 'NS', local_path=str(root / 'NS'))
    issues.create_project(actor, 'DEV', 'DEV', local_path=str(root / 'devrepo'))
    ns = root / 'NS'
    sha0 = git(ns, 'rev-parse', 'main:AGENTS.md')

    # 저장 → 커밋과 diff
    r = put(client, 'NS', 'AGENTS.md', '# NS\n새 규칙\n', sha0)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d['commit'] == git(ns, 'rev-parse', 'HEAD') and '+새 규칙' in d['diff'], d
    assert d['base_sha'] == git(ns, 'rev-parse', 'main:AGENTS.md') != sha0
    assert git(ns, 'log', '-1', '--format=%an') == 'dev harness editor'
    assert git(ns, 'status', '--porcelain', '--untracked-files=no') == ''
    assert (ns / 'AGENTS.md').read_text(encoding='utf-8') == '# NS\n새 규칙\n'

    # 없던 파일은 새로 만든다
    r = put(client, 'NS', 'CLAUDE.md', '@AGENTS.md\n', '')
    assert r.status_code == 200 and git(ns, 'cat-file', '-e', 'main:CLAUDE.md') == '', r.text

    # base_sha 불일치 409, 내용 같음 400
    assert put(client, 'NS', 'AGENTS.md', 'x\n', sha0).status_code == 409
    assert put(client, 'NS', 'AGENTS.md', '# NS\n새 규칙\n', d['base_sha']).status_code == 400

    # 허용 밖 경로 400
    for bad in ('.env', '../x', 'docs/../.env', '/etc/passwd', 'docs/project/06_x.md', 'harness/COMMON.md', 'app.py', 'a\\b'):
        assert put(client, 'NS', bad, 'x', '').status_code == 400, bad
    assert not (root / 'x').exists()
    assert put(client, 'common', 'AGENTS.md', 'x', '').status_code == 400
    assert put(client, 'NOPE', 'AGENTS.md', 'x', '').status_code == 404
    assert put(client, 'NS', 'AGENTS.md', 'x' * (256 * 1024 + 1), d['base_sha']).status_code == 400

    # 심볼릭 링크 400 (만들 수 없는 환경이면 건너뜀)
    try:
        os.symlink(root / 'devrepo' / 'docs' / 'DESIGN.md', ns / 'docs' / 'project' / '01_link.md')
        link_ok = True
    except (OSError, NotImplementedError):
        link_ok = False
    if link_ok:
        assert put(client, 'NS', 'docs/project/01_link.md', 'x', '').status_code == 400

    # 커밋하지 않은 변경이 있으면 거절
    (ns / 'AGENTS.md').write_text('손으로 고침\n', encoding='utf-8')
    cur = git(ns, 'rev-parse', 'main:AGENTS.md')
    assert put(client, 'NS', 'AGENTS.md', 'y\n', cur).status_code == 409
    git(ns, 'checkout', '--', 'AGENTS.md')

    # 실행 중 run이 있으면 409 — 프로젝트 파일은 그 프로젝트만, 공통 파일은 어떤 실행이든
    with db.connect() as c:
        pid = c.execute("SELECT id FROM projects WHERE key='DEV'").fetchone()[0]
    dev_issue = issues.create_issue(actor, 'DEV', '실행 중', '')
    with db.connect() as c:
        c.execute("INSERT INTO runs(issue_id, mode, status, actor, started_at) VALUES (?, 'execute', 'running', 'agent', '2026-01-01T00:00:00+00:00')",
                  (dev_issue['id'],))
    cur = git(ns, 'rev-parse', 'main:AGENTS.md')
    assert put(client, 'NS', 'AGENTS.md', 'z\n', cur).status_code == 200   # 다른 프로젝트 실행은 무관
    cc = git(root / 'devrepo', 'rev-parse', 'main:harness/COMMON.md')
    assert put(client, 'common', 'harness/COMMON.md', '새 공통\n', cc).status_code == 409
    assert put(client, 'DEV', 'AGENTS.md', 'x\n', '').status_code == 409
    with db.connect() as c:
        c.execute("UPDATE runs SET status='ok'")
    r = put(client, 'common', 'harness/COMMON.md', '새 공통\n', cc)
    assert r.status_code == 200 and '+새 공통' in r.json()['diff'], r.text

    # 4층 프롬프트: 정상 저장은 dev 저장소에 커밋, 자리표시 문제는 400+이유, 허용 밖 경로 400
    dev = root / 'devrepo'
    pf = 'harness/prompts/review_claude.md'
    pb = git(dev, 'rev-parse', f'main:{pf}')
    r = put(client, 'prompts', pf, '{ref}를 검토해요. {web_domains}\n', pb)
    assert r.status_code == 200 and r.json()['commit'] == git(dev, 'rev-parse', 'HEAD') and '+{ref}를' in r.json()['diff'], r.text
    pb = r.json()['base_sha']
    head = git(dev, 'rev-parse', 'HEAD')
    r = put(client, 'prompts', pf, '{web_domains}만 남김\n', pb)
    assert r.status_code == 400 and '{ref}' in r.json()['detail'], r.text
    r = put(client, 'prompts', pf, '{ref} {nope}\n', pb)
    assert r.status_code == 400 and '{nope}' in r.json()['detail'], r.text
    assert git(dev, 'rev-parse', 'HEAD') == head
    assert put(client, 'prompts', pf, '{ref}\n', 'stale').status_code == 409
    for bad in ('harness/COMMON.md', 'harness/prompts/unknown.md', 'harness/prompts/../COMMON.md', '.env', 'harness/prompts/'):
        assert put(client, 'prompts', bad, '{ref}', '').status_code == 400, bad
    assert put(client, 'common', pf, 'x', '').status_code == 400
    assert client.get('/api/harness/history', params={'scope': 'prompts', 'path': pf}).json()['commits'][0]['subject'] == f'Harness 편집: {pf}'

    # 이력에 diff 노출
    h = client.get('/api/harness/history', params={'scope': 'NS', 'path': 'AGENTS.md'})
    assert h.status_code == 200, h.text
    hd = h.json()
    assert len(hd['commits']) == 3 and '+새 규칙' in hd['commits'][1]['diff'] and hd['commits'][0]['subject'] == 'Harness 편집: AGENTS.md', hd
    assert hd['base_sha'] == git(ns, 'rev-parse', 'main:AGENTS.md')
    assert client.get('/api/harness/history', params={'scope': 'NS', 'path': '.env'}).status_code == 400

    # 비로그인 401 · 에이전트 키 403
    agent = client.post('/api/agents', json={'name': 'writer', 'kind': 'chat'}, headers=H).json()['key']
    client.cookies.clear()
    assert put(client, 'NS', 'AGENTS.md', 'q\n', '').status_code == 401
    bearer = {**H, 'Authorization': f'Bearer {agent}'}
    r = client.put('/api/harness/file', json={'scope': 'NS', 'path': 'AGENTS.md', 'content': 'q\n', 'base_sha': ''}, headers=bearer)
    assert r.status_code == 403, r.text
    assert client.get('/api/harness/history', params={'scope': 'NS', 'path': 'AGENTS.md'}, headers=bearer).status_code == 403
    assert git(ns, 'show', 'main:AGENTS.md') == 'z'
print('harness edit API OK')
