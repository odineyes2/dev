"""실패 검사 재현 상태 전환·소유권·격리·기존 Agent와 검사 기록 보존을 확인한다."""
import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess
from unittest.mock import patch

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
os.environ['NTFY_TOPIC'] = ''
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import httpx
from fastapi.testclient import TestClient
import app as A
import auth, config, db, execute, issues, jobs, orchestrate, project_build, regression, reproduce, review

db.init()
me = {'kind': 'human', 'name': 'admin'}
repo = Path(tempfile.mkdtemp())
def git(*args):
    return project_build._git(str(repo), *args)
git('init', '-q', '-b', 'main')
(repo / 'tests').mkdir()
(repo / 'tests/test_ui.py').write_text('''import sys
page = shots = None
def check_progress_refresh(page, shots):
    print('focused reproduction')
    raise SystemExit(7)
if '--progress-only' in sys.argv:
    check_progress_refresh(page, shots)
else:
    raise SystemExit(99)
''', encoding='utf-8')
git('add', '.'); git('commit', '-qm', 'test fixture')
issues.create_project(me, 'REP', 'reproduction', local_path=str(repo))
parent = issues.create_issue(me, 'REP', 'parent')
issues.post_plan(me, parent['ref'], 'approved')
issues.decide(me, parent['ref'], 'approve', plan_version=1)
task = issues.create_issue(me, 'REP', 'task', parent=parent['ref'])
ref = task['ref']
wt = execute.prepare_worktree(str(repo), ref)
_, rid = review.begin(me, task, 'execute', 'codex')
log = config.DATA_DIR / 'merge-tests/test_ui-1.log'
log.parent.mkdir()
log.write_text('File "tests/test_ui.py", line 8, in check_progress_refresh\nTimeoutError', encoding='utf-8')
with db.connect() as c:
    c.execute("UPDATE runs SET status='failed',input_tokens=123 WHERE id=?", (rid,))
issues.set_status(me, ref, 'changes_requested', '전체 로그: ' + str(log))
selection = {'results': {'test_ui.py': 'failed', 'test_db.py': 'passed'}}
regression.save_record('regression-selection:' + ref + ':server_completion', selection)
state = reproduce.panel(issues.get_issue(ref))
assert state['available'] and state['targets'] == [{'file':'tests/test_ui.py', 'args':['--progress-only']}]
auth._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(
    200, json={'user': {'username':'admin', 'role':'admin'} if 'ns_session=fake' in req.headers.get('cookie', '') else None})))
client = TestClient(A.app)
url = '/api/issues/' + ref + '/reproduce-checks'
headers = {'X-Requested-With': 'dev'}
r = client.post(url, headers=headers, json={'token':state['token']}); assert r.status_code == 401, (r.status_code, r.text)
client.cookies.set('ns_session', 'fake')
assert client.post(url, json={'token':state['token']}).status_code == 403
assert client.post(url, headers=headers, json={'token':'old'}).status_code == 409
agent, key = auth.create_agent('test', 'openai', '')
assert client.post(url, headers={'Authorization':'Bearer '+key}, json={'token':state['token']}).status_code == 403
assert client.get(url+'/log').status_code == 404
before = git('rev-parse', 'HEAD')
with patch.object(reproduce, 'Thread'):
    response = client.post(url, headers=headers, json={'token':state['token']})
assert response.status_code == 202, response.text
row = response.json()
assert issues.get_issue(ref)['status'] == 'in_progress'
assert client.post(url, headers=headers, json={'token':state['token']}).status_code == 409
with db.connect() as c:
    assert reproduce.running(c)
    assert '실패 검사 재현' in jobs.start_block(c, 'REP', 'execute')
assert orchestrate._busy({'pm2_app':jobs.SELF_PM2_APP})
with patch.object(review, 'launch', side_effect=AssertionError('모델 호출 금지')), patch.dict(os.environ, {'MOAI_SECRET':'synthetic-secret'}):
    reproduce.worker(ref, str(repo), row)
result = reproduce.result(ref)
assert result['status'] == 'failed' and result['results'][0]['exit_code'] == 7
assert 'focused reproduction' in client.get(url+'/log').text
assert 'synthetic-secret' not in client.get(url+'/log').text
assert client.get(url+'/log', headers={'Authorization':'Bearer '+key}).status_code == 403
assert issues.get_issue(ref)['status'] == 'changes_requested'
assert regression.load_record('regression-selection:' + ref + ':server_completion') == selection
assert git('rev-parse', 'HEAD') == before and not git('status', '--porcelain')
with db.connect() as c:
    assert c.execute('SELECT COUNT(*),SUM(input_tokens) FROM runs').fetchone()[0] == 1
    assert c.execute('SELECT input_tokens FROM runs WHERE id=?', (rid,)).fetchone()[0] == 123
def begin():
    state = reproduce.panel(issues.get_issue(ref))
    with patch.object(reproduce, 'Thread'):
        row = reproduce.start(me, ref, state['token'])
    assert issues.get_issue(ref)['status'] == 'in_progress'
    return row

passed = subprocess.CompletedProcess([], 0, 'passed', '')
row = begin()
with patch.object(orchestrate, 'run_test', return_value=passed):
    reproduce.worker(ref, str(repo), row)
assert issues.get_issue(ref)['status'] == 'in_review'
assert reproduce.result(ref)['status'] == 'passed'
record = {'auto_merge':True, 'phase':'failed', 'merge_sha':'', 'task_sha':row['source_sha']}
assert orchestrate.retry_available(issues.get_issue(ref), record)
assert not orchestrate.retry_available(issues.get_issue(ref), {**record, 'task_sha':'different'})
assert regression.load_record('regression-selection:' + ref + ':server_completion') == selection

row = begin()
issues.set_status(me, ref, 'on_hold', '사람이 변경한 상태')
with patch.object(orchestrate, 'run_test', return_value=passed):
    reproduce.worker(ref, str(repo), row)
assert issues.get_issue(ref)['status'] == 'on_hold'
assert issues.get_issue(ref)['events'][-1]['body'] == '사람이 변경한 상태'

row = begin()
(wt / 'changed.txt').write_text('new commit')
project_build._git(str(wt), 'add', 'changed.txt')
project_build._git(str(wt), 'commit', '-qm', 'changed during reproduction')
with patch.object(orchestrate, 'run_test', return_value=passed):
    reproduce.worker(ref, str(repo), row)
assert issues.get_issue(ref)['status'] == 'on_hold'
assert '커밋이 바뀌어' in reproduce.result(ref)['note']

row = begin()
regression.save_record(reproduce.PREFIX + ref, {**row, 'process':'previous-server'})
reproduce.recover()
assert reproduce.result(ref)['status'] == 'interrupted' and issues.get_issue(ref)['status'] == 'on_hold'
assert reproduce.panel(issues.get_issue(ref))['available']
state = reproduce.panel(issues.get_issue(ref))
with patch.object(reproduce, 'Thread') as thread:
    thread.return_value.start.side_effect = RuntimeError('cannot start')
    assert client.post(url, headers=headers, json={'token':state['token']}).status_code == 503
assert issues.get_issue(ref)['status'] == 'changes_requested'
log.write_text('File "tests/test_ui.py", line 8, in check_unknown', encoding='utf-8')
assert not reproduce.panel(issues.get_issue(ref))['available']
print('Reproduction workflow: In Progress, pass→In Review, failure→Changes Requested, restart, ownership, SHA, auth, logs and preserved token records OK')
