"""임시 DB와 모의 인증으로 직접 실행 집계·페이지를 검사한다."""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))
import app, auth, db, issues, review, usage
import httpx
from fastapi.testclient import TestClient

db.init()
human = {'kind': 'human', 'name': 'admin'}
issues.create_project(human, 'US', 'usage')
issues.create_project(human, 'OTHER', 'other')
parent = issues.create_issue(human, 'US', 'parent')
task = issues.create_issue(human, 'US', 'task', parent=parent['ref'])
other = issues.create_issue(human, 'OTHER', 'excluded')
start, end = '2026-01-01T00:00:00+00:00', '2026-01-01T00:01:00+00:00'
with db.connect() as c:
    c.execute("UPDATE issues SET first_started_at=?,last_done_at=?,status='done' WHERE id=?", (start, end, parent['id']))
    c.execute("UPDATE issues SET first_started_at=?,last_done_at=?,status='in_progress' WHERE id=?", (start, end, task['id']))
    for iid, status, inp, out, stop in [
        (parent['id'], 'ok', 0, 0, end), (task['id'], 'failed', 10, 2, end),
        (task['id'], 'timeout', None, 3, end), (task['id'], 'orphaned', -1, 1.5, end),
        (task['id'], 'running', None, None, None), (other['id'], 'ok', 999, 999, end)]:
        c.execute('INSERT INTO runs(issue_id,mode,status,actor,started_at,ended_at,input_tokens,output_tokens) VALUES(?,?,?,?,?,?,?,?)',
                  (iid, 'review', status, 'human:admin', start, stop, inp, out))
r = usage.project_usage('us', 1, as_of=end)
s = r['summary']
assert (s['input_tokens'], s['output_tokens'], s['total_tokens']) == (10, 5, 15), s
assert (s['run_count'], s['unmeasured_run_count'], s['running_run_count'], s['agent_seconds']) == (5, 3, 1, 300)
assert s['completed_elapsed_seconds'] == 60 and s['partial']
assert r['issues'][0]['ref'] == task['ref'] and r['issues'][0]['agent_seconds'] == 240
assert r['issues'][0]['ongoing_elapsed_seconds'] == 60 and r['has_more']
p = usage.project_usage('US', 1, 1, as_of=end)['issues'][0]
assert p['run_count'] == 1 and p['total_tokens'] == 0 and not p['partial']
assert usage.project_usage('US', 1, 99)['issues'] == []
assert usage.project_usage('OTHER')['summary']['total_tokens'] == 1998
assert 'log_file' not in str(r) and 'local_path' not in str(r)
issues.create_project(human, 'EMPTY', 'empty')
assert usage.project_usage('EMPTY')['summary']['run_count'] == 0
assert usage._seconds(None, end) is None
assert usage._seconds(end, start) is None
assert usage._seconds('broken', end) is None
# 재개 상태의 과거 완료 기록은 현재 완료 시간 합계에 넣지 않는다.
assert s['completed_issue_count'] == 1 and r['issues'][0]['elapsed_seconds'] == 60
auth._client = httpx.AsyncClient(transport=httpx.MockTransport(
    lambda req: httpx.Response(200, json={'user': {'role': 'admin', 'username': 'admin'}})))
with TestClient(app.app) as client:
    assert client.get('/api/projects/US/usage').status_code == 401
    client.cookies.set('ns_session', 'temporary')
    assert client.get('/api/projects/US/usage').status_code == 200
    assert client.get('/api/projects/MISSING/usage').status_code == 404
    for query in ('limit=0', 'limit=201', 'offset=-1'):
        assert client.get('/api/projects/US/usage?' + query).status_code == 400
    assert client.get('/api/projects/US/usage?limit=abc').status_code == 422
    key = auth.create_agent('usage-test')[1]
    client.cookies.clear()
    assert client.get('/api/projects/US/usage', headers={'Authorization': 'Bearer ' + key}).status_code == 200
    assert client.get('/api/projects/US/usage', headers={'Authorization': 'Bearer invalid'}).status_code == 401
    with patch.object(auth, 'nightshift_user', return_value={'role': 'member'}):
        assert client.get('/api/projects/US/usage').status_code == 403
# 실제 실행 루프의 재시도까지 턴 합계를 한 번씩만 저장한다.
import json
import orchestrate
outputs = ['\n'.join(json.dumps({'type': 'turn.completed', 'usage': u}) for u in turns)
           for turns in ([{'input_tokens': 10, 'output_tokens': 2}, {'input_tokens': 20, 'output_tokens': 3}],
                         [{'input_tokens': 7, 'output_tokens': 1}])]
class FakeProcess:
    returncode = 0
    def communicate(self, timeout=None):
        return outputs.pop(0), ''
log, rid = review.begin(human, issues.get_issue(task['ref']), 'execute', 'codex')
with patch.object(review.subprocess, 'Popen', side_effect=lambda *a, **kw: FakeProcess()), \
     patch.object(orchestrate, 'run_tests', side_effect=['mock failure', None]), \
     patch('execute.finalize_codex', return_value='ok'):
    assert review.run_headless(human, task['ref'], log, rid, ['codex', 'prompt'], None, None, 10, '실행', 'codex') == 'ok'
saved = review.list_runs(task['ref'])[0]
assert (saved['input_tokens'], saved['output_tokens']) == (37, 6), saved
print('OK usage')
