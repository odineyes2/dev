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
    def communicate(self, input=None, timeout=None):
        assert input.startswith('prompt')
        return outputs.pop(0), ''
log, rid = review.begin(human, issues.get_issue(task['ref']), 'execute', 'codex')
with patch.object(review.subprocess, 'Popen', side_effect=lambda *a, **kw: FakeProcess()), \
     patch.object(orchestrate, 'run_tests', side_effect=['mock failure', None]), \
     patch('execute.finalize_codex', return_value='ok'):
    assert review.run_headless(human, task['ref'], log, rid, ['codex', 'prompt'], None, None, 10, '실행', 'codex') == 'ok'
saved = review.list_runs(task['ref'])[0]
assert (saved['input_tokens'], saved['output_tokens']) == (37, 6), saved
assert issues.get_issue(task['ref'])['token_usage']['total_tokens'] == 58

# 2026-02-01 00:30 KST: 하루·월·연 경계와 Task, 재개, 병렬 실행을 고정 시각으로 확인한다.
issues.create_project(human, 'SUM', 'summary')
summary_parent = issues.create_issue(human, 'SUM', 'summary parent')
summary_task = issues.create_issue(human, 'SUM', 'summary task', parent=summary_parent['ref'])
reopened = issues.create_issue(human, 'SUM', 'reopened')
prior = '2026-01-31T14:00:00+00:00'
today = '2026-01-31T15:10:00+00:00'
fixed_now = '2026-01-31T15:30:00+00:00'
with db.connect() as c:
    c.execute("UPDATE issues SET created_at=?,last_done_at=?,status='done' WHERE id=?",
              (prior, today, summary_parent['id']))
    c.execute("UPDATE issues SET created_at=?,last_done_at=?,status='done' WHERE id=?",
              (today, today, summary_task['id']))
    c.execute("UPDATE issues SET created_at=?,last_done_at=?,status='in_progress' WHERE id=?",
              (prior, today, reopened['id']))
    for iid, begin, stop, status, inp, out in [
        (summary_parent['id'], prior, today, 'ok', 3, 1),
        (summary_task['id'], today, fixed_now, 'ok', 5, 2),
        (summary_task['id'], today, fixed_now, 'ok', 7, 3),
        (reopened['id'], today, None, 'running', None, None),
        (reopened['id'], today, None, 'failed', 1, None)]:
        c.execute('INSERT INTO runs(issue_id,mode,status,actor,started_at,ended_at,input_tokens,output_tokens) VALUES(?,?,?,?,?,?,?,?)',
                  (iid, 'review', status, 'human:admin', begin, stop, inp, out))
    c.execute("""INSERT INTO project_audits(project_id,request_id,input_fingerprint,provider,actor,status,
        usage_json,created_at,updated_at,started_at,ended_at) VALUES(?,'summary-audit','test','codex','human:admin',
        'complete','{"input_tokens":11,"output_tokens":4}',?,?,?,?)""",
        (summary_parent['project_id'], today, today, today, fixed_now,))
result = usage.issue_summary('today', 'sum', as_of=fixed_now)
totals = result['summary']
assert result['since'] == '2026-01-31T15:00:00+00:00' and result['project'] == 'SUM'
assert (totals['created_issue_count'], totals['completed_issue_count']) == (1, 2), totals
assert (totals['input_tokens'], totals['output_tokens'], totals['total_tokens']) == (24, 9, 33), totals
assert (totals['run_count'], totals['running_run_count'], totals['unmeasured_run_count'],
        totals['unmeasured_duration_run_count'], totals['agent_seconds']) == (6, 1, 2, 1, 5400), totals
assert totals['partial']
assert usage.issue_summary('month', 'SUM', as_of=fixed_now)['summary']['created_issue_count'] == 1
assert usage.issue_summary('year', 'SUM', as_of=fixed_now)['summary']['created_issue_count'] == 3
assert usage.issue_summary('all', 'SUM', as_of=fixed_now)['summary']['created_issue_count'] == 3
assert usage.issue_summary('today', 'EMPTY', as_of=fixed_now)['summary']['total_tokens'] == 0
assert usage.issue_summary('all', as_of=fixed_now)['summary']['created_issue_count'] >= 3

# 서울 시간의 월요일·월초·연초 경계와 과거 구간의 배타적 끝을 확인한다.
monday = '2026-03-01T16:00:00+00:00'  # 3월 2일 월요일 01:00 KST
for period, since, until in [
    ('yesterday', '2026-02-28T15:00:00+00:00', '2026-03-01T15:00:00+00:00'),
    ('week', '2026-03-01T15:00:00+00:00', monday),
    ('last_week', '2026-02-22T15:00:00+00:00', '2026-03-01T15:00:00+00:00'),
    ('last_month', '2026-01-31T15:00:00+00:00', '2026-02-28T15:00:00+00:00'),
]:
    window = usage.issue_summary(period, 'EMPTY', as_of=monday)
    assert (window['since'], window['until']) == (since, until), window
assert usage.issue_summary('last_month', 'EMPTY', as_of='2026-01-15T00:00:00+00:00')['since'] == '2025-11-30T15:00:00+00:00'
assert usage.issue_summary('today', 'EMPTY', as_of=monday)['until'] == monday

issues.create_project(human, 'BOUND', 'boundary')
before = issues.create_issue(human, 'BOUND', 'before')
at_end = issues.create_issue(human, 'BOUND', 'at end')
crossing = issues.create_issue(human, 'BOUND', 'crossing')
with db.connect() as c:
    c.execute("UPDATE issues SET created_at=?,last_done_at=?,status='done' WHERE id=?",
              ('2026-02-28T15:00:00+00:00', '2026-03-01T15:00:00+00:00', before['id']))
    c.execute("UPDATE issues SET created_at=?,last_done_at=?,status='done' WHERE id=?",
              ('2026-03-01T15:00:00+00:00', '2026-03-01T15:00:00+00:00', at_end['id']))
    c.execute("UPDATE issues SET created_at=?,last_done_at=?,status='done' WHERE id=?",
              ('2026-02-28T15:30:00+00:00', '2026-02-28T16:00:00+00:00', crossing['id']))
    for started, ended, status, tokens in [
        ('2026-02-28T14:00:00+00:00', '2026-03-01T16:00:00+00:00', 'ok', 3),
        ('2026-02-28T16:00:00+00:00', '2026-03-01T16:00:00+00:00', 'ok', 5),
        ('2026-02-28T17:00:00+00:00', None, 'running', 7),
        ('2026-03-01T15:00:00+00:00', None, 'running', 11),
    ]:
        c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at,ended_at,input_tokens,output_tokens) "
                  "VALUES(?,'review',?,'human:admin',?,?,?,0)",
                  (crossing['id'], status, started, ended, tokens))
    for request_id, started in [('audit-inside', '2026-02-28T16:00:00+00:00'),
                                ('audit-at-end', '2026-03-01T15:00:00+00:00')]:
        c.execute("""INSERT INTO project_audits(project_id,request_id,input_fingerprint,provider,actor,status,
                  usage_json,created_at,updated_at,started_at,ended_at)
                  VALUES(?,?,'test','codex','human:admin','complete','{"input_tokens":2,"output_tokens":0}',?,?,?,?)""",
                  (crossing['project_id'], request_id, started, started, started, monday))
past = usage.issue_summary('yesterday', 'BOUND', as_of=monday)['summary']
assert (past['created_issue_count'], past['completed_issue_count']) == (2, 1), past
assert (past['run_count'], past['running_run_count'], past['input_tokens']) == (4, 0, 14), past
assert past['agent_seconds'] == 86400 + 82800 + 79200 + 82800, past

# TestClient 시작 시 db.init()는 합성 running 실행을 orphaned로 바꾸므로 보존한다.
with patch.object(db, 'init'), patch.object(usage, '_now', return_value=usage._time(fixed_now)), TestClient(app.app) as client:
    assert client.get('/api/issue-summary?period=today&project=SUM').status_code == 401
    client.cookies.set('ns_session', 'temporary')
    for period in ('today', 'yesterday', 'week', 'last_week', 'month', 'last_month', 'year', 'all'):
        response = client.get(f'/api/issue-summary?period={period}&project=SUM')
        assert response.status_code == 200, response.text
        assert response.json()['period'] == period
    assert client.get('/api/issue-summary?period=bad').status_code == 400
    assert client.get('/api/issue-summary?project=MISSING').status_code == 404
    client.cookies.clear()
    with patch.object(auth, 'nightshift_user', return_value={'role': 'member'}):
        assert client.get('/api/issue-summary?period=today').status_code == 403
# API 환산 금액: cost_usd 우선·캐시 구분·예전 합산(어림)·모르는 모델(환산 못 한 토큰).
issues.create_project(human, 'PRC', 'pricing')
priced = issues.create_issue(human, 'PRC', 'priced')
with db.connect() as c:
    for model, inp, out, write, read, cost in [
        (None, 100, 50, None, None, 1.5),
        ('claude-sonnet-4-6', 1_000_000, 100_000, 200_000, 300_000, None),
        ('claude-sonnet-4-6', 1_000_000, 0, None, None, None),
        ('mystery-model', 7, 3, None, None, None),
    ]:
        c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at,ended_at,input_tokens,output_tokens,"
                  "model,cache_write_tokens,cache_read_tokens,cost_usd) "
                  "VALUES(?,'execute','ok','human:admin','2026-01-10T03:00:00+00:00','2026-01-10T03:10:00+00:00',?,?,?,?,?,?)",
                  (priced['id'], inp, out, model, write, read, cost))
with patch.object(db, 'init'), patch.object(usage, '_now', return_value=usage._time(fixed_now)), TestClient(app.app) as client:
    client.cookies.set('ns_session', 'temporary')
    api = client.get('/api/trends?from=2026-01-10&to=2026-01-10&project=prc')
    assert api.status_code == 200, api.text
    s = api.json()['summary']
    assert abs(s['estimated_api_cost_usd'] - 8.34) < 1e-9, s
    assert (s['cost_estimated_run_count'], s['cost_approx_run_count'], s['unpriced_tokens']) == (3, 1, 10), s
    empty_cost = client.get('/api/trends?from=2026-01-10&to=2026-01-10&project=EMPTY').json()['summary']
    assert (empty_cost['estimated_api_cost_usd'], empty_cost['cost_estimated_run_count'],
            empty_cost['unpriced_tokens']) == (0, 0, 0), empty_cost
# 새 API는 실제 라우트에서 서울 날짜 버킷, 권한, 필터와 직접 실행 순위를 확인한다.
with patch.object(db, 'init'), patch.object(usage, '_now', return_value=usage._time(fixed_now)), TestClient(app.app) as client:
    assert client.get('/api/trends').status_code == 401
    client.cookies.set('ns_session', 'temporary')
    response = client.get('/api/trends?from=2026-01-31&to=2026-02-01&project=sum')
    assert response.status_code == 200, response.text
    trend = response.json()
    assert (trend['from_date'], trend['to_date'], trend['project']) == ('2026-01-31', '2026-02-01', 'SUM')
    jan, feb = trend['days']
    assert (jan['created_issue_count'], jan['completed_issue_count'], jan['agent_seconds'], jan['total_tokens']) == (2, 0, 3600, 4), jan
    assert (feb['created_issue_count'], feb['completed_issue_count'], feb['agent_seconds'], feb['total_tokens']) == (1, 2, 5400, 33), feb
    assert (feb['run_count'], feb['running_run_count'], feb['unmeasured_run_count'],
            feb['unmeasured_duration_run_count']) == (5, 1, 2, 1), feb
    assert feb['partial'] and trend['summary']['partial']
    assert trend['summary']['total_tokens'] == 37
    assert [(r['ref'], r['total_tokens']) for r in trend['top_issues']] == [
        (summary_task['ref'], 17), (summary_parent['ref'], 4), (reopened['ref'], 1)]
    assert len({r['ref'] for r in trend['top_issues']}) == len(trend['top_issues'])
    assert sum(r['total_tokens'] for r in trend['top_issues']) == 22  # 점검 15는 순위에 넣지 않는다.
    assert client.get('/api/trends?from=2026-02-01&to=2026-02-01&project=SUM').json()['days'][0]['agent_seconds'] == 5400
    assert client.get('/api/trends?from=2026-01-31&to=2026-02-01').json()['summary']['total_tokens'] == 37
    assert len(client.get('/api/trends?project=EMPTY').json()['days']) == 30
    assert client.get('/api/trends?from=2026-02-03&to=2026-02-03&project=SUM').json()['summary']['total_tokens'] == 0
    for query in ('from=bad', 'to=2026-02-30', 'from=2026-02-02&to=2026-02-01',
                  'from=2025-01-01&to=2026-02-01'):
        assert client.get('/api/trends?' + query).status_code == 400, query
    assert client.get('/api/trends?project=MISSING').status_code == 404
    client.cookies.clear()
    key = auth.create_agent('trend-test')[1]
    assert client.get('/api/trends', headers={'Authorization': 'Bearer ' + key}).status_code == 200
    assert client.get('/api/trends', headers={'Authorization': 'Bearer invalid'}).status_code == 401
    with patch.object(auth, 'nightshift_user', return_value={'role': 'member'}):
        assert client.get('/api/trends', cookies={'ns_session': 'temporary'}).status_code == 403
print('OK usage')

# 하위 Task만 더하고 실행 중 여부로 합계 표시 가능 시점을 판단한다.
aggregate_parent = issues.create_issue(human, 'US', 'aggregate parent')
measured = issues.create_issue(human, 'US', 'measured', parent=aggregate_parent['ref'])
unmeasured = issues.create_issue(human, 'US', 'unmeasured task', parent=aggregate_parent['ref'])
issues.create_issue(human, 'US', 'no run task', parent=aggregate_parent['ref'])
superseded = issues.create_issue(human, 'US', 'superseded task', parent=aggregate_parent['ref'])
with db.connect() as c:
    for item, status, inp, out in [(measured, 'ok', 11, 4),
                                   (unmeasured, 'running', None, 2),
                                   (superseded, 'ok', 999, 999)]:
        c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at,input_tokens,output_tokens) "
                  "VALUES(?,'review',?,'human:admin',?,?,?)", (item['id'], status, start, inp, out))
    issues._event(c, superseded['id'], human, 'comment',
                  data={'superseded_by_plan': 2})
    totals = usage.task_token_usage(c, [aggregate_parent['id'], empty['id']])
    assert usage.task_token_usage(c, []) == {}
    assert len(usage.task_token_usage(c, range(1, 2002))) == 2001
assert totals[empty['id']] is None
assert totals[aggregate_parent['id']] == dict(input_tokens=11, output_tokens=6, total_tokens=17,
                                              run_count=2, unmeasured_run_count=1,
                                              running_run_count=1, partial=True, task_count=3)
with db.connect() as c:
    c.execute("UPDATE runs SET status='timeout' WHERE issue_id=?", (unmeasured['id'],))
    settled = usage.task_token_usage(c, [aggregate_parent['id']])[aggregate_parent['id']]
assert settled['running_run_count'] == 0 and settled['partial']
assert settled['unmeasured_run_count'] == 1 and settled['task_count'] == 3
assert issues.get_issue(aggregate_parent['ref'])['token_usage']['total_tokens'] == 0
print('OK task token usage')

# 완료된 부모 단위 단계 집계와 이전 기간, 빈 대기열, 부분 측정을 고정 시각으로 확인한다.
issues.create_project(human, 'STG', 'stages')
stage_parent = issues.create_issue(human, 'STG', 'stage parent')
stage_task = issues.create_issue(human, 'STG', 'stage task', parent=stage_parent['ref'])
older_parent = issues.create_issue(human, 'STG', 'older stage parent')
older_task = issues.create_issue(human, 'STG', 'older stage task', parent=older_parent['ref'])
unfinished_parent = issues.create_issue(human, 'STG', 'unfinished stage parent')
unfinished_task = issues.create_issue(human, 'STG', 'unfinished stage task', parent=unfinished_parent['ref'])
partial_parent = issues.create_issue(human, 'STG', 'partial stage parent')
partial_task = issues.create_issue(human, 'STG', 'partial stage task', parent=partial_parent['ref'])
issues.create_project(human, 'DEV', 'dev changes')
dev_parent = issues.create_issue(human, 'DEV', 'dev parent')
dev_task = issues.create_issue(human, 'DEV', 'dev task', parent=dev_parent['ref'])

def stage_run(c, iid, mode, begin, stop, inp, out, status='ok'):
    return c.execute('''INSERT INTO runs(issue_id,mode,status,actor,started_at,ended_at,
        input_tokens,output_tokens,provider,model) VALUES(?,?,?,?,?,?,?,?,?,?)''',
        (iid, mode, status, 'human:admin', begin, stop, inp, out, 'codex', 'test-model')).lastrowid

def stage_completion(c, iid, ref, run_id, moment):
    c.execute('''INSERT INTO execution_completion(run_id,issue_id,ref,actor,task_sha,
        auto_merge,phase,merge_sha,owner_event,cfg_json,updated_at)
        VALUES(?,?,?,'human:admin','test-sha',1,'complete','merged-sha',1,'{}',?)''',
        (run_id, iid, ref, moment))

with db.connect() as c:
    # 첫 작업은 기간 밖에 시작했고 마지막 병합만 기간 안이다.
    review_id = stage_run(c, stage_parent['id'], 'review',
                          '2026-01-30T23:00:00+00:00', '2026-01-30T23:01:00+00:00', 10, 2)
    first = stage_run(c, stage_task['id'], 'execute',
                      '2026-01-31T14:55:00+00:00', '2026-01-31T14:56:00+00:00', 20, 3, 'failed')
    last = stage_run(c, stage_task['id'], 'execute',
                     '2026-01-31T15:00:00+00:00', '2026-01-31T15:02:00+00:00', 30, 4)
    stage_completion(c, stage_task['id'], stage_task['ref'], last, '2026-01-31T15:05:00+00:00')
    c.execute("INSERT INTO jobs(issue_id,mode,actor,status,created_at,started_at,run_id) VALUES(?,'execute','human:admin','started',?,?,?)",
              (stage_task['id'], '2026-01-31T14:58:00+00:00', '2026-01-31T15:00:00+00:00', last))
    # 겹친 두 실행은 대기열 점유 시간을 한 번만 뺀다.
    stage_run(c, other['id'], 'review', '2026-01-31T14:58:30+00:00',
              '2026-01-31T14:59:15+00:00', 1, 1)
    stage_run(c, other['id'], 'review', '2026-01-31T14:59:00+00:00',
              '2026-01-31T14:59:30+00:00', 1, 1)
    c.execute("INSERT INTO events(issue_id,actor,kind,data_json,created_at) VALUES(?,'human:admin','status',?,?)",
              (stage_parent['id'], '{"from":"in_progress","to":"triage"}', '2026-01-30T23:01:00+00:00'))
    c.execute("INSERT INTO decisions(issue_id,gate,plan_version,verdict,actor,created_at) VALUES(?,'plan',1,'approve','human:admin',?)",
              (stage_parent['id'], '2026-01-30T23:03:00+00:00'))
    c.execute("INSERT INTO events(issue_id,actor,kind,data_json,created_at) VALUES(?,'human:admin','status',?,?)",
              (stage_parent['id'], '{"from":"in_review","to":"done"}', '2026-01-31T15:10:00+00:00'))
    previous_run = stage_run(c, older_task['id'], 'execute',
                             '2026-01-30T16:00:00+00:00', '2026-01-30T16:01:00+00:00', 5, 1)
    stage_completion(c, older_task['id'], older_task['ref'], previous_run, '2026-01-30T16:02:00+00:00')
    stage_run(c, unfinished_task['id'], 'execute',
              '2026-01-31T15:00:00+00:00', '2026-01-31T15:01:00+00:00', 5, 1)
    partial_run = stage_run(c, partial_task['id'], 'execute',
                            '2026-01-31T15:06:00+00:00', '2026-01-31T15:07:00+00:00', None, 2)
    stage_completion(c, partial_task['id'], partial_task['ref'], partial_run, '2026-01-31T15:08:00+00:00')
    dev_run = stage_run(c, dev_task['id'], 'execute',
                        '2026-01-31T14:59:00+00:00', '2026-01-31T15:03:00+00:00', 2, 1)
    stage_completion(c, dev_task['id'], dev_task['ref'], dev_run, '2026-01-31T15:06:00+00:00')
    c.execute("INSERT INTO jobs(issue_id,mode,actor,status,created_at,started_at,run_id) VALUES(?,'execute','human:admin','started',?,?,?)",
              (dev_task['id'], '2026-01-31T14:57:00+00:00', '2026-01-31T14:59:00+00:00', dev_run))

stage_result = usage.stage_summary('2026-02-01', '2026-02-01', 'STG', as_of=fixed_now)
assert stage_result['previous'] == {'from_date': '2026-01-31', 'to_date': '2026-01-31'}
assert {r['ref'] for r in stage_result['issues']} == {stage_parent['ref'], partial_parent['ref']}
stage_row = next(r for r in stage_result['issues'] if r['ref'] == stage_parent['ref'])
assert stage_row['stages']['review'] == {'seconds': 60, 'tokens': 12}
assert stage_row['stages']['execute'] == {'seconds': 180, 'tokens': 57}
assert stage_row['stages']['queue']['seconds'] == 120
assert stage_row['queue_idle_seconds'] == 0  # 앞에서 만든 다른 프로젝트의 긴 실행이 대기 전부와 겹친다.
assert stage_row['stages']['completion']['seconds'] == 180
assert stage_row['stages']['plan_wait']['seconds'] == 120
assert stage_row['stages']['done_wait']['seconds'] == 300
assert (stage_row['execute_runs'], stage_row['failed_runs'], stage_row['total_tokens']) == (2, 1, 69)
assert not stage_row['partial']
assert next(r for r in stage_result['issues'] if r['ref'] == partial_parent['ref'])['partial']
assert stage_result['partial_count'] == 1
assert stage_result['daily_task_median'] == [{'date': '2026-02-01', 'seconds': 420, 'n': 1}]
assert stage_result['daily_task_tokens_median'] == [{'date': '2026-02-01', 'tokens': 57, 'n': 1}]
assert stage_result['daily_task_unmeasured'] == 1
assert stage_result['dev_changes'] == [{'ref': dev_task['ref'], 'title': 'dev task',
    'merged_at': '2026-01-31T15:06:00+00:00', 'date': '2026-02-01',
    'merged_at_local': '2026-02-01 00:06'}]
assert usage.stage_summary('2026-02-01', '2026-02-01', as_of=fixed_now)['daily_task_median'][0] == {
    'date': '2026-02-01', 'seconds': 480, 'n': 2}
assert usage.stage_summary('2026-02-01', '2026-02-01', as_of=fixed_now)['daily_task_tokens_median'][0] == {
    'date': '2026-02-01', 'tokens': 30, 'n': 2}
execute_stats = next(s for s in stage_result['stages'] if s['key'] == 'execute')
assert execute_stats['seconds'] == {'mean': 180, 'median': 180, 'p90': 180, 'n': 1}
assert execute_stats['previous']['seconds']['mean'] == 60
assert execute_stats['tokens']['mean'] == 57
assert stage_row['tasks'][0]['stages']['execute'] == {'seconds': 180, 'tokens': 57}
assert stage_row['tasks'][0]['stages']['completion']['seconds'] == 180
assert usage._idle_seconds(usage._time('2026-01-31T14:58:00+00:00'),
                           usage._time('2026-01-31T15:00:00+00:00'), [
                               (usage._time('2026-01-31T14:58:30+00:00'), usage._time('2026-01-31T14:59:15+00:00')),
                               (usage._time('2026-01-31T14:59:00+00:00'), usage._time('2026-01-31T14:59:30+00:00'))]) == 60
assert usage.stage_summary(project='STG', as_of=fixed_now)['from_date'] == '2026-01-26'

issues.create_project(human, 'TOK', 'daily tokens')
token_parent = issues.create_issue(human, 'TOK', 'token parent')
token_tasks = [issues.create_issue(human, 'TOK', f'token task {i}', parent=token_parent['ref'])
               for i in range(5)]
with db.connect() as c:
    for task_row, queued, merged, inp, out in [
        (token_tasks[0], '2026-01-31T14:55:00+00:00', '2026-01-31T15:05:00+00:00', 7, 3),
        (token_tasks[1], '2026-01-31T14:55:00+00:00', '2026-01-31T15:06:00+00:00', 20, 10),
        (token_tasks[2], '2026-02-01T14:55:00+00:00', '2026-02-01T15:05:00+00:00', 4, 2),
        (token_tasks[3], None, '2026-01-31T15:07:00+00:00', 500, 500)]:
        run_id = stage_run(c, task_row['id'], 'execute',
                           '2026-01-31T15:00:00+00:00' if task_row != token_tasks[2]
                           else '2026-02-01T15:00:00+00:00', merged, inp, out)
        stage_completion(c, task_row['id'], task_row['ref'], run_id, merged)
        if queued:
            c.execute("INSERT INTO jobs(issue_id,mode,actor,status,created_at,started_at,run_id) VALUES(?,'execute','human:admin','started',?,?,?)",
                      (task_row['id'], queued, queued, run_id))
    stage_run(c, token_tasks[0]['id'], 'execute', '2026-01-31T15:01:00+00:00',
              '2026-01-31T15:02:00+00:00', 3, 1, 'failed')
    stage_run(c, token_tasks[0]['id'], 'execute', '2026-01-31T15:08:00+00:00',
              '2026-01-31T15:09:00+00:00', 500, 500)
    stage_run(c, token_tasks[4]['id'], 'execute', '2026-01-31T15:00:00+00:00',
              '2026-01-31T15:01:00+00:00', 500, 500)
tokens_result = usage.stage_summary('2026-02-01', '2026-02-03', 'TOK',
                                    as_of='2026-02-03T15:30:00+00:00')
assert tokens_result['daily_task_tokens_median'] == [
    {'date': '2026-02-01', 'tokens': 22, 'n': 2},
    {'date': '2026-02-02', 'tokens': 6, 'n': 1},
    {'date': '2026-02-03', 'tokens': None, 'n': 0}]
assert tokens_result['daily_task_unmeasured'] == 1
assert [day['n'] for day in tokens_result['daily_task_median']] == [2, 1, 0]

with patch.object(db, 'init'), patch.object(usage, '_now', return_value=usage._time(fixed_now)), TestClient(app.app) as client:
    assert client.get('/api/trends/stages').status_code == 401
    client.cookies.set('ns_session', 'temporary')
    response = client.get('/api/trends/stages?from=2026-02-01&to=2026-02-01&project=stg&human=1')
    assert response.status_code == 200, response.text
    assert response.json()['include_human'] and response.json()['issues'][0]['ref'] == stage_parent['ref']
    assert response.json()['daily_task_tokens_median'] == [{'date': '2026-02-01', 'tokens': 57, 'n': 1}]
    for query in ('from=bad', 'to=2026-02-30', 'from=2026-02-02&to=2026-02-01',
                  'from=2025-01-01&to=2026-02-01', 'human=2'):
        assert client.get('/api/trends/stages?' + query).status_code == 400, query
    assert client.get('/api/trends/stages?project=MISSING').status_code == 404
    client.cookies.clear()
    key = auth.create_agent('stage-test')[1]
    assert client.get('/api/trends/stages', headers={'Authorization': 'Bearer ' + key}).status_code == 200
    assert client.get('/api/trends/stages', headers={'Authorization': 'Bearer invalid'}).status_code == 401
    with patch.object(auth, 'nightshift_user', return_value={'role': 'member'}):
        assert client.get('/api/trends/stages', cookies={'ns_session': 'temporary'}).status_code == 403

# agent 필터: all은 기존 결과와 같고, 실행이 모두 codex인 데이터는 codex와 같으며 claude는 비어 있다.
with db.connect() as c:   # 테스트 대기열 행은 기본 provider(claude)라 실행과 같은 codex로 맞춘다.
    c.execute("UPDATE jobs SET provider='codex'")
base = usage.stage_summary('2026-02-01', '2026-02-01', 'STG', as_of=fixed_now)
assert base['agent'] == 'all'
codex_only = usage.stage_summary('2026-02-01', '2026-02-01', 'STG', as_of=fixed_now, agent='codex')
assert codex_only['issues'] == base['issues'] and codex_only['stages'] == base['stages']
claude_only = usage.stage_summary('2026-02-01', '2026-02-01', 'STG', as_of=fixed_now, agent='claude')
assert claude_only['agent'] == 'claude' and claude_only['issues'] == []
assert claude_only['distribution'] == {'seconds': {'unit': 'seconds', 'bins': [], 'n': 0},
                                       'tokens': {'unit': 'tokens', 'bins': [], 'n': 0}}
# 분포 구간 합 = 부분 측정이 아닌 Issue 수
for kind in ('seconds', 'tokens'):
    dist = base['distribution'][kind]
    assert dist['n'] == sum(not r['partial'] for r in base['issues']) == sum(b['count'] for b in dist['bins']) and len(dist['bins']) == 8
# 한 Issue를 두 에이전트가 나눠 했으면 각자 자기 몫만 잡힌다.
with db.connect() as c:
    c.execute("UPDATE runs SET provider='claude' WHERE id=?", (review_id,))
mixed_claude = usage.stage_summary('2026-02-01', '2026-02-01', 'STG', as_of=fixed_now, agent='claude')
claude_row = next(r for r in mixed_claude['issues'] if r['ref'] == stage_parent['ref'])
assert claude_row['stages']['review'] == {'seconds': 60, 'tokens': 12} and claude_row['stages']['execute']['seconds'] == 0
assert claude_row['stages']['completion']['seconds'] == 0 and claude_row['stages']['queue']['seconds'] == 0
mixed_codex = usage.stage_summary('2026-02-01', '2026-02-01', 'STG', as_of=fixed_now, agent='codex')
codex_row = next(r for r in mixed_codex['issues'] if r['ref'] == stage_parent['ref'])
assert codex_row['stages']['review']['seconds'] == 0 and codex_row['stages']['execute'] == {'seconds': 180, 'tokens': 57}
try:
    usage.stage_summary(agent='gpt')
    raise AssertionError('invalid agent accepted')
except issues.StoreError:
    pass

with patch.object(db, 'init'), patch.object(usage, '_now', return_value=usage._time(fixed_now)), TestClient(app.app) as client:
    client.cookies.set('ns_session', 'temporary')
    for name in ('all', 'claude', 'codex'):
        r = client.get(f'/api/trends/stages?from=2026-02-01&to=2026-02-01&project=stg&agent={name}')
        assert r.status_code == 200 and r.json()['agent'] == name and 'distribution' in r.json(), r.text
    assert client.get('/api/trends/stages?agent=gpt').status_code == 400
    client.cookies.clear()
    assert client.get('/api/trends/stages?agent=claude').status_code == 401
print('OK stage usage')

# 새 읽기 라우트의 부모 합계·기간·페이지·권한을 실제 앱 요청으로 확인한다.
fixed = '2026-01-01T00:01:00+00:00'
grouped = usage.trend_issues('7', 'US', as_of=fixed)
parent_row = next(r for r in grouped['issues'] if r['ref'] == parent['ref'])
task_row = next(r for r in grouped['issues'] if r['ref'] == task['ref'])
assert parent_row['total_tokens'] == 15 and task_row['total_tokens'] == 15
assert parent_row['agent_seconds'] >= task_row['agent_seconds'] and task_row['agent_seconds'] >= 240, (parent_row, task_row)
assert parent_row['partial'] and task_row['partial']
assert grouped['issues'] == sorted(grouped['issues'], key=lambda r: (-usage._time(r['last_activity_at']).timestamp(), r['ref']))
assert usage.trend_issues('7', 'US', as_of='2026-02-01T00:00:00+00:00')['total'] == 0
assert usage.trend_issues('7', 'US', as_of='2026-01-20T00:00:00+00:00')['total'] == 0
assert usage.trend_issues('30', 'US', as_of='2026-01-20T00:00:00+00:00')['total'] >= 2
assert all(r['project'] == 'US' for r in grouped['issues'])
assert usage.trend_issues('all', 'US', limit=1, offset=1, as_of=fixed)['issues'][0]
with patch.object(db, 'init'), TestClient(app.app) as client:
    assert client.get('/api/trends/issues').status_code == 401
    client.cookies.set('ns_session', 'temporary')
    assert client.get('/api/trends/issues?period=all&project=US&limit=1').status_code == 200
    assert client.get('/api/trends/issues?period=all&project=US&limit=1').json()['has_more']
    for query in ('period=bad', 'limit=0', 'limit=201', 'offset=-1'):
        assert client.get('/api/trends/issues?' + query).status_code == 400
    assert client.get('/api/trends/issues?project=MISSING').status_code == 404
    assert client.get('/api/trends/issues?limit=abc').status_code == 422
    client.cookies.clear()
    key = auth.create_agent('trend-issues-test')[1]
    assert client.get('/api/trends/issues', headers={'Authorization': 'Bearer ' + key}).status_code == 200
    with patch.object(auth, 'nightshift_user', return_value={'role': 'member'}):
        assert client.get('/api/trends/issues', cookies={'ns_session': 'temporary'}).status_code == 403
print('OK trend issues')

# 수동 사용량은 캐시·실행 시간·횟수와 분리하고 실제 HTTP 응답까지 확인한다.
issues.create_project(human, 'MAN', 'manual')
mp = issues.create_issue(human, 'MAN', 'manual parent')
mt = issues.create_issue(human, 'MAN', 'manual task', parent=mp['ref'])
mt2 = issues.create_issue(human, 'MAN', 'second manual task', parent=mp['ref'])
with db.connect() as c:
    for message, session, iid, inp, out, ts in [
        ('mp1', 'session-a', mp['id'], 10, 2, '2026-02-01T14:59:00+00:00'),
        ('mp2', 'session-a', mp['id'], 20, 3, '2026-02-01T14:59:01+00:00'),
        ('mt1', 'session-a', mt['id'], 30, 4, '2026-02-01T14:59:02+00:00'),
        ('mt2', 'session-a', mt2['id'], 1, 1, '2026-02-01T14:59:02+00:00'),
        ('mu1', 'session-u', None, 40, 5, '2026-02-01T14:59:03+00:00'),
        ('future', 'session-a', mp['id'], 900, 9, '2026-02-02T15:00:00+00:00')]:
        c.execute("""INSERT INTO manual_usage(message_id,session_id,issue_id,model,input_tokens,
            output_tokens,cache_creation_tokens,cache_read_tokens,ts,created_at)
            VALUES(?,?,?,'unknown-model',?,?,500,600,?,?)""", (message, session, iid, inp, out, ts, ts))
manual_now = '2026-02-02T00:00:00+00:00'
with TestClient(app.app) as client:
    client.cookies.set('ns_session', 'temporary')
    detail = client.get('/api/issues/' + mp['ref']).json()
    assert detail['token_usage']['total_tokens'] == 944
    assert detail['token_usage']['manual_session_count'] == 1
    assert detail['token_usage']['run_count'] == 0
    assert detail['task_token_usage']['total_tokens'] == 36
    assert detail['task_token_usage']['manual_tokens'] == 36
    assert detail['task_token_usage']['manual_session_count'] == 1
    assert len(detail['manual_sessions']) == 1
    assert detail['manual_sessions'][0]['input_tokens'] == 930
    assert detail['manual_sessions'][0]['started_at'] == '2026-02-01T14:59:00.000Z'
    assert 'cost_usd' not in detail['manual_sessions'][0]
    rows = client.get('/api/issues?project=MAN').json()['issues']
    assert next(r for r in rows if r['id'] == mt['id'])['token_usage']['total_tokens'] == 34
    trend = client.get('/api/trends?from=2026-02-01&to=2026-02-01&project=MAN').json()
    assert trend['summary']['total_tokens'] == 71
with patch.object(usage, '_now', return_value=usage._time(manual_now)):
    all_trends = usage.trends('2026-02-01', '2026-02-01')
    scoped = usage.trends('2026-02-01', '2026-02-01', 'MAN')
    assert all_trends['summary']['unlinked_manual_tokens'] == 45
    assert scoped['summary']['manual_tokens'] == scoped['summary']['total_tokens'] == 71
    assert scoped['summary']['unlinked_manual_tokens'] == 0
    assert scoped['summary']['run_count'] == scoped['summary']['agent_seconds'] == 0
    assert scoped['summary']['unpriced_tokens'] == 71
    assert {r['ref']: r['total_tokens'] for r in scoped['top_issues']} == {mp['ref']:35, mt['ref']:34, mt2['ref']:2}
    assert usage.issue_summary('yesterday', 'MAN')['summary']['total_tokens'] == 71
# 직접 실행과 수동 사용량을 함께 저장해도 곱집계하지 않는다.
with db.connect() as c:
    c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at,ended_at,input_tokens,output_tokens) VALUES(?,'review','ok','human:admin',?,?,7,8)", (mt['id'], start, end))
assert issues.get_issue(mp['ref'])['task_token_usage']['total_tokens'] == 51
assert usage.project_usage('MAN')['summary']['total_tokens'] == 995
print('OK manual usage aggregation and HTTP')
