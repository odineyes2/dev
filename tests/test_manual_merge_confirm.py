"""수동 반영 확인(DEV-148) — 자동 병합 실패 뒤 직접 main에 넣은 Task를 git 확인 후 병합 완료로 표시하는지 실제 앱 요청으로 확인한다."""
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
import auth, db, execute, issues, mcp_tools, orchestrate, review

db.init()
me = {'kind': 'human', 'name': 'admin'}
repo = Path(tempfile.mkdtemp())
def git(*args, cwd=repo):
    r = orchestrate._git(str(cwd), *args)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()
git('init', '-q', '-b', 'main')
git('config', 'user.email', 't@example.com'); git('config', 'user.name', 't')
(repo / 'notes.txt').write_text('base\n')
git('add', '.'); git('commit', '-qm', 'base')
issues.create_project(me, 'MAN', 'manual', local_path=str(repo))
parent = issues.create_issue(me, 'MAN', 'parent')
issues.post_plan(me, parent['ref'], 'approved')
issues.decide(me, parent['ref'], 'approve', plan_version=1)
task = issues.create_issue(me, 'MAN', 'task', parent=parent['ref'])
ref = task['ref']
wt = execute.prepare_worktree(str(repo), ref)
_, rid = review.begin(me, task, 'execute', 'codex')
with db.connect() as c:
    c.execute('UPDATE runs SET task_start_sha=? WHERE id=?', (git('rev-parse', 'HEAD'), rid))
# Task와 main이 같은 줄을 바꿔 자동 병합이 충돌로 실패하게 한다(MO-55-1의 결정 기록 충돌과 같은 꼴).
(wt / 'notes.txt').write_text('task\n')
git('commit', '-qam', 'implemented', cwd=wt)
sha = git('rev-parse', 'HEAD', cwd=wt)
issues.link_commit(me, ref, sha)
with db.connect() as c:
    c.execute('INSERT INTO task_execution_results(run_id,plan_version) VALUES(?,1)', (rid,))
execute.register_completion(me, ref, wt, rid, 'implemented')
(repo / 'notes.txt').write_text('other\n')
git('commit', '-qam', 'other change')
with patch.object(orchestrate, 'run_tests', return_value=None):
    orchestrate.handle(me, ref)
record = orchestrate.completion(ref)
assert record['phase'] == 'failed' and not record['merge_sha'], record
assert issues.get_issue(ref)['status'] == 'changes_requested'
panel = execute.panel(issues.get_issue(ref))
assert panel['manual_merge_confirmable'] and panel['result_approval_blocked']

auth._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(
    200, json={'user': {'username': 'admin', 'role': 'admin'}})))
client = TestClient(A.app)
url = '/api/issues/' + ref + '/confirm-manual-merge'
assert client.post(url, headers={'X-Requested-With': 'dev'}).status_code == 401
client.cookies.set('ns_session', 'test')
headers = {'X-Requested-With': 'dev'}

# 아직 main에 없으면 거절하고 기록은 그대로다.
r = client.post(url, headers=headers)
assert r.status_code == 409 and 'main에 없어요' in r.json()['detail'], r.text
assert orchestrate.completion(ref)['phase'] == 'failed'

# 사람이 Task 브랜치에서 충돌을 풀고(main 병합) main을 ff — 브랜치 끝이 반영 커밋이 된다.
assert orchestrate._git(str(wt), 'merge', 'main').returncode != 0
(wt / 'notes.txt').write_text('task\nother\n')
git('add', 'notes.txt', cwd=wt); git('commit', '-qm', 'resolve', cwd=wt)
tip = git('rev-parse', 'HEAD', cwd=wt)
git('merge', '--ff-only', orchestrate.branch_name(ref))
issues.set_status(me, ref, 'in_review', 'resolved by hand')

# 에이전트 키로도 쓸 수 있다 — git 확인이 같은 경계다.
client.cookies.clear()
_, key = auth.create_agent('chat', 'claude', '')
r = client.post(url, headers={'Authorization': 'Bearer ' + key})
assert r.status_code == 200, r.text
assert r.json() == {'ref': ref, 'task_sha': sha, 'merge_sha': tip, 'status': 'complete'}
record = orchestrate.completion(ref)
assert record['phase'] == 'complete' and record['merge_sha'] == tip and record['notified'] == 1
full = issues.get_issue(ref)
assert full['status'] == 'in_review'
assert any(e['kind'] == 'comment' and e['body'].startswith('🔀 수동 반영 확인') for e in full['events'])
panel = execute.panel(full)
assert not panel['result_approval_blocked'] and not panel['manual_merge_confirmable']
assert orchestrate.merge_state(full) == '병합됨'

# 이미 완료된 기록은 다시 바꾸지 않는다.
client.cookies.set('ns_session', 'test')
assert client.post(url, headers=headers).status_code == 409
# MCP 도구로 노출된다.
assert callable(getattr(mcp_tools.confirm_manual_merge, 'fn', mcp_tools.confirm_manual_merge))
print('ok')
