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
# Issue 조회는 시간·점검 사용량 없이 같은 직접 실행 토큰을 제공한다.
for row in r['issues'] + [p]:
    direct = issues.get_issue(row['ref'])['token_usage']
    assert direct == {k: row[k] for k in direct}, (direct, row)
assert issues.get_issue(parent['ref'])['children'][0]['token_usage']['total_tokens'] == 15
empty = issues.create_issue(human, 'US', 'no runs')
missing = issues.create_issue(human, 'US', 'unmeasured')
with db.connect() as c:
    c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at,input_tokens,output_tokens) VALUES(?,'qa','failed','human:admin',?,'broken',NULL)",
              (missing['id'], start))
    statements = []
    c.set_trace_callback(statements.append)
    direct = usage.issue_token_usage(c, [empty['id'], missing['id'], task['id']])
    assert sum('FROM runs' in sql for sql in statements) == 1
    assert usage.issue_token_usage(c, []) == {}
    # 목록 상한이 SQLite 변수 제한을 넘더라도 묶음 수에 비례한다.
    assert len(usage.issue_token_usage(c, range(1, 2002))) == 2001
assert direct[empty['id']] == dict(input_tokens=0, output_tokens=0, total_tokens=0,
                                  run_count=0, unmeasured_run_count=0, running_run_count=0, partial=False)
assert direct[missing['id']]['run_count'] == direct[missing['id']]['unmeasured_run_count'] == 1
assert direct[missing['id']]['total_tokens'] == 0 and direct[missing['id']]['partial']
assert direct[task['id']]['running_run_count'] == 1
# 프로젝트 점검은 프로젝트 합계에만 들어가고 Issue 직접 실행분은 보존한다.
with db.connect() as c:
    c.execute("""INSERT INTO project_audits(project_id,request_id,input_fingerprint,provider,actor,status,
        usage_json,created_at,updated_at,started_at,ended_at) VALUES(?,'usage-audit','test','codex','human:admin',
        'complete','{"input_tokens":50,"output_tokens":6,"cost_usd":0}',?,?,?,?)""",
        (parent['project_id'], start, end, start, end))
after_audit = usage.project_usage('US', as_of=end)
assert after_audit['summary']['total_tokens'] == 71
assert after_audit['audit_summary']['total_tokens'] == 56
assert issues.get_issue(task['ref'])['token_usage'] == direct[task['id']]
assert issues.get_issue(parent['ref'])['token_usage']['total_tokens'] == 0
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
assert issues.get_issue(task['ref'])['token_usage']['total_tokens'] == 58
print('OK usage')
