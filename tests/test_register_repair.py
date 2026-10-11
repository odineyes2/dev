"""수동 수정 결과는 기존 승인·실행 기록으로 서버 검사에 넘기며 모델을 호출하지 않는다."""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
os.environ['NTFY_TOPIC'] = ''
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import httpx
from fastapi.testclient import TestClient
import app as A
import auth, db, execute, issues, orchestrate, review

db.init()
me = {'kind': 'human', 'name': 'admin'}
repo = Path(tempfile.mkdtemp())
def git(*args):
    r = orchestrate._git(str(repo), *args)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()
git('init', '-q', '-b', 'main')
(repo / 'base.txt').write_text('base')
git('add', '.'); git('commit', '-qm', 'base')
issues.create_project(me, 'RETRY', 'retry', local_path=str(repo))
parent = issues.create_issue(me, 'RETRY', 'parent')
issues.post_plan(me, parent['ref'], 'approved')
issues.decide(me, parent['ref'], 'approve', plan_version=1)
task = issues.create_issue(me, 'RETRY', 'task', parent=parent['ref'])
ref = task['ref']
wt = execute.prepare_worktree(str(repo), ref)
_, rid = review.begin(me, task, 'execute', 'codex')
with db.connect() as c:
    c.execute('UPDATE runs SET task_start_sha=? WHERE id=?', (git('rev-parse', 'HEAD'), rid))
(wt / 'feature.txt').write_text('feature')
assert orchestrate._git(str(wt), 'add', '.').returncode == 0
assert orchestrate._git(str(wt), 'commit', '-qm', 'implemented').returncode == 0
sha = orchestrate._git(str(wt), 'rev-parse', 'HEAD').stdout.strip()
issues.link_commit(me, ref, sha)
with db.connect() as c:
    c.execute('INSERT INTO task_execution_results(run_id,plan_version) VALUES(?,1)', (rid,))

original_end = db.now_iso()
with db.connect() as c:
    c.execute("UPDATE runs SET status='ok',input_tokens=123,output_tokens=45,ended_at=? WHERE id=?", (original_end,rid))
issues.set_status(me, ref, 'in_review', 'manual repair')
assert execute.repair_available(issues.get_issue(ref))
assert execute.panel(issues.get_issue(ref))['repair_available']
(wt / 'dirty.txt').write_text('dirty')
try:
    execute.register_repair(me, ref)
    raise AssertionError('dirty worktree accepted')
except issues.StoreError:
    pass
(wt / 'dirty.txt').unlink()
with db.connect() as c:
    c.execute('UPDATE task_execution_results SET plan_version=99 WHERE run_id=?', (rid,))
try:
    execute.register_repair(me, ref)
    raise AssertionError('stale plan accepted')
except issues.StoreError:
    pass
with db.connect() as c:
    c.execute('UPDATE task_execution_results SET plan_version=1 WHERE run_id=?', (rid,))
auth._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(
    200, json={'user': {'username': 'admin', 'role': 'admin'}})))
client = TestClient(A.app)
url = '/api/issues/' + ref + '/register-repair'
assert client.post(url, headers={'X-Requested-With': 'dev'}).status_code == 401
client.cookies.set('ns_session', 'test')
assert client.post(url).status_code == 403
_, key = auth.create_agent('chat', 'openai', '')
assert client.post(url, headers={'Authorization': 'Bearer ' + key}).status_code == 403
with patch.object(execute, 'threading') as thread_module:
    response = client.post(url, headers={'X-Requested-With':'dev'})
    assert response.status_code == 202, response.text
    assert thread_module.Thread.call_args.kwargs['args'][0] is execute._register_repair
    assert client.post(url, headers={'X-Requested-With':'dev'}).status_code == 409
with patch.object(orchestrate, 'run_tests', return_value='failed check'), patch.object(orchestrate, 'handle') as merge:
    execute._register_repair(me, ref, rid, sha, original_end)
    merge.assert_not_called()
assert issues.get_issue(ref)['status'] == 'on_hold'
assert execute.repair_available(issues.get_issue(ref))
with patch.object(execute, 'threading'):
    execute.register_repair(me, ref)
with patch.object(orchestrate, 'run_tests', return_value=None) as tests, patch.object(orchestrate, 'handle') as merge:
    execute._register_repair(me, ref, rid, sha, original_end)
    tests.assert_called_once()
    merge.assert_called_once_with(me, ref)
assert orchestrate.completion(ref)['task_sha'] == sha
assert not execute.repair_available(issues.get_issue(ref))
with db.connect() as c:
    assert c.execute('SELECT COUNT(*) FROM runs WHERE issue_id=?', (task['id'],)).fetchone()[0] == 1
    assert c.execute('SELECT input_tokens FROM runs WHERE id=?', (rid,)).fetchone()[0] == 123
    assert c.execute('SELECT ended_at FROM runs WHERE id=?', (rid,)).fetchone()[0] == original_end
print('register repair OK: admin, clean commit, original plan, failure retry, no agent')
