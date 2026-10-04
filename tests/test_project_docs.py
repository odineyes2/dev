"""임시 DB와 모의 실행으로 문서 요청·중복·재시도·provider·승인을 검사한다."""
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))
import db, issues, jobs, execute, project_docs

db.init()
me = {'kind': 'human', 'name': 'admin'}
issues.create_project(me, 'DOC', '대상', '', '/target', '사용자 제품 설명')
issues.create_project(me, 'EMPTY', '빈 설명', '', '/empty')

def denied(fn, status):
    try:
        fn()
    except issues.StoreError as e:
        assert e.status == status, e
    else:
        raise AssertionError('차단되어야 한다')

denied(lambda: project_docs.request_documents(me, 'EMPTY', 'codex'), 400)
denied(lambda: project_docs.request_documents(me, 'MISSING', 'codex'), 404)
denied(lambda: project_docs.request_documents(me, 'DOC', 'other'), 400)
denied(lambda: project_docs.request_documents({'kind': 'agent', 'id': 1}, 'DOC', 'codex'), 403)

with patch.object(jobs, 'busy', return_value=True):
    for provider in ('codex', 'claude'):
        request = project_docs.request_documents(me, 'doc', provider)
        ref = request['ref']
        parent = issues.get_issue(ref)
        assert parent['project_key'] == 'DOC' and parent['parent_ref'] is None
        assert parent['type_ids'] == [2] and parent['types'][0]['source'] == 'human'
        assert '사용자 제품 설명' in parent['body']
        assert all(path in parent['body'] and purpose in parent['body'] for path, purpose in project_docs.DOCUMENTS)
        assert jobs.list_jobs()[-1]['provider'] == provider
        assert project_docs.request_documents(me, 'DOC', provider)['ref'] == ref
        issues.update_issue(me, ref, {'type_ids': [1]})
        assert project_docs.request_documents(me, 'DOC', provider)['ref'] == ref
        assert issues.get_issue(ref)['type_ids'] == [1, 2]
        task = issues.create_issue(me, 'DOC', '문서 생성', parent=ref, body='바꿀 파일: docs/project/01_PRD.md, AGENTS.md')
        denied(lambda: jobs.enqueue(me, task['ref'], 'execute'), 409)
        issues.post_plan(me, ref, '대상 worktree에서 문서 작성')
        issues.decide(me, ref, 'approve', plan_version=1)
        result = jobs.enqueue(me, task['ref'], 'execute')
        assert next(j for j in jobs.list_jobs() if j['id'] == result['job_id'])['provider'] == provider
        other = 'claude' if provider == 'codex' else 'codex'
        denied(lambda: jobs.enqueue(me, task['ref'], 'execute', other), 409)
        denied(lambda: execute.start(me, task['ref'], other), 409)
        with patch.object(execute, 'prepare_worktree', return_value=Path('/target/worktree')) as prepare, \
             patch.object(execute, 'safe_env', return_value={}), \
             patch.object(execute, 'blocked_reason', return_value=None), \
             patch('review.running_ref', return_value=None), \
             patch('review.begin', return_value=(Path('/log'), 100)), \
             patch('review.launch') as launch, \
             patch.dict(os.environ, {'DEV_CODEX_AGENT_KEY': 'mock'}):
            execute.start(me, task['ref'])
            prepare.assert_called_once_with('/target', task['ref'])
            assert launch.call_args.args[3] == provider
        assert project_docs.request_documents(me, 'DOC', provider)['reviewed']

# 문서 Task도 기존 다른 저장소 변경 차단을 그대로 적용한다.
issues.create_project(me, 'OTHER', '다른 저장소', '', '/other-repo')
foreign = issues.create_issue(me, 'DOC', '범위 초과', parent=ref, body='바꿀 파일: /other-repo/AGENTS.md')
assert 'OTHER' in execute.blocked_reason(issues.get_issue(foreign['ref']), issues.get_issue(ref), wait=False)
with patch.object(jobs, 'busy', return_value=True):
    denied(lambda: jobs.enqueue(me, foreign['ref'], 'execute'), 409)

# 설명 수정은 요청 당시 스냅샷을 바꾸지 않는다. 실패 후 동일 이슈로 다시 등록한다.
issues.create_project(me, 'RETRY', '재시도', '', '/retry', '원래 설명')
with patch.object(jobs, 'enqueue', side_effect=RuntimeError('queue failed')):
    failed = project_docs.request_documents(me, 'RETRY', 'codex')
assert failed['retryable'] and failed['queue_error'] == 'queue failed'
assert issues.get_issue(failed['ref'])['type_ids'] == [2]
issues.update_project(me, 'RETRY', {'description': '새 설명'})
with patch.object(jobs, 'busy', return_value=True):
    retry = project_docs.request_documents(me, 'RETRY', 'codex')
assert retry['ref'] == failed['ref'] and retry['reused'] and 'job' in retry
assert '원래 설명' in issues.get_issue(retry['ref'])['body']
with db.connect() as c:
    assert c.execute('SELECT description FROM project_doc_requests WHERE issue_id=?',
                     (issues.get_issue(retry['ref'])['id'],)).fetchone()[0] == '원래 설명'

# 동시에 누른 요청도 하나만 발행한다.
issues.create_project(me, 'RACE', '동시', '', '/race', '설명')
with patch.object(jobs, 'enqueue', return_value={'queued': True}):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: project_docs.request_documents(me, 'RACE', 'codex'), range(8)))
assert len({r['ref'] for r in results}) == 1
with db.connect() as c:
    assert c.execute('SELECT COUNT(*) FROM issue_type_links WHERE issue_id=? AND type_id=2',
                     (issues.get_issue(results[0]['ref'])['id'],)).fetchone()[0] == 1
issues.set_status(me, results[0]['ref'], 'done')
with patch.object(jobs, 'enqueue', return_value={'queued': True}):
    assert project_docs.request_documents(me, 'RACE', 'codex')['ref'] != results[0]['ref']

# 일반 종류 선택은 공식 요청이나 유료 작업을 시작하지 않는다.
with db.connect() as c:
    counts = tuple(c.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                   for table in ('project_doc_requests', 'jobs', 'runs'))
ordinary = issues.create_issue(me, 'DOC', '일반 문서', type_ids=[2])
issues.update_issue(me, ordinary['ref'], {'type_ids': [1, 2]})
assert project_docs.provider_for(ordinary['id']) is None
with db.connect() as c:
    assert counts == tuple(c.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                           for table in ('project_doc_requests', 'jobs', 'runs'))

# 기존 DB 이관은 이름·활성 여부와 무관하며 종류·요청 스냅샷을 보존한다.
import sqlite3
with patch.object(db.config, 'DB_PATH', Path(tempfile.mkdtemp()) / 'legacy.db'):
    with sqlite3.connect(db.config.DB_PATH) as c:
        # 문서 요청 이관 직전 스키마를 구성한다. 이후 추가된 마이그레이션과 독립적이다.
        legacy_version = next(i for i, sql in enumerate(db.MIGRATIONS)
                              if 'INSERT OR IGNORE INTO issue_type_links' in sql
                              and 'FROM project_doc_requests' in sql)
        for version, sql in enumerate(db.MIGRATIONS[:legacy_version], 1):
            c.executescript(sql + f'PRAGMA user_version={version};')
    issues.create_project(me, 'OLD', '기존', '', '/old', '현재 설명')
    old = issues.create_issue(me, 'OLD', '기존 요청', type_ids=[1])
    unrelated = issues.create_issue(me, 'OLD', '일반 이슈')
    existing = issues.create_issue(me, 'OLD', '이미 연결', type_ids=[2])
    issues.update_issue_type(me, 2, {'name': '공식 자료', 'active': False})
    with db.connect() as c:
        for iid, provider in ((old['id'], 'codex'), (existing['id'], 'claude')):
            c.execute('INSERT INTO project_doc_requests VALUES(?,?,?,?,?)',
                      (iid, old['project_id'], provider, '과거 설명', old['created_at']))
        before = tuple(c.execute('SELECT * FROM issue_type_links WHERE issue_id=?', (existing['id'],)).fetchone())
    assert db.init() == len(db.MIGRATIONS)
    assert db.init() == len(db.MIGRATIONS)
    assert issues.get_issue(old['ref'])['type_ids'] == [1, 2]
    assert issues.get_issue(unrelated['ref'])['type_ids'] == []
    with db.connect() as c:
        assert tuple(c.execute('SELECT * FROM issue_type_links WHERE issue_id=?', (existing['id'],)).fetchone()) == before
        assert [tuple(r) for r in c.execute('SELECT provider,description FROM project_doc_requests ORDER BY issue_id')] == [('codex', '과거 설명'), ('claude', '과거 설명')]
    with patch.object(jobs, 'enqueue', return_value={'queued': True}):
        assert project_docs.request_documents(me, 'OLD', 'codex')['ref'] == old['ref']
        new = project_docs.request_documents(me, 'OLD', 'claude')
        assert new['ref'] == existing['ref']
        issues.set_status(me, existing['ref'], 'done')
        new = project_docs.request_documents(me, 'OLD', 'claude')
        assert issues.get_issue(new['ref'])['type_ids'] == [2]

# REST는 인증된 사람의 요청을 연결하고, 도구를 생략한 후속 실행은 저장된 도구를 따른다.
import httpx
from fastapi.testclient import TestClient
import app, auth
auth._client = httpx.AsyncClient(transport=httpx.MockTransport(
    lambda req: httpx.Response(200, json={'user': {'id': 1, 'username': 'admin', 'role': 'admin'}})))
with TestClient(app.app) as client, patch.object(jobs, 'busy', return_value=True):
    client.cookies.set('ns_session', 'adm')
    headers = {'X-Requested-With': 'dev'}
    response = client.post('/api/projects/RETRY/documents/request', json={'provider': 'codex'}, headers=headers)
    assert response.status_code == 200 and response.json()['ref'] == retry['ref'], response.text
    denied_request = client.post('/api/projects/EMPTY/documents/request', json={'provider': 'claude'}, headers=headers)
    assert denied_request.status_code == 400
    parent = issues.get_issue(retry['ref'])
    issues.post_plan(me, parent['ref'], '승인된 계획')
    issues.decide(me, parent['ref'], 'approve', plan_version=1)
    task = issues.create_issue(me, 'RETRY', '후속 Task', parent=parent['ref'])
    response = client.post(f"/api/issues/{task['ref']}/execute", headers=headers)
    assert response.status_code == 200, response.text
    assert next(j for j in jobs.list_jobs() if j['ref'] == task['ref'])['provider'] == 'codex'
    response = client.post(f"/api/issues/{task['ref']}/execute", json={'provider': 'claude'}, headers=headers)
    assert response.status_code == 409
print('OK')

# 기준 브랜치 원본 조회와 경로·링크·크기 제한을 검사한다.
import subprocess
repo = Path(tempfile.mkdtemp())
fixture_env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_CONFIG_')}
fixture_env['GIT_CONFIG_NOSYSTEM'] = '1'
fixture_env['GIT_CONFIG_GLOBAL'] = os.devnull
def git(*args):
    r = subprocess.run(['git', *args], cwd=repo, env=fixture_env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()
git('init', '-b', 'main')
git('config', 'user.name', 'test')
git('config', 'user.email', 'test@example.test')
folder = repo / 'docs/project'
folder.mkdir(parents=True)
path = project_docs.DOCUMENTS[0][0]
(repo / path).write_text('기준 원문 <script>alert(1)</script>', encoding='utf-8')
(repo / project_docs.DOCUMENTS[1][0]).write_bytes(b'x' * (project_docs.MAX_DOCUMENT_BYTES + 1))
git('add', '.')
git('commit', '-m', 'fixture')
for name in list(os.environ):
    if name.startswith('GIT_CONFIG_'):
        del os.environ[name]
os.environ.update({k: v for k, v in fixture_env.items() if k.startswith('GIT_CONFIG_')})
issues.create_project(me, 'READ', '조회', '', str(repo), '조회 설명')
(repo / path).write_text('미병합 변경', encoding='utf-8')
assert project_docs.read_document('read', path)['content'] == '기준 원문 <script>alert(1)</script>'
assert project_docs.read_document('READ', 'AGENTS.md')['status'] == 'missing'
denied(lambda: project_docs.read_document('UNKNOWN', path), 404)
for invalid in ('../CLAUDE.md', '/etc/passwd', 'docs/project/../../CLAUDE.md', 'docs\\project\\00_PRODUCT_BRIEF.md'):
    denied(lambda: project_docs.read_document('READ', invalid), 400)
denied(lambda: project_docs.read_document('READ', project_docs.DOCUMENTS[1][0]), 413)
# Git 링크 mode를 사용하여 Windows 링크 생성 권한에 의존하지 않는다.
oid = subprocess.run(['git', 'hash-object', '-w', '--stdin'], cwd=repo, env=fixture_env, input=b'../../outside', capture_output=True).stdout.decode().strip()
git('update-index', '--add', '--cacheinfo', '120000,' + oid + ',AGENTS.md')
git('commit', '-m', 'symlink fixture')
denied(lambda: project_docs.read_document('READ', 'AGENTS.md'), 400)
listing = project_docs.list_documents('READ')
assert len(listing['documents']) == 7
assert listing['documents'][0]['status'] == 'available'
assert listing['documents'][1]['status'] == 'unavailable'
with patch.object(jobs, 'busy', return_value=True):
    request = project_docs.request_documents(me, 'READ', 'codex')
assert project_docs.list_documents('READ')['requests'][0]['state'] == 'in_progress'
issues.set_status(me, request['ref'], 'in_review')
assert project_docs.list_documents('READ')['requests'][0]['state'] == 'merge_pending'
issues.set_status(me, request['ref'], 'done')
assert project_docs.list_documents('READ')['requests'][0]['state'] == 'merged'
assert project_docs.list_documents('RETRY')['requests'][0]['url'].startswith('#/issue/')
# 대기열 등록 자체가 실패한 요청도 진행 중으로 꾸미지 않는다.
issues.create_project(me, 'FAIL', '실패', '', str(repo), '설명')
with patch.object(jobs, 'enqueue', side_effect=RuntimeError('queue failed')):
    project_docs.request_documents(me, 'FAIL', 'claude')
assert project_docs.list_documents('FAIL')['requests'][0]['state'] == 'failed'
# 상위 경로 링크도 저장소 밖을 따라가지 않는다.
git('update-index', '--force-remove', project_docs.DOCUMENTS[0][0])
git('update-index', '--force-remove', project_docs.DOCUMENTS[1][0])
git('update-index', '--add', '--cacheinfo', '120000,' + oid + ',docs/project')
git('commit', '-m', 'parent symlink fixture')
denied(lambda: project_docs.read_document('READ', path), 400)
git('update-index', '--force-remove', 'docs/project')
git('add', 'docs/project')
git('commit', '-m', 'restore fixture')
with TestClient(app.app) as client:
    client.cookies.set('ns_session', 'adm')
    assert client.get('/api/projects/READ/documents').status_code == 200
    response = client.get('/api/projects/READ/documents/content', params={'path': path})
    assert response.json()['content'] == '미병합 변경'
    assert client.get('/api/projects/READ/documents/content', params={'path': '../secret'}).status_code == 400
    assert client.get('/api/projects/UNKNOWN/documents').status_code == 404
import review
for command in (review.command_for('DOC-1'), review.codex_command_for('DOC-1'),
                execute.command_for('DOC-1-1', 'DOC-1', 'mock'), execute.codex_command_for('DOC-1-1', 'DOC-1')):
    text = ' '.join(command)
    assert 'description' in text and all(path in text for path, _ in project_docs.DOCUMENTS)
    assert '확대하지 않는다' in text
for command in (execute.command_for('DOC-1-1', 'DOC-1', 'mock'), execute.codex_command_for('DOC-1-1', 'DOC-1')):
    assert '현재 Task worktree의 상대 경로에서만' in ' '.join(command)
print('Document read and context OK')
