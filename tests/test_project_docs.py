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
        assert '사용자 제품 설명' in parent['body']
        assert all(path in parent['body'] and purpose in parent['body'] for path, purpose in project_docs.DOCUMENTS)
        assert jobs.list_jobs()[-1]['provider'] == provider
        assert project_docs.request_documents(me, 'DOC', provider)['ref'] == ref
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
issues.set_status(me, results[0]['ref'], 'done')
with patch.object(jobs, 'enqueue', return_value={'queued': True}):
    assert project_docs.request_documents(me, 'RACE', 'codex')['ref'] != results[0]['ref']

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
