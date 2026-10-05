"""Claude 맡기기 대기열(DEV-43) — 가짜 claude로: 순차 실행·중복 방지·취소·재시작 후 이어감. 선행 대기 건너뛰기는 test_execute."""
import os, sys, tempfile, time
from pathlib import Path

os.environ["DEV_DATA_DIR"] = tempfile.mkdtemp()
os.environ["DEV_AUTO_REVIEW"] = "1"   # 옛 설정이 남아 있어도 자동 검토하지 않는다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import db, issues, jobs, review  # noqa: E402

fake = Path(tempfile.mkdtemp()) / "fake_claude.py"
fake.write_text("import time\ntime.sleep(0.5)\nprint('ok')\n", "utf-8")
review.command_for = lambda ref: [sys.executable, str(fake)]


def wait_idle():
    idle = 0
    for _ in range(100):
        time.sleep(0.2)
        idle = idle + 1 if not jobs.busy() and not jobs.list_jobs() else 0
        if idle >= 2:
            return
    raise AssertionError("대기열이 비지 않아요")


db.init()
me = {"kind": "human", "name": "admin"}
issues.create_project(me, "JQ", "q", "", "/nowhere/q")
for t in ("하나", "둘", "셋", "넷"):
    issues.create_issue(me, "JQ", t, type_ids=[1])  # 분류 호출이 없는 모의 검토용 초기 선택이다.

try:
    jobs.enqueue({"kind": "agent", "id": 1}, "JQ-1", "review")
    raise AssertionError("에이전트는 못 넣어요")
except issues.StoreError as e:
    assert e.status == 403

assert jobs.enqueue(me, "JQ-1", "review")["started"]          # 아무것도 안 돌면 바로 시작
assert issues.get_issue('JQ-1')['status'] == 'in_progress'   # 대기열에서 실제 착수하면 진행 중이다.
r2 = jobs.enqueue(me, "JQ-2", "review"); r3 = jobs.enqueue(me, "JQ-3", "review"); r4 = jobs.enqueue(me, "JQ-4", "review")
assert issues.get_issue("JQ-2")["status"] == "waiting"
assert (r2["position"], r3["position"], r4["position"]) == (1, 2, 3), (r2, r3, r4)
assert jobs.enqueue(me, "JQ-2", "review")["job_id"] == r2["job_id"] and len(jobs.list_jobs()) == 3   # 중복 클릭은 하나만
assert jobs.enqueue(me, "JQ-1", "review")["started"] and len(jobs.list_jobs()) == 3                # 도는 중인 것도 다시 안 넣음
jobs.cancel(me, r3["job_id"])                                                                      # 취소한 것은 안 돎
with db.connect() as c:
    assert c.execute('SELECT cancellation_reason,cancellation_event_id FROM jobs WHERE id=?', (r3['job_id'],)).fetchone()[:] == ('human_cancel', None)
assert issues.get_issue("JQ-3")["status"] == "backlog"
try:
    jobs.cancel(me, r3["job_id"]); raise AssertionError("두 번은 못 취소")
except issues.StoreError as e:
    assert e.status == 404
wait_idle()

runs = {ref: review.list_runs(ref) for ref in ("JQ-1", "JQ-2", "JQ-3", "JQ-4")}
assert [len(runs[r]) for r in runs] == [1, 1, 0, 1], runs
assert all(x[0]["status"] == "ok" for x in runs.values() if x)
assert runs["JQ-1"][0]["ended_at"] <= runs["JQ-2"][0]["started_at"] <= runs["JQ-2"][0]["ended_at"] <= runs["JQ-4"][0]["started_at"]   # 하나씩 차례로
with db.connect() as c:
    assert c.execute("SELECT run_id FROM jobs WHERE id=?", (r2["job_id"],)).fetchone()[0] == runs["JQ-2"][0]["id"]

# 재시작 — 줄은 DB에 남고, 서버가 다시 뜨면(db.init 뒤 펌프) 이어서 돈다
with db.connect() as c:
    c.execute("INSERT INTO jobs(issue_id, mode, actor, status, created_at) VALUES(?, 'review', 'human:admin', 'queued', ?)",
              (issues.get_issue("JQ-3")["id"], db.now_iso()))
db.init()
assert len(jobs.list_jobs()) == 1
jobs.pump(); wait_idle()
assert review.list_runs("JQ-3")[0]["status"] == "ok"

# 지워진 이슈의 대기 항목은 같이 사라진다
jobs.enqueue(me, "JQ-4", "review"); jobs.enqueue(me, "JQ-2", "review")
issues.delete_issue(me, "JQ-2")
wait_idle()

# 이슈 발행은 저장만 한다. 검토는 사람이 도구를 골라 직접 요청한다.
import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as A, auth  # noqa: E402
auth._client = httpx.AsyncClient(transport=httpx.MockTransport(
    lambda req: httpx.Response(200, json={"user": {"id": 1, "username": "admin", "role": "admin"}})))
H = {"X-Requested-With": "dev"}
fake.write_text("import time\ntime.sleep(2)\nprint('ok')\n", "utf-8")   # 첫 검토가 도는 동안 뒤 것이 줄에 남게
with TestClient(A.app) as c:
    c.cookies.set("ns_session", "adm")
    top = c.post("/api/issues", json={"project": "JQ", "title": "발행"}, headers=H).json()
    assert "job" not in top and top["ref"] == "JQ-5", top
    top2 = c.post("/api/issues", json={"project": "JQ", "title": "발행 둘"}, headers=H).json()
    assert "job" not in top2 and top2["ref"] == "JQ-6", top2
    task = c.post("/api/issues", json={"project": "JQ", "title": "Task", "parent": "JQ-5"}, headers=H).json()
    assert "job" not in task and task["parent_ref"] == "JQ-5"
    key = c.post("/api/agents", json={"name": "a"}, headers=H).json()["key"]
    c.cookies.clear()
    r = c.post("/api/issues", json={"project": "JQ", "title": "에이전트 발행"}, headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200 and "job" not in r.json(), r.text
    assert jobs.list_jobs() == []
    wait_idle()
    assert [len(review.list_runs(ref)) for ref in ("JQ-5", "JQ-6", task["ref"], r.json()["ref"])] == [0, 0, 0, 0]
    c.cookies.set("ns_session", "adm")
    assert c.post("/api/issues/JQ-5/review", headers=H).json()["started"]
    wait_idle()
    assert review.list_runs("JQ-5")[0]["provider"] == "claude"
# Waiting 소유권과 가짜 실행기로 전체 전환을 검사한다(실제 Git·유료 호출 없음).
from unittest.mock import patch
import execute
issues.create_project(me, 'WT', 'waiting', '', '/unused')
parent = issues.create_issue(me, 'WT', 'parent')
issues.post_plan(me, parent['ref'], 'approved plan')
issues.decide(me, parent['ref'], 'approve', plan_version=1)
task = issues.create_issue(me, 'WT', 'task', parent=parent['ref'])
ref = task['ref']

def queued(mode='review', provider='claude'):
    with patch.object(jobs, 'busy', return_value=True):
        result = jobs.enqueue(me, ref, mode, provider)
    assert issues.get_issue(ref)['status'] == 'waiting'
    return result['job_id']

# 중복 등록은 이벤트도 늘리지 않는다.
jid = queued()
events = len(issues.get_issue(ref)['events'])
assert queued() == jid and len(issues.get_issue(ref)['events']) == events
issues.set_status(me, ref, 'on_hold')
issues.set_status(me, ref, 'waiting')
jobs.cancel(me, jid)
assert issues.get_issue(ref)['status'] == 'waiting'  # 같은 상태로 돌아온 수동 변경도 보존한다.
issues.set_status(me, ref, 'changes_requested')

# 다른 대기 항목이 남아 있으면 마지막 취소까지 Waiting을 유지한다.
a, b = queued('execute'), queued('review')
jobs.cancel(me, a)
assert issues.get_issue(ref)['status'] == 'waiting'
jobs.cancel(me, b)
assert issues.get_issue(ref)['status'] == 'changes_requested'

# 다른 모드가 대기 중이어도 착수·실패 후 마지막 대기의 소유권을 보존한다.
a, b = queued('execute'), queued('review')
_, run_id = review.begin(me, issues.get_issue(ref), 'execute', 'claude')
assert issues.get_issue(ref)['status'] == 'in_progress'
with db.connect() as c:
    review.finish_codex_status(c, me, ref, run_id, 'failed', '실행')
    c.execute("UPDATE runs SET status='failed' WHERE id=?", (run_id,))
assert issues.get_issue(ref)['status'] == 'waiting'
jobs.cancel(me, b)
assert issues.get_issue(ref)['status'] == 'changes_requested'

# 재시작으로 착수가 끊겨도 같은 이슈의 다른 대기 항목은 유지한다.
a, b = queued('execute'), queued('review')
_, run_id = review.begin(me, issues.get_issue(ref), 'execute', 'claude')
db.init(); jobs.reconcile()
assert issues.get_issue(ref)['status'] == 'waiting'
assert [j['id'] for j in jobs.list_jobs()] == [b]
jobs.cancel(me, b)
assert issues.get_issue(ref)['status'] == 'changes_requested'

# 영구 실행 불가(승인이 뒤에 거절됨)는 skipped와 원래 상태로 돌아간다.
jid = queued('execute')
issues.decide(me, parent['ref'], 'reject', 'test rejection', plan_version=1)
jobs.pump()
assert issues.get_issue(ref)['status'] == 'closed' and not jobs.list_jobs()
issues.set_status(me, ref, 'changes_requested')
issues.set_status(me, parent['ref'], 'triage')
issues.decide(me, parent['ref'], 'approve', plan_version=1)

# 일시적 시작 실패는 Waiting에 남고, 실제 시작 실패도 재시도 가능한 대기로 돌아간다.
jid = queued('execute')
with patch.object(execute, 'start', side_effect=issues.StoreError('temporary', 409)):
    jobs.pump()
assert issues.get_issue(ref)['status'] == 'waiting'
jobs.cancel(me, jid)
jid = queued()
with patch.object(review.threading.Thread, 'start', side_effect=RuntimeError('thread failed')):
    jobs.pump()
assert issues.get_issue(ref)['status'] == 'waiting' and jobs.list_jobs()[0]['id'] == jid
jobs.cancel(me, jid)
assert issues.get_issue(ref)['status'] == 'changes_requested'

# Claude/Codex 모두 실제 착수와 실패·수동 상태 변경·재시작 복구를 검증한다.
for provider in ('claude', 'codex'):
    for ending in ('failed', 'manual', 'restart'):
        jid = queued('execute', provider)
        log, run_id = review.begin(me, issues.get_issue(ref), 'execute', provider)
        assert issues.get_issue(ref)['status'] == 'in_progress'
        if ending == 'manual':
            issues.set_status(me, ref, 'on_hold')
            issues.set_status(me, ref, 'in_progress')
        if ending == 'restart':
            db.init(); jobs.reconcile()
        else:
            with db.connect() as c:
                review.finish_codex_status(c, me, ref, run_id, 'failed', '실행')
                c.execute("UPDATE runs SET status='failed' WHERE id=?", (run_id,))
        assert issues.get_issue(ref)['status'] == ('in_progress' if ending == 'manual' else 'changes_requested')
        issues.set_status(me, ref, 'changes_requested')

# 선행 미완료 동안 Waiting을 유지하고 완료 후 시작할 수 있다.
dependency = issues.create_issue(me, 'WT', 'dependency', parent=parent['ref'])
with db.connect() as c:
    c.execute('INSERT INTO issue_deps VALUES(?,?)', (task['id'], dependency['id']))
jid = queued('execute')
jobs.pump()
assert jobs.list_jobs()[0]['id'] == jid and issues.get_issue(ref)['status'] == 'waiting'
issues.set_status(me, dependency['ref'], 'done')
def fake_start(actor, task_ref, provider):
    review.begin(actor, issues.get_issue(task_ref), 'execute', provider)
with patch.object(execute, 'start', side_effect=fake_start):
    jobs.pump()
assert issues.get_issue(ref)['status'] == 'in_progress' and not jobs.list_jobs()
with db.connect() as c:
    c.execute("UPDATE runs SET status='ok' WHERE status='running'")
issues.set_status(me, ref, 'in_review')

# 사람이 상태를 바꾸거나 종결·goal인 이슈는 펌프가 시작하지 않는다.
issues.set_status(me, ref, 'backlog')
jid = queued()
issues.set_status(me, ref, 'done')
with patch.object(review, 'start') as start:
    jobs.pump(); start.assert_not_called()
assert issues.get_issue(ref)['status'] == 'done'
goal = issues.create_issue(me, 'WT', 'goal', labels=['goal'])
try:
    jobs.enqueue(me, goal['ref'], 'review')
    raise AssertionError('goal must not enter waiting')
except issues.StoreError as e:
    assert e.status == 409
assert issues.get_issue(goal['ref'])['status'] == 'backlog' and not jobs.list_jobs()
print("OK")

# 실행 프로세스 종료 후에도 DB 후처리가 대기열을 막고, 완료 뒤 한 번만 착수한다.
import orchestrate, notify
pending_task = issues.create_issue(me, 'WT', 'postprocessing', parent=parent['ref'])
_, rid = review.begin(me, pending_task, 'execute')
with db.connect() as c:
    owner = c.execute("SELECT MAX(id) FROM events WHERE issue_id=? AND kind='status'", (pending_task['id'],)).fetchone()[0]
    c.execute("INSERT INTO execution_completion(run_id,issue_id,ref,actor,task_sha,auto_merge,phase,owner_event,cfg_json,worker_id,updated_at) VALUES(?,?,?,'human:admin','fake',1,'restart_requested',?,'{}',?,?)", (rid, pending_task['id'], pending_task['ref'], owner, orchestrate.PROCESS_ID, db.now_iso()))
db.init(); jobs.reconcile()
assert issues.get_issue(pending_task['ref'])['status'] == 'in_progress' and jobs.busy()
next_issue = issues.create_issue(me, 'WT', 'next review', type_ids=[1])
with patch.object(orchestrate, 'recover'), patch.object(review, 'start') as start:
    result = jobs.enqueue(me, next_issue['ref'], 'review')
    assert result['queued'] and '병합' in result['note']
    jobs.pump(); start.assert_not_called()
try:
    review.begin(me, next_issue, 'review')
    raise AssertionError('pending completion must block direct begin')
except issues.StoreError as e:
    assert e.status == 409
record = orchestrate.completion(pending_task['ref'])
with patch.object(notify, 'send') as sent, patch.object(orchestrate, 'promote_parent', return_value=False):
    assert orchestrate._finish(me, record, 'merged', 'fake health verified') == 'merged'
    assert orchestrate._finish(me, record, 'merged', 'duplicate') is None
    assert sent.call_count == 1
sequence = []
def next_start(actor, ref, provider):
    assert sequence and sequence[-1] == 'recover'
    assert not orchestrate.pending()
    sequence.append('start')
    review.begin(actor, issues.get_issue(ref), 'review', provider)
with patch.object(orchestrate, 'recover', side_effect=lambda: sequence.append('recover')), patch.object(review, 'start', side_effect=next_start) as start, patch.object(orchestrate, 'promote_parent', return_value=False):
    jobs.pump(); jobs.pump()
    assert start.call_count == 1 and not jobs.list_jobs()
    assert sequence == ['recover', 'start', 'recover']
print('OK completion queue boundary')

# 대기열 순서 이동(DEV-85) — 사람만, 화면에서 본 이웃과 한 칸 교환, 재시작 후 유지, 펌프도 같은 순서.
with db.connect() as c:
    c.execute("UPDATE runs SET status='ok' WHERE status='running'")
patch.object(orchestrate, 'recover').start()   # 앞 블록의 가짜 병합 기록이 실제 git을 부르지 않게
issues.create_project(me, 'MV', 'move', '', '/unused')
mv = [issues.create_issue(me, 'MV', t, type_ids=[1])['ref'] for t in ('a', 'b', 'c', 'd')]
with patch.object(jobs, 'busy', return_value=True):
    ja, jb, jc = (jobs.enqueue(me, r, 'review')['job_id'] for r in mv[:3])
order = lambda: [j['id'] for j in jobs.list_jobs()]
assert order() == [ja, jb, jc]
assert [(j['prev_id'], j['next_id']) for j in jobs.list_jobs()] == [(None, jb), (ja, jc), (jb, None)]
for args, status in ((({'kind': 'agent', 'id': 1}, jb, 'up', ja), 403), ((me, jb, 'left', ja), 400), ((me, jb, 'up', '1'), 400),
                     ((me, 999999, 'up', None), 404), ((me, jb, 'up', jc), 409), ((me, jb, 'down', None), 409), ((me, jc, 'up', None), 409)):
    try:
        jobs.move(*args); raise AssertionError(args)
    except issues.StoreError as e:
        assert e.status == status, (args, e.status)
assert order() == [ja, jb, jc]
assert jobs.move(me, ja, 'up', None)['moved'] is False and jobs.move(me, jc, 'down', None)['moved'] is False   # 끝은 그대로
assert order() == [ja, jb, jc]
r = jobs.move(me, jc, 'up', jb)
assert r['moved'] and [j['id'] for j in r['jobs']] == [ja, jc, jb]
jobs.move(me, jc, 'up', ja)
assert order() == [jc, ja, jb] and jobs.job_for(issues.get_issue(mv[2])['id'])['position'] == 1
db.init(); jobs.reconcile()
assert order() == [jc, ja, jb]   # 재시작 후에도 유지
with patch.object(jobs, 'busy', return_value=True):
    jd = jobs.enqueue(me, mv[3], 'review')['job_id']
assert order() == [jc, ja, jb, jd]   # 새 등록은 맨 뒤
jobs.cancel(me, ja)
try:
    jobs.move(me, ja, 'down', jb); raise AssertionError('취소한 항목은 못 옮겨요')
except issues.StoreError as e:
    assert e.status == 404
jobs.move(me, jb, 'up', jc)
assert order() == [jb, jc, jd]

# 선행이 막힌 실행을 맨 앞으로 옮겨도 건너뛰고 뒤 항목부터, 나머지는 바뀐 순서대로 돈다.
blocker = issues.create_issue(me, 'WT', 'blocker', parent=parent['ref'])
blocked = issues.create_issue(me, 'WT', 'blocked', parent=parent['ref'])
with db.connect() as c:
    c.execute('INSERT INTO issue_deps VALUES(?,?)', (blocked['id'], blocker['id']))
with patch.object(jobs, 'busy', return_value=True):
    jx = jobs.enqueue(me, blocked['ref'], 'execute')['job_id']
for neighbor in (jd, jc, jb):
    jobs.move(me, jx, 'up', neighbor)
assert order() == [jx, jb, jc, jd]
started = []
with patch.object(execute, 'start') as ex, patch.object(review, 'start', side_effect=lambda actor, ref, provider: started.append(ref)):
    for _ in range(3):
        jobs.pump()
    ex.assert_not_called()
assert started == [mv[1], mv[2], mv[3]], started
assert order() == [jx] and jobs.list_jobs()[0]['note']
jobs.cancel(me, jx)

# REST — 사람만, 잘못된 본문은 400.
with TestClient(A.app) as c:
    with patch.object(jobs, 'busy', return_value=True):
        ids = [jobs.enqueue(me, r, 'review')['job_id'] for r in mv[:2]]
    c.cookies.set('ns_session', 'adm')
    assert c.post(f'/api/jobs/{ids[1]}/move', json={'direction': 'up', 'neighbor_id': ids[0]}, headers=H).json()['moved']
    assert order() == [ids[1], ids[0]]
    assert c.post(f'/api/jobs/{ids[1]}/move', json={'direction': 'down', 'neighbor_id': ids[1]}, headers=H).status_code == 409
    assert c.post(f'/api/jobs/{ids[1]}/move', json=[1], headers=H).status_code == 400
    c.cookies.clear()
    assert c.post(f'/api/jobs/{ids[1]}/move', json={'direction': 'down', 'neighbor_id': ids[0]}, headers={'Authorization': f'Bearer {key}'}).status_code == 403
    assert order() == [ids[1], ids[0]]
    for i in ids:
        jobs.cancel(me, i)
print('OK move')
