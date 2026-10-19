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

# DEV-146 — 새 판에서 같은 제목으로 그대로 쓰는 Task는 선행·본문을 새 판에 맞춘다(MO-52-2가 보관된 52-1을 기다렸다).
def plan_deps(rows):
    return '## Tasks\n' + '\n'.join(f'{i}. {t} | 파일: {f} | 확인: {k} | 선행: {a}' for i, (t, f, k, a) in enumerate(rows, 1))
def blockers(ref):
    return [b['ref'] for b in issues.get_issue(ref)['blocked_by']]
for started in (False, True):
    p = issues.create_issue(me, 'TASK', f'재사용 {started}')['ref']
    issues.post_plan(me, p, plan_deps([('A 작업', 'a.py', '옛 확인', '없음'), ('B 화면', 'b.js', '옛 확인', '1')]))
    issues.decide(me, p, 'approve', plan_version=1)
    a, b = issues.get_issue(p)['children']
    assert blockers(b['ref']) == [a['ref']]
    if started:
        with db.connect() as conn:
            conn.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at,ended_at) VALUES(?,'execute','failed','human:admin',?,?)", (b['id'], db.now_iso(), db.now_iso()))
    issues.post_plan(me, p, plan_deps([('X 이관', 'x.sql', '이관', '없음'), ('Y 발행', 'y.py', '발행', '1'), ('B 화면', 'b.js, c.css', '새 확인', '2')]))
    issues.decide(me, p, 'approve_notes', note='표 열은 끝에', plan_version=2)
    cur = issues.get_issue(p)
    y = next(t for t in cur['children'] if t['title'] == 'Y 발행')
    assert {t['title'] for t in cur['children']} == {'X 이관', 'Y 발행', 'B 화면'} and [t['ref'] for t in cur['superseded_children']] == [a['ref']]
    assert blockers(b['ref']) == [y['ref']], blockers(b['ref'])          # 보관된 A 대신 새 판의 Y를 기다린다
    body = issues.get_issue(b['ref'])['body']
    if started:   # 실행 기록이 있으면 본문은 그대로
        assert body.startswith(f'{p} 계획서 v1의 Task 2.') and '옛 확인' in body
    else:
        assert body.startswith(f'{p} 계획서 v2의 Task 3.') and 'b.js, c.css' in body and '새 확인' in body and '표 열은 끝에' in body
    issues.decide(me, p, 'approve_notes', note='표 열은 끝에', plan_version=2)   # 같은 판 재승인은 다시 바꾸지 않는다
    assert blockers(b['ref']) == [y['ref']]
    assert sum('realigned_to_plan' in (e.get('data') or {}) for e in issues.get_issue(b['ref'])['events']) == 1

# 이미 망가진 데이터(고치기 전 승인)는 reconcile_plan_tasks가 바로잡고, 반복해도 한 번만 맞춘다.
p = issues.create_issue(me, 'TASK', '복구')['ref']
issues.post_plan(me, p, plan_deps([('A 작업', 'a.py', '옛', '없음'), ('B 화면', 'b.js', '옛', '1')]))
issues.decide(me, p, 'approve', plan_version=1)
a, b = issues.get_issue(p)['children']
issues.post_plan(me, p, plan_deps([('Y 발행', 'y.py', '발행', '없음'), ('B 화면', 'b.js', '새', '1')]))
with patch.object(issues, '_realign_tasks', lambda *args, **kw: 0):   # 고치기 전 동작으로 승인
    issues.decide(me, p, 'approve', plan_version=2)
assert blockers(b['ref']) == [a['ref']]
with db.connect() as conn:
    issues.reconcile_plan_tasks(conn)
    issues.reconcile_plan_tasks(conn)
y = next(t for t in issues.get_issue(p)['children'] if t['title'] == 'Y 발행')
assert blockers(b['ref']) == [y['ref']] and issues.get_issue(b['ref'])['body'].startswith(f'{p} 계획서 v2의 Task 2.')
assert sum('realigned_to_plan' in (e.get('data') or {}) for e in issues.get_issue(b['ref'])['events']) == 1
print('plan supersession: old history, active/manual/reused tasks, queue cancellation, repair, active guard, reused task realign OK')
