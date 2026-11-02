"""목록·상세 묶음 조회의 응답 동일성과 연결·git 호출 수를 검사한다."""
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
import auth, db, issues, jobs, orchestrate, run_progress

db.init()
me = {'kind': 'human', 'name': 'admin'}
repo = Path(tempfile.mkdtemp())
def git(*args):
    r = orchestrate._git(str(repo), *args)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()
git('init', '-q', '-b', orchestrate.BASE_BRANCH)
(repo / 'base').write_text('base')
git('add', '.')
git('commit', '-qm', 'base')
issues.create_project(me, 'BATCH', 'batch', local_path=str(repo))
parent = issues.create_issue(me, 'BATCH', 'parent')
tasks = [issues.create_issue(me, 'BATCH', str(n), parent=parent['ref']) for n in range(600)]
with db.connect() as c:
    c.execute("UPDATE issues SET status='in_review' WHERE sub_of=?", (parent['id'],))
    for n, task in enumerate(tasks[:12]):
        for phase in ['complete', ['checking', 'complete', 'held'][n % 3]]:
            rid = c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at) VALUES(?,'execute','ok','human:admin',?)", (task['id'], db.now_iso())).lastrowid
            c.execute("INSERT INTO execution_completion(run_id,issue_id,ref,actor,task_sha,auto_merge,phase,owner_event,cfg_json,updated_at) VALUES(?,?,?,'human:admin','sha',?,?,0,'{}',?)", (rid, task['id'], task['ref'], n % 2, phase, db.now_iso()))
    for mode in ['review', 'execute']:
        c.execute("INSERT INTO jobs(issue_id,mode,actor,status,created_at) VALUES(?,?,'human:admin','queued',?)", (tasks[0]['id'], mode, db.now_iso()))
# 병합된 브랜치·안 된 브랜치·없는 브랜치를 섞는다.
for task in tasks[12:22]:
    git('branch', orchestrate.branch_name(task['ref']))
git('checkout', '-qb', orchestrate.branch_name(tasks[22]['ref']))
(repo / 'feature').write_text('feature')
git('add', '.')
git('commit', '-qm', 'feature')
git('checkout', orchestrate.BASE_BRANCH)
assert orchestrate.completions([t['ref'] for t in tasks]) == {t['ref']: orchestrate.completion(t['ref']) for t in tasks if orchestrate.completion(t['ref'])}
with db.connect() as c:
    assert 'execution_completion_ref' in {r['name'] for r in c.execute('PRAGMA index_list(execution_completion)')}
    assert any('execution_completion_ref' in r['detail'] for r in c.execute('EXPLAIN QUERY PLAN SELECT * FROM execution_completion WHERE ref=?', (tasks[0]['ref'],)))

auth._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={'user': {'username': 'admin', 'role': 'admin'}})))
client = TestClient(A.app)
client.cookies.set('ns_session', 'test')
original_connect = db.connect
original_git = orchestrate._run_git
counts = []
for limit in [60, 600]:
    with patch.object(db, 'connect', wraps=original_connect) as con, patch.object(orchestrate, '_run_git', wraps=original_git) as calls:
        r = client.get(f'/api/issues?project=BATCH&limit={limit}')
        assert r.status_code == 200, r.text
        counts.append(con.call_count)
        assert calls.call_count == 1, calls.call_args_list
    for item in r.json()['issues']:
        assert item['merge_state'] == orchestrate.merge_state(item), item
        assert item['job'] == jobs.job_for(item['id'])
        assert item['run_progress'] == run_progress.for_issue(item)
assert counts[0] == counts[1] and counts[1] <= 10, counts
r = client.get('/api/issues/' + parent['ref'])
assert r.status_code == 200, r.text
children = {ch['id']: ch for ch in r.json()['children']}
events = issues.events_for(children)
queued = {j['issue_id']: j for j in jobs.list_jobs() if j['mode'] == 'execute'}
for ch in children.values():
    assert ch['merge_state'] == orchestrate.merge_state({**ch, 'events': events[ch['id']]})
    assert ch['job'] == queued.get(ch['id'])
    assert ch['run_progress'] == run_progress.for_issue(ch)
# 요청 밖 ref의 단건 경로와 설정 OFF도 비교한다.
batch = orchestrate.MergeBatch([])
item = issues.get_issue(tasks[22]['ref'])
assert orchestrate.merge_state(item, batch=batch) == orchestrate.merge_state(item)
batch = orchestrate.MergeBatch([item])
assert orchestrate.merge_state(item, {'auto_merge': False}, batch=batch) == orchestrate.merge_state(item, {'auto_merge': False})
print('OK — 응답 동일성·600개 묶음·연결 수·git 1회·인덱스', counts)
