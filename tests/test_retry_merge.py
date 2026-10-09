"""병합 재시도 API·중복/변경 거부와 기존 완료 기록을 통한 병합만 실행을 확인한다."""
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
execute.register_completion(me, ref, wt, rid, 'implemented')
(repo / 'other.txt').write_text('new base')
git('add', '.'); git('commit', '-qm', 'base advanced')
with patch.object(orchestrate, 'run_tests', return_value='old failure'):
    orchestrate.handle(me, ref)
assert orchestrate.completion(ref)['phase'] == 'failed'
assert issues.get_issue(ref)['status'] == 'changes_requested'
auth._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(
    200, json={'user': {'username': 'admin', 'role': 'admin'}})))
client = TestClient(A.app)
url = '/api/issues/' + ref + '/retry-merge'
assert client.post(url, headers={'X-Requested-With': 'dev'}).status_code == 401
client.cookies.set('ns_session', 'test')
assert client.post(url).status_code == 403
headers = {'X-Requested-With': 'dev'}
_, key = auth.create_agent('chat', 'openai', '')
assert client.post(url, headers={'Authorization': 'Bearer ' + key}).status_code == 403
(wt / 'dirty.txt').write_text('unsaved')
assert client.post(url, headers=headers).status_code == 409
(wt / 'dirty.txt').unlink()
with db.connect() as c:
    c.execute('UPDATE execution_completion SET task_sha=? WHERE run_id=?', ('0' * 40, rid))
assert client.post(url, headers=headers).status_code == 409
with db.connect() as c:
    c.execute('UPDATE execution_completion SET task_sha=?,merge_sha=? WHERE run_id=?', (sha, sha, rid))
assert client.post(url, headers=headers).status_code == 409
with db.connect() as c:
    c.execute("UPDATE execution_completion SET merge_sha='' WHERE run_id=?", (rid,))
    c.execute('UPDATE task_execution_results SET plan_version=99 WHERE run_id=?', (rid,))
assert client.post(url, headers=headers).status_code == 409
with db.connect() as c:
    c.execute('UPDATE task_execution_results SET plan_version=1 WHERE run_id=?', (rid,))
with patch.object(orchestrate, 'threading') as threading_mock:
    thread = threading_mock.Thread
    response = client.post(url, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()['task_sha'] == sha
    assert thread.call_args.kwargs['args'][0] is orchestrate.handle
    assert client.post(url, headers=headers).status_code == 409
assert orchestrate.completion(ref)['phase'] == 'ready'
with patch.object(orchestrate, 'run_tests', return_value=None) as tests:
    orchestrate.handle(me, ref)
    assert tests.call_count == 1
assert issues.get_issue(ref)['status'] == 'in_review'
assert orchestrate.completion(ref)['phase'] == 'complete'
assert git('merge-base', '--is-ancestor', sha, 'main') == ''
assert client.post(url, headers=headers).status_code == 409
with db.connect() as c:
    assert c.execute('SELECT COUNT(*) FROM runs WHERE issue_id=?', (task['id'],)).fetchone()[0] == 1
print('retry merge OK: same run, no agent, checked and merged')
