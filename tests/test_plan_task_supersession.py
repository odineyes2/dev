"""새 계획 승인은 이전 자동 Task를 실행 대상에서 빼고 이력을 남긴다."""
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import db, issues, jobs, execute
db.init()
me = {'kind': 'human', 'name': 'admin'}
issues.create_project(me, 'TASK', '계획 교체')
root = issues.create_issue(me, 'TASK', '목표', labels=['goal'])
def plan(titles):
    return '## Tasks\n' + '\n'.join(f'{i}. {t} | 파일: a.py | 확인: 동작 | 선행: 없음' for i,t in enumerate(titles,1))
issues.post_plan(me, root['ref'], plan(['SQLite 수집','SQLite 화면','유지']))
issues.decide(me, root['ref'], 'approve', plan_version=1)
old = issues.get_issue(root['ref'])['children']
manual = issues.create_issue(me, 'TASK', '수동 작업', parent=root['ref'])
with db.connect() as conn:
    # 보관 대상의 기존 검토/실행/커밋 기록을 보존한다.
    conn.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at,ended_at) VALUES(?,'execute','ok','human:admin',?,?)", (old[0]['id'], db.now_iso(), db.now_iso()))
    issues._event(conn, old[0]['id'], me, 'commit', '기존 커밋', {'sha': 'a'*40})
with patch.object(jobs, 'pump', lambda: None), patch.object(execute, 'blocked_reason', lambda *a,**kw: None):
    queued = jobs.enqueue(me, old[1]['ref'], 'execute')
issues.post_plan(me, root['ref'], plan(['PostgreSQL 수집','유지']))
before = issues.get_issue(root['ref'])
assert len(before['children']) == 4 and not before['superseded_children']  # 승인 전에는 보관하지 않는다.
issues.decide(me, root['ref'], 'approve', plan_version=2)
current = issues.get_issue(root['ref'])
assert {t['title'] for t in current['children']} == {'PostgreSQL 수집','유지','수동 작업'}
assert {t['ref'] for t in current['superseded_children']} == {old[0]['ref'],old[1]['ref']}
assert not jobs.list_jobs()
for task in old[:2]:
    saved = issues.get_issue(task['ref'])
    assert saved['superseded_by_plan'] == 2
    assert execute.blocked_reason(saved, current)
    assert task['ref'] not in {t['ref'] for t in issues.list_issues()}
assert len(issues.get_issue(old[0]['ref'])['events']) >= 2
with db.connect() as conn:
    assert conn.execute('SELECT COUNT(*) FROM runs WHERE issue_id=?',(old[0]['id'],)).fetchone()[0] == 1
    assert conn.execute('SELECT status,cancellation_reason FROM jobs WHERE id=?',(queued['job_id'],)).fetchone()[:] == ('cancelled','plan_superseded')
    # 이미 승인된 구 데이터의 누락된 보관을 복구하며 반복하지 않는다.
    conn.execute("DELETE FROM events WHERE issue_id=? AND json_extract(data_json,'$.superseded_by_plan') IS NOT NULL", (old[0]['id'],))
    issues.reconcile_plan_tasks(conn)
    issues.reconcile_plan_tasks(conn)
    assert conn.execute("SELECT COUNT(*) FROM events WHERE issue_id=? AND json_extract(data_json,'$.superseded_by_plan')=2",(old[0]['id'],)).fetchone()[0] == 1

# 실행 중인 이전 Task가 있으면 새 승인/Task 생성/보관 모두 되돌린다.
next_root = issues.create_issue(me, 'TASK', '실행 중')
issues.post_plan(me, next_root['ref'], plan(['이전 작업']))
issues.decide(me, next_root['ref'], 'approve', plan_version=1)
child = issues.get_issue(next_root['ref'])['children'][0]
with db.connect() as conn:
    conn.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at) VALUES(?,'execute','running','human:admin',?)", (child['id'],db.now_iso()))
issues.post_plan(me, next_root['ref'], plan(['새 작업']))
try:
    issues.decide(me, next_root['ref'], 'approve', plan_version=2)
    raise AssertionError('실행 중 승인 거절 필요')
except issues.StoreError as error:
    assert error.status == 409
assert len(issues.get_issue(next_root['ref'])['children']) == 1
assert issues.get_issue(next_root['ref'])['approval']['plan_version'] == 1
print('plan supersession: old history, active/manual/reused tasks, queue cancellation, repair, active guard OK')
