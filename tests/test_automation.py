"""임시 DB에서 자동화 설정의 영속성·검증·관리자 경계를 확인한다."""
import os
import sys
import tempfile
import sqlite3
import json
from pathlib import Path

os.environ["DEV_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import httpx
from fastapi.testclient import TestClient
import app as A, auth, auto_settings, automation, config, db, issues, jobs
automation.provider_available = lambda provider: False

# 운영 작업이나 타이머를 시작하지 않는다.
jobs.start_timer = lambda: None
auth._client = httpx.AsyncClient(transport=httpx.MockTransport(
    lambda req: httpx.Response(200, json={"user": {"id": 1, "username": "admin", "role": "admin"}})))
admin = {"kind": "human", "name": "admin"}

# 이전 판 DB의 실제 데이터를 남긴 뒤 새 migration만 적용한다.
with sqlite3.connect(config.DB_PATH) as c:
    legacy_version = next(i for i, sql in enumerate(db.MIGRATIONS) if "ALTER TABLE jobs ADD COLUMN cancellation_reason" in sql)
    for i, sql in enumerate(db.MIGRATIONS[:legacy_version], 1):
        c.executescript("BEGIN;" + sql + f"PRAGMA user_version={i};COMMIT;")
issues.create_project(admin, "DEV", "기존 프로젝트", description="제품 설명")
legacy = issues.create_issue(admin, "DEV", "기존 이슈")
with db.connect() as c:
    c.execute("INSERT INTO jobs(issue_id,mode,status,actor,created_at,provider) VALUES(?,'review','cancelled','human:admin',?,'codex')", (legacy['id'], db.now_iso()))
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    before = {t: [tuple(r) for r in c.execute(f'SELECT * FROM "{t}"')] for t in tables}
assert db.init() == len(db.MIGRATIONS)
with db.connect() as c:
    for table in tables:
        added = (None, None) if table == 'jobs' else (0,) if table == 'project_auto_settings' else ('',) if table == 'runs' else ()
        assert [r + added for r in before[table]] == [tuple(r) for r in c.execute(f'SELECT * FROM "{table}"')], table
    job = c.execute('SELECT * FROM jobs WHERE issue_id=?', (legacy['id'],)).fetchone()
    assert job['source'] == 'manual' and job['provider'] == 'codex'
    assert job['delegation_id'] is None and job['approval_version'] is None
    assert c.execute('PRAGMA foreign_key_check').fetchall() == []
issues.create_project(admin, "NS", "격리 프로젝트")

with TestClient(A.app) as c:
    c.cookies.set("ns_session", "admin")
    def call(method, path="/api/projects/dev/auto-settings", **kw):
        return c.request(method, path, headers={"X-Requested-With": "dev"}, **kw)

    defaults = call("GET").json()
    assert all(defaults[k] is False for k in ("auto_review", "auto_execute", "auto_approve"))
    assert defaults["provider_order"] == ["claude", "codex"]
    assert defaults["auto_approve_available"] and not defaults["auto_approve_disabled_reason"]
    assert defaults["updated_by"] is None
    assert call("GET", "/api/projects/missing/auto-settings").status_code == 404
    assert call("PATCH", "/api/projects/missing/auto-settings", json={"auto_review": True}).status_code == 404
    r = call("PATCH", json={"auto_review": True, "provider_order": ["codex", "claude"]})
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["auto_review"] and not saved["auto_execute"] and saved["updated_by"] == "human:admin"
    assert auto_settings.get_settings("NS") == {**defaults, "project_key": "NS"}
    for bad in ({"auto_review": 1}, {"auto_execute": "true"}, {"auto_approve": None},
                {"unknown": True},
                *({"provider_order": v} for v in ([], ["claude"], ["claude", "claude"],
                    ["codex", "other"], "claude", None, [1, 2]))):
        assert call("PATCH", json=bad).status_code == 400, bad
        assert call("GET").json() == saved
    assert call("PATCH", json=[]).status_code == 400
    assert call("PATCH", content="{").status_code == 400
    assert call("PATCH", json={}).json() == saved
    agent, key = auth.create_agent("테스트 에이전트")
    c.cookies.clear()
    assert c.get("/api/projects/dev/auto-settings").status_code == 401
    for method in ("GET", "PATCH"):
        r = c.request(method, "/api/projects/dev/auto-settings",
                      headers={"Authorization": f"Bearer {key}"}, json={"auto_execute": True})
        assert r.status_code == (200 if method == "GET" else 403)
    try:
        auto_settings.update_settings({"kind": "agent", "id": agent["id"]}, "DEV", {"auto_review": False})
        raise AssertionError("에이전트 변경 허용")
    except issues.StoreError as e:
        assert e.status == 403
    c.cookies.set("ns_session", "admin")
    assert c.patch("/api/projects/dev/auto-settings", json={"auto_review": False}).status_code == 403
    final = call("PATCH", json={"auto_execute": True, "auto_approve": False}).json()
    assert final["auto_review"] and final["auto_execute"]
    assert final["provider_order"] == ["codex", "claude"]
    auth._auth_cache.clear()
    auth._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"user": {"username": "member", "role": "user"}})))
    assert call("PATCH", json={"auto_review": False}).status_code == 403

db.init()
assert auto_settings.get_settings("DEV") == final
assert issues.get_issue(legacy["ref"])["title"] == "기존 이슈"
with db.connect() as c:
    events = c.execute("SELECT * FROM project_auto_settings_events ORDER BY id").fetchall()
    assert len(events) == 2
    assert all(e["actor"] == "human:admin" for e in events)
    assert json.loads(events[0]["before_json"]) == defaults
    assert json.loads(events[-1]["after_json"]) == final
print("OK — 자동화 설정 기본값·격리·영속성·입력 검증·권한·감사·기존 migration 보존")

# 가짜 provider로만 자동 실행하며 운영 CLI·Git 작업은 시작하지 않는다.
import review, execute
auto_settings.update_settings(admin, 'DEV', {'auto_review': False, 'auto_execute': False})
jobs._threads = 1
issues.create_project(admin, 'AUTO', '자동 검사', '', '/fake')
one = issues.create_issue(admin, 'AUTO', '첫 검토')
two = issues.create_issue(admin, 'AUTO', '둘째 검토')
goal = issues.create_issue(admin, 'AUTO', '목표', labels=['goal'])
auto_settings.update_settings(admin, 'AUTO', {'auto_review': True, 'provider_order': ['codex','claude']})
automation.provider_available = lambda provider: provider == 'claude'
jobs.pump()
auto_jobs = [j for j in jobs.list_jobs() if j['source'] == 'auto']
assert [j['ref'] for j in auto_jobs] == [one['ref'], two['ref']]
assert all(j['provider'] == 'claude' and j['delegation_id'] and '/delegation/' in j['actor'] for j in auto_jobs)
jobs.pump(); db.init(); jobs.pump()
assert len([j for j in jobs.list_jobs() if j['source'] == 'auto']) == 2
manual = issues.create_issue(admin, 'AUTO', '수동')
jobs.enqueue(admin, manual['ref'], 'review', 'codex')
auto_settings.update_settings(admin, 'AUTO', {'auto_review': False})
jobs.pump()
assert [j['ref'] for j in jobs.list_jobs()] == [manual['ref']]
assert issues.get_issue(one['ref'])['status'] == 'backlog'
jobs.cancel(admin, jobs.list_jobs()[0]['id'])
auto_settings.update_settings(admin, 'AUTO', {'auto_review': True})
jobs.pump()
assert not [j for j in jobs.list_jobs() if j['ref'] in (one['ref'],two['ref'])]
new = issues.create_issue(admin, 'AUTO', '착수')
starts = []
def fake_start(actor, ref, provider):
    starts.append((ref, provider))
    review.begin(actor, issues.get_issue(ref), 'review', provider)
review.start = fake_start
jobs._threads = 0
jobs.pump(); jobs.pump()
assert starts == [(new['ref'], 'claude')]
db.init(); jobs.pump()
assert len(starts) == 1
assert issues.get_issue(goal['ref'])['status'] == 'backlog'
print('OK — Auto 선정·provider fallback·중복·재시작·OFF 취소·수동 보존·실패 재시도 차단')

jobs._threads = 1
auto_settings.update_settings(admin, 'AUTO', {'auto_review': False, 'auto_execute': True})
parent = issues.create_issue(admin, 'AUTO', '승인 부모')
issues.post_plan(admin, parent['ref'], '## Tasks\n1. 먼저 | 파일: server/jobs.py\n2. 다음 | 파일: server/jobs.py | 선행: 1')
issues.decide(admin, parent['ref'], 'approve', plan_version=1)
children = issues.get_issue(parent['ref'])['children']
first, second = [ch['ref'] for ch in children]
unapproved = issues.create_issue(admin, 'AUTO', '미승인 부모')
unapproved_task = issues.create_issue(admin, 'AUTO', '미승인 Task', parent=unapproved['ref'])
jobs.pump()
assert [j['ref'] for j in jobs.list_jobs()] == [first]
issues.post_plan(admin, parent['ref'], '새 계획')
jobs.pump()
assert jobs.list_jobs() == []
issues.decide(admin, parent['ref'], 'approve', plan_version=2)
issues.set_status(admin, first, 'done')
jobs.pump()
assert [j['ref'] for j in jobs.list_jobs()] == [second]
assert jobs.list_jobs()[0]['approval_version'] == 2
issues.set_status(admin, second, 'changes_requested')
jobs.pump()
assert jobs.list_jobs() == []
assert issues.get_issue(second)['status'] == 'changes_requested'
assert issues.get_issue(unapproved_task['ref'])['status'] == 'backlog'
print('OK — 미승인·stale·선행 미완료·상태 변경 차단과 선행 완료 후 등록')

# 준비 도중 OFF가 저장되어도 실행 기록 생성 트랜잭션에서 막는다.
late = issues.create_issue(admin, 'AUTO', 'OFF 경합')
auto_settings.update_settings(admin, 'AUTO', {'auto_review': True})
def off_before_begin(actor, ref, provider):
    auto_settings.update_settings(admin, 'AUTO', {'auto_review': False})
    review.begin(actor, issues.get_issue(ref), 'review', provider)
review.start = off_before_begin
jobs._threads = 0
jobs.pump()
assert not review.list_runs(late['ref'])
assert issues.get_issue(late['ref'])['status'] == 'backlog'
assert not jobs.list_jobs()
print('OK — 착수 준비 중 OFF 경합 차단')

from unittest.mock import patch
import threading
import orchestrate

jobs._threads = 1
issues.create_project(admin, 'RESULT', '결과 승인', '', '/fake')
auto_settings.update_settings(admin, 'RESULT', {'auto_approve': True})

def result_task(state='unmerged', run_status='ok', labels=None, receipt=True):
    parent = issues.create_issue(admin, 'RESULT', '사람 승인 부모')
    issues.post_plan(admin, parent['ref'], '## Tasks\n1. 먼저 | 파일: server/db.py\n2. 다음 | 파일: server/issues.py | 선행: 1')
    issues.decide(admin, parent['ref'], 'approve_notes', '사람 조건', plan_version=1)
    first, second = [ch['ref'] for ch in issues.get_issue(parent['ref'])['children']]
    if labels:
        issues.update_issue(admin, first, {'labels':labels})
    with db.connect() as c:
        rid = c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at,ended_at) VALUES(?,'execute',?,'human:admin',?,?)",
                        (issues._find(c, first)['id'], run_status, db.now_iso(), None if run_status == 'running' else db.now_iso())).lastrowid
        issues._set_status(c, admin, issues._find(c, first), 'in_progress', data={'run_id':rid})
    issues.link_commit(admin, first, 'a' * 40)
    issues.set_status(admin, first, 'in_review')
    if receipt:
        with db.connect() as c:
            iid = issues._find(c, first)['id']
            commit = c.execute("SELECT MAX(id) FROM events WHERE issue_id=? AND kind='commit'", (iid,)).fetchone()[0]
            status = c.execute("SELECT MAX(id) FROM events WHERE issue_id=? AND kind='status'", (iid,)).fetchone()[0]
            c.execute('INSERT INTO task_execution_results VALUES(?,1,?,?,?)', (rid,state,commit,status))
    return parent['ref'], first, second, rid

with patch.object(orchestrate, 'settings', return_value={'auto_merge':False}):
    # 자동 Plan 승인 경로가 사라졌고 수동 승인 기록은 보존된다.
    unreviewed = issues.create_issue(admin, 'RESULT', '자동 승인하지 않는 Plan')
    issues.post_plan(admin, unreviewed['ref'], '## Tasks\n1. 작업 | 파일: server/db.py')
    parent, first, second, rid = result_task()
    automation.sync(); automation.sync(); db.init(); automation.sync()
    assert issues.get_issue(unreviewed['ref'])['approval'] is None
    assert issues.get_issue(unreviewed['ref'])['children'] == []
    assert issues.get_issue(first)['status'] == 'done'
    assert issues.get_issue(second)['status'] == 'backlog'
    assert issues.get_issue(parent)['status'] != 'done'
    completed = [e for e in issues.get_issue(first)['events'] if e['data'].get('to') == 'done']
    assert len(completed) == 1
    assert completed[0]['data']['run_id'] == rid and completed[0]['data']['plan_version'] == 1
    assert completed[0]['data']['delegated_by'] == 'human:admin'
    assert not [j for j in jobs.list_jobs() if j['ref'] == second]
    auto_settings.update_settings(admin, 'RESULT', {'auto_execute':True})
    automation.sync()
    assert [j for j in jobs.list_jobs() if j['ref'] == second]
    auto_settings.update_settings(admin, 'RESULT', {'auto_execute':False})
    automation.sync()

    # 실행 결과·부모 승인·상태·후손 조건을 각각 검사한다.
    blocked = []
    for status in ('failed','timeout','orphaned','running'):
        blocked.append(result_task(run_status=status)[1])
    for state in ('processing','blocked'):
        blocked.append(result_task(state=state)[1])
    blocked.append(result_task(labels=['goal'])[1])
    p, task, _, _ = result_task()
    issues.decide(admin, p, 'approve_notes', '실행 뒤 바뀐 사람 조건', plan_version=1)
    blocked.append(task)
    p, task, _, _ = result_task()
    issues.post_plan(admin, p, '새 계획')
    blocked.append(task)
    p, task, _, _ = result_task()
    issues.decide(admin, p, 'reject', '철회', plan_version=1)
    blocked.append(task)
    _, task, _, _ = result_task()
    issues.create_issue(admin, 'RESULT', '미완료 후손', parent=task)
    blocked.append(task)
    _, task, _, _ = result_task()
    issues.set_status(admin, task, 'on_hold')
    issues.set_status(admin, task, 'in_review')
    blocked.append(task)
    _, task, _, _ = result_task()
    issues.link_commit(admin, task, 'b' * 40)
    blocked.append(task)
    _, task, _, _ = result_task()
    with db.connect() as c:
        c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at,ended_at) VALUES(?,'execute','failed','human:admin',?,?)", (issues._find(c,task)['id'],db.now_iso(),db.now_iso()))
    blocked.append(task)
    automation.sync()
    assert all(issues.get_issue(t)['status'] != 'done' for t in blocked)
    with db.connect() as c:
        assert not c.execute('PRAGMA foreign_key_check').fetchall()

    _, off, _, _ = result_task()
    auto_settings.update_settings(admin, 'RESULT', {'auto_approve':False})
    automation.sync()
    assert issues.get_issue(off)['status'] == 'in_review'
    auto_settings.update_settings(admin, 'RESULT', {'auto_approve':True})
    errors = []
    def concurrent_sync():
        try: automation.sync()
        except Exception as e: errors.append(e)
    threads = [threading.Thread(target=concurrent_sync) for _ in range(2)]
    for t in threads: t.start()
    for t in threads: t.join(10)
    assert not errors and all(not t.is_alive() for t in threads)
    assert len([e for e in issues.get_issue(off)['events'] if e['data'].get('to') == 'done']) == 1

    # 이전에 완료한 실행도 병합 OFF에서는 같은 조건으로 승인한다.
    _, old, _, _ = result_task(receipt=False)
    automation.sync()
    assert issues.get_issue(old)['status'] == 'done'

    # 실제 쓰기 잠금과 OFF·수동 변경을 겹쳐 오래된 관측으로 완료하지 않는다.
    def race_change(module, name, action):
        locked, release = threading.Event(), threading.Event()
        original = getattr(module, name)
        owner = []
        errors = []
        def pause(*args, **kwargs):
            value = original(*args, **kwargs)
            if threading.get_ident() == owner[0] and not locked.is_set():
                locked.set()
                assert release.wait(5)
            return value
        def writer():
            owner.append(threading.get_ident())
            try: action()
            except Exception as e: errors.append(e)
        def reader():
            try: automation.sync()
            except Exception as e: errors.append(e)
        with patch.object(module, name, pause):
            w = threading.Thread(target=writer); w.start()
            try:
                assert locked.wait(5)
                r = threading.Thread(target=reader); r.start()
            finally:
                release.set()
            w.join(10); r.join(10)
            assert not errors and not w.is_alive() and not r.is_alive(), errors
    _, race_off, _, _ = result_task()
    race_change(auto_settings, '_settings', lambda: auto_settings.update_settings(admin, 'RESULT', {'auto_approve':False}))
    assert issues.get_issue(race_off)['status'] == 'in_review'
    auto_settings.update_settings(admin, 'RESULT', {'auto_approve':True})
    _, race_status, _, _ = result_task()
    race_change(issues, '_event', lambda: issues.set_status(admin, race_status, 'on_hold'))
    assert issues.get_issue(race_status)['status'] == 'on_hold'
    race_parent, race_plan, _, _ = result_task()
    race_change(issues, '_event', lambda: issues.post_plan(admin, race_parent, '새 판'))
    assert issues.get_issue(race_plan)['status'] == 'in_review'

    _, isolated, _, _ = result_task()
    issues.create_project(admin, 'ISOLATED', '다른 프로젝트')
    with db.connect() as c:
        pid = c.execute("SELECT id FROM projects WHERE key='ISOLATED'").fetchone()[0]
        c.execute('UPDATE issues SET project_id=? WHERE id=?', (pid,issues._find(c,isolated)['id']))
    automation.sync()
    with db.connect() as c:
        assert c.execute('SELECT status FROM issues WHERE project_id=?', (pid,)).fetchone()[0] == 'in_review'
    _, archived, _, _ = result_task()
    with db.connect() as c:
        c.execute("UPDATE projects SET archived=1 WHERE key='RESULT'")
    automation.sync()
    assert issues.get_issue(archived)['status'] == 'in_review'
    with db.connect() as c:
        c.execute("UPDATE projects SET archived=0 WHERE key='RESULT'")

with patch.object(orchestrate, 'settings', return_value={'auto_merge':True}):
    pending = [result_task(state=s)[1] for s in ('processing','blocked','unmerged')]
    _, merged, _, _ = result_task(state='merged')
    automation.sync()
    assert issues.get_issue(merged)['status'] == 'done'
    assert all(issues.get_issue(t)['status'] == 'in_review' for t in pending)

# 실행→병합 처리 종료 기록을 실제 wrapper로 검사한다(mock만 사용한다).
with patch.object(orchestrate, 'settings', return_value={'auto_merge':True}):
    _, wrapped, _, wrapped_run = result_task(state='processing')
    def finish_wrapped(actor, ref):
        with db.connect() as c:
            c.execute("UPDATE task_execution_results SET state='merged' WHERE run_id=?", (wrapped_run,))
        return 'merged'
    with patch.object(review, 'run_headless', return_value='ok'), patch.object(orchestrate, 'handle', side_effect=finish_wrapped), patch.object(orchestrate, 'promote_parent'):
        execute._run_then_merge(admin, wrapped, None, wrapped_run)
    automation.sync()
    assert issues.get_issue(wrapped)['status'] == 'done'
    _, failed_merge, _, failed_run = result_task(state='processing')
    with patch.object(review, 'run_headless', return_value='ok'), patch.object(orchestrate, 'handle', side_effect=RuntimeError('deploy failure')):
        execute._run_then_merge(admin, failed_merge, None, failed_run)
    automation.sync()
    assert issues.get_issue(failed_merge)['status'] == 'in_review'
print('OK — 단일 Task 결과 승인·감사·순차 등록·실패/경합/병합/후손 차단·Plan 수동 승인 보존')

# 이전 DB를 별도로 만들어 ON 의미 전환과 모든 기존 데이터 보존을 검사한다.
old_path = Path(tempfile.mkdtemp()) / 'legacy.sqlite3'
with patch.object(config, 'DB_PATH', old_path):
    with sqlite3.connect(old_path) as c:
        result_version = next(i for i, sql in enumerate(db.MIGRATIONS) if "CREATE TABLE task_execution_results" in sql)
        for i, sql in enumerate(db.MIGRATIONS[:result_version], 1):
            c.executescript('BEGIN;' + sql + f'PRAGMA user_version={i};COMMIT;')
    issues.create_project(admin, 'MIG', '이전 설정')
    old_issue = issues.create_issue(admin, 'MIG', '보존하는 승인')
    issues.post_plan(admin, old_issue['ref'], '## Tasks\n1. 기존 Task | 파일: server/db.py')
    issues.decide(admin, old_issue['ref'], 'approve_notes', '기존 사람 조건', plan_version=1)
    with db.connect() as c:
        c.execute("INSERT INTO project_auto_settings VALUES(1,1,1,1,'[\"codex\",\"claude\"]','human:admin',?)", (db.now_iso(),))
    with db.connect() as c:
        names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        snapshot = {t:[tuple(r) for r in c.execute(f'SELECT * FROM "{t}"')] for t in names}
    db.init(); db.init()
    with db.connect() as c:
        for table in names:
            after = [tuple(r) for r in c.execute(f'SELECT * FROM "{table}"')]
            if table == 'project_auto_settings':
                assert len(after) == len(snapshot[table])
                assert after[0][0:3] == snapshot[table][0][0:3]
                assert after[0][3] == 0
                assert after[0][4:] == snapshot[table][0][4:] + (0,)
            elif table == 'project_auto_settings_events':
                assert after[:-1] == snapshot[table] and len(after) == len(snapshot[table]) + 1
                assert after[-1][2] == 'system:migration/task-result-approval'
                assert json.loads(after[-1][4])['auto_approve'] is False
            else:
                assert after == [r + ('',) for r in snapshot[table]] if table == 'runs' else after == snapshot[table], table
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
    assert auto_settings.get_settings('MIG')['auto_approve'] is False
print('OK — 기존 ON 초기화·전환 감사·다른 설정/승인/Task/이력 보존·migration 재실행')

agent_actor = {"kind": "agent", "id": agent["id"]}
def run_sync():
    try: automation.sync()
    except Exception as error: errors.append(error)

# DEV-82: 실행 OFF 이력만 제외하고 매 등록 시 현재 조건을 다시 검사한다.
issues.create_project(admin, 'REC', '재등록', '', '/fake')
p = issues.create_issue(admin, 'REC', 'parent')
issues.post_plan(admin, p['ref'], 'plan')
issues.decide(admin, p['ref'], 'approve', plan_version=1)
auto_settings.update_settings(admin, 'REC', {'auto_execute': True})

def rec_task():
    return issues.create_issue(admin, 'REC', 'task', parent=p['ref'])

def rec_job(task):
    return next(j for j in jobs.list_jobs() if j['issue_id'] == task['id'])

def toggle_off():
    auto_settings.update_settings(admin, 'REC', {'auto_execute': False})
    automation.sync()

def toggle_on():
    auto_settings.update_settings(admin, 'REC', {'auto_execute': True})
    automation.sync()

t = rec_task()
automation.sync()
a = rec_job(t)
toggle_off()
with db.connect() as c:
    old = dict(c.execute('SELECT * FROM jobs WHERE id=?', (a['id'],)).fetchone())
assert old['cancellation_reason'] == 'auto_execute_off' and old['cancellation_event_id']
assert old['started_at'] is None and old['run_id'] is None
toggle_on()
b = rec_job(t)
assert b['id'] != a['id'] and b['delegation_id'] != a['delegation_id']
for _ in range(3):
    toggle_off(); toggle_on(); db.init(); jobs.reconcile(); automation.sync()
assert len([j for j in jobs.list_jobs() if j['issue_id'] == t['id']]) == 1
errors = []
threads = [threading.Thread(target=run_sync) for _ in range(3)]
for thread in threads: thread.start()
for thread in threads: thread.join(10)
assert not errors and all(not thread.is_alive() for thread in threads)
assert len([j for j in jobs.list_jobs() if j['issue_id'] == t['id']]) == 1
jobs.cancel(admin, rec_job(t)['id'])
toggle_off(); toggle_on()
assert not any(j['issue_id'] == t['id'] for j in jobs.list_jobs())
with db.connect() as c:
    assert c.execute('SELECT cancellation_reason FROM jobs WHERE issue_id=? ORDER BY id DESC', (t['id'],)).fetchone()[0] == 'human_cancel'

# OFF와 상태/claim/goal/승인/선행 변경이 함께 있으면 재등록 가능한 사유로 기록하지 않는다.
for change in ('claim', 'goal', 'status', 'dependency', 'approval'):
    t = rec_task(); automation.sync(); a = rec_job(t)
    if change == 'claim':
        issues.claim(agent_actor, t['ref'])
    elif change == 'goal':
        with db.connect() as c: c.execute("UPDATE issues SET labels_json='[\"goal\"]' WHERE id=?", (t['id'],))
    elif change == 'status':
        issues.set_status(admin, t['ref'], 'on_hold')
    elif change == 'dependency':
        dep = rec_task()
        with db.connect() as c: c.execute('INSERT INTO issue_deps VALUES(?,?)', (t['id'], dep['id']))
    else:
        issues.post_plan(admin, p['ref'], 'new plan')
    toggle_off()
    with db.connect() as c:
        assert c.execute('SELECT cancellation_reason FROM jobs WHERE id=?', (a['id'],)).fetchone()[0] == 'conditions_changed', change
    toggle_on()
    assert not any(j['issue_id'] == t['id'] for j in jobs.list_jobs())

issues.decide(admin, p['ref'], 'approve', plan_version=2)
# 착수 직전 OFF 경합: DB gate가 차단하고 실행 이력 없이 재등록한다.
t = rec_task(); automation.sync(); a = rec_job(t)
def execute_off(actor, ref, provider):
    auto_settings.update_settings(admin, 'REC', {'auto_execute': False})
    review.begin(actor, issues.get_issue(ref), 'execute', provider)
with patch.object(execute, 'start', side_effect=execute_off):
    jobs._threads = 0; jobs.pump(); jobs._threads = 1
with db.connect() as c:
    assert not c.execute('SELECT 1 FROM runs WHERE issue_id=?', (t['id'],)).fetchone()
    assert c.execute('SELECT cancellation_reason FROM jobs WHERE id=?', (a['id'],)).fetchone()[0] == 'auto_execute_off'
toggle_on()
assert rec_job(t)['id'] != a['id']

# 준비 중 sync가 취소까지 마친 경우에도 오래된 위임은 실행 gate를 우회하지 않는다.
t = rec_task(); automation.sync(); a = rec_job(t)
def sync_off_before_begin(actor, ref, provider):
    toggle_off()
    review.begin(actor, issues.get_issue(ref), 'execute', provider)
with patch.object(execute, 'start', side_effect=sync_off_before_begin):
    jobs._threads = 0; jobs.pump(); jobs._threads = 1
with db.connect() as c:
    assert not c.execute('SELECT 1 FROM runs WHERE issue_id=?', (t['id'],)).fetchone()
    assert c.execute('SELECT cancellation_reason FROM jobs WHERE id=?', (a['id'],)).fetchone()[0] == 'auto_execute_off'
toggle_on()
assert rec_job(t)['id'] != a['id']

# 다양한 차단 이력과 OFF 이력이 섞여도 자동 재시도하지 않는다.
for status in ('skipped', 'started', 'cancelled'):
    t = rec_task(); automation.sync(); a = rec_job(t); toggle_off()
    with db.connect() as c:
        c.execute('INSERT INTO jobs(issue_id,mode,actor,status,created_at) VALUES(?,?,?,?,?)', (t['id'], 'execute', 'human:admin', status, db.now_iso()))
    toggle_on()
    assert not any(j['issue_id'] == t['id'] for j in jobs.list_jobs())
for status in ('failed', 'orphaned'):
    t = rec_task(); automation.sync(); toggle_off()
    with db.connect() as c:
        c.execute('INSERT INTO runs(issue_id,mode,status,actor,started_at) VALUES(?,?,?,?,?)', (t['id'], 'execute', status, 'human:admin', db.now_iso()))
    toggle_on()
    assert not any(j['issue_id'] == t['id'] for j in jobs.list_jobs())

# legacy 복구는 명시한 대상과 OFF/복구 증거만 허용하고 dry-run은 쓰지 않는다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
from recover_auto_off_jobs import recover
with patch.object(db, 'now_iso', return_value='2030-01-01T00:00:00+00:00'):
    t = rec_task(); automation.sync(); a = rec_job(t)
with patch.object(db, 'now_iso', return_value='2030-01-01T00:00:01+00:00'):
    toggle_off()
with db.connect() as c:
    c.execute('UPDATE jobs SET cancellation_reason=NULL,cancellation_event_id=NULL WHERE id=?', (a['id'],))
with db.connect() as c:
    before = [tuple(r) for r in c.execute('SELECT * FROM jobs')]
    assert recover(c, [a['id']])[0]['applied'] is False
    assert [tuple(r) for r in c.execute('SELECT * FROM jobs')] == before
with db.connect() as c: result = recover(c, [a['id']], True)
with db.connect() as c: assert recover(c, [a['id']], True) == result
for field, value in [('source', 'manual'), ('started_at', '2030'), ('note', 'manual'), ('cancellation_reason', 'human_cancel')]:
    with db.connect() as c:
        original = c.execute(f'SELECT {field} FROM jobs WHERE id=?', (a['id'],)).fetchone()[0]
        c.execute(f'UPDATE jobs SET {field}=? WHERE id=?', (value, a['id']))
    try:
        with db.connect() as c: recover(c, [a['id']], True)
        raise AssertionError(field)
    except ValueError: pass
    with db.connect() as c: c.execute(f'UPDATE jobs SET {field}=? WHERE id=?', (original, a['id']))
toggle_on()
assert rec_job(t)['id'] != a['id']
print('OK — DEV-82 OFF→ON·반복·재시작·동시 등록·혼합 이력·조건 변경·실행 gate·legacy dry-run/반복/거부')

# 복구 증거가 없거나 여러 OFF 이벤트가 겹치면 적용하지 않고 전체 요청을 롤백한다.
with db.connect() as c:
    c.execute('UPDATE jobs SET cancellation_reason=NULL,cancellation_event_id=NULL WHERE id=?', (a['id'],))
    event_id = result[0]['event_id']
    event = dict(c.execute('SELECT * FROM project_auto_settings_events WHERE id=?', (event_id,)).fetchone())
    c.execute("UPDATE project_auto_settings_events SET before_json='{}' WHERE id=?", (event_id,))
try:
    with db.connect() as c: recover(c, [a['id']], True)
    raise AssertionError('OFF 증거 누락')
except ValueError: pass
with db.connect() as c:
    c.execute('UPDATE project_auto_settings_events SET before_json=? WHERE id=?', (event['before_json'], event_id))
    duplicate = c.execute('INSERT INTO project_auto_settings_events(project_id,actor,before_json,after_json,created_at,plan_id_floor) VALUES(?,?,?,?,?,?)',
                          tuple(event[k] for k in ('project_id', 'actor', 'before_json', 'after_json', 'created_at', 'plan_id_floor'))).lastrowid
try:
    with db.connect() as c: recover(c, [a['id']], True)
    raise AssertionError('모호한 OFF 증거')
except ValueError: pass
with db.connect() as c: c.execute('DELETE FROM project_auto_settings_events WHERE id=?', (duplicate,))
try:
    with db.connect() as c: recover(c, [a['id'], -999], True)
    raise AssertionError('부분 적용 금지')
except ValueError: pass
with db.connect() as c:
    assert c.execute('SELECT cancellation_reason FROM jobs WHERE id=?', (a['id'],)).fetchone()[0] is None
    c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at) VALUES(?,'execute','failed','human:admin',?)", (a['issue_id'], db.now_iso()))
try:
    with db.connect() as c: recover(c, [a['id']], True)
    raise AssertionError('실행 이력 복구 금지')
except ValueError: pass
print('OK — legacy 증거 부족·모호함·부분 적용·실행 이력 거부')


# 자동 승인 자체는 provider 호출 없이 완료된 검토에만 적용한다.
from unittest.mock import patch
import threading
jobs._threads = 1
auto_settings.update_settings(admin, 'AUTO', {'auto_review': False, 'auto_execute': False})
issues.create_project(admin, 'PLAN', '계획 승인', '', str(Path(os.environ['DEV_DATA_DIR'])))
agent_actor = {'kind': 'agent', 'id': agent['id']}
valid_plan = ('## Tasks\n'
              '1. 먼저 | 파일: server/db.py | 확인: 임시 DB 검사 | 선행: 없음\n'
              '2. 다음 | 파일: server/issues.py | 확인: 권한 검사 | 선행: 1')


def reviewed(body=valid_plan, status='ok', labels=None, project='PLAN'):
    issue = issues.create_issue(admin, project, '검토 계획', labels=labels or [])
    with db.connect() as c:
        rid = c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at) VALUES(?,'review','running','human:admin',?)",
                        (issue['id'], db.now_iso())).lastrowid
    issues.post_plan(agent_actor, issue['ref'], body)
    issues.set_status(admin, issue['ref'], 'triage')
    with db.connect() as c:
        c.execute('UPDATE runs SET status=?,ended_at=? WHERE id=?', (status, db.now_iso() if status != 'running' else None, rid))
    return issue


old = reviewed()
enabled = auto_settings.update_settings(admin, 'PLAN', {'auto_plan_approve': True})
assert enabled['auto_plan_approve'] and not enabled['auto_review'] and not enabled['auto_execute']
with db.connect() as c:
    delegation = c.execute('SELECT * FROM project_auto_settings_events WHERE project_id=? ORDER BY id DESC LIMIT 1', (old['project_id'],)).fetchone()
    assert delegation['plan_id_floor'] == issues.list_plans(old['ref'])[0]['id']
new = reviewed()
isolated = reviewed(project='NS')
goal_plan = reviewed(labels=['goal'])
failed = [reviewed(status=s) for s in ('failed', 'timeout', 'orphaned', 'running')]
automation.sync()
approved = issues.get_issue(new['ref'])
assert approved['approval']['verdict'] == 'approve' and approved['approval']['actor'] == f"human:auto/delegation/{delegation['id']}"
assert len(approved['children']) == 2
assert all(t['status'] == 'backlog' for t in approved['children'])
assert not [j for j in jobs.list_jobs() if j['ref'] in [t['ref'] for t in approved['children']]]
event = next(e for e in approved['events'] if e['data'].get('decision'))
assert 'Auto 계획 승인' in event['body']
assert event['data']['delegated_by'] == 'human:admin' and event['data']['delegation_id'] == delegation['id']
assert event['data']['plan_version'] == 1 and event['data']['run_id']
assert not any('조건부 승인 메모' in issues.get_issue(t['ref'])['body'] for t in approved['children'])
for item in (old, isolated, goal_plan, *failed):
    assert issues.get_issue(item['ref'])['approval'] is None
automation.sync()
assert len(issues.get_issue(new['ref'])['children']) == 2
with db.connect() as c:
    assert c.execute("SELECT COUNT(*) FROM decisions WHERE issue_id=?", (new['id'],)).fetchone()[0] == 1

# 새 판·사람의 기존 결정·OFF·재위임을 먼저 저장하면 이전 관측으로 승인하지 않는다.
changed = reviewed()
issues.post_plan(agent_actor, changed['ref'], valid_plan)
human = reviewed()
issues.decide(admin, human['ref'], 'approve_notes', '사람 조건', plan_version=1)
issues.post_plan(agent_actor, human['ref'], valid_plan)
rejected = reviewed()
issues.decide(admin, rejected['ref'], 'reject', '사람 거절', plan_version=1)
off = reviewed()
auto_settings.update_settings(admin, 'PLAN', {'auto_plan_approve': False})
automation.sync()
assert issues.get_issue(off['ref'])['approval'] is None
auto_settings.update_settings(admin, 'PLAN', {'auto_plan_approve': True})
automation.sync()
assert issues.get_issue(off['ref'])['approval'] is None
assert issues.get_issue(changed['ref'])['approval'] is None
assert issues.get_issue(human['ref'])['approval']['actor'] == 'human:admin'
assert issues.get_issue(rejected['ref'])['approval']['verdict'] == 'reject'

# 불완전한 구조는 이유를 한 번만 남기며 승인·Task 생성은 하지 않는다.
bad_bodies = [
    '작은 계획',
    valid_plan.replace('2. 다음', '1. 중복'),
    valid_plan.replace('선행: 1', '선행: 9'),
    valid_plan.replace('선행: 없음', '선행: 2'),
    valid_plan.replace('선행: 1', '선행: 2'),
    valid_plan.replace('선행: 1', '선행: 1, 1'),
    valid_plan.replace('선행: 1', '선행: 미정'),
    valid_plan.replace('확인: 권한 검사', '확인:'),
    valid_plan.replace(' | 선행: 1', ''),
    valid_plan + '\n번호 없는 Task',
    valid_plan + '\n## 정해야 할 것\n- 배포 여부',
    *(valid_plan.replace('server/db.py', path) for path in ('../other.py', '/etc/passwd', 'C:\\other.py', '\\\\host\\share\\a.py', '~/.env')),
]
invalid = [reviewed(body=body) for body in bad_bodies]
automation.sync(); automation.sync()
for item in invalid:
    issue = issues.get_issue(item['ref'])
    assert issue['approval'] is None and issue['children'] == []
    assert len([e for e in issue['events'] if e['data'].get('auto_plan_approve_blocked')]) == 1

# 결정 기록 후 Task 생성 실패도 같은 트랜잭션에서 되돌린다.
rollback = reviewed()
original_spawn = issues._spawn_tasks
def fail_spawn(*args, **kwargs):
    original_spawn(*args, **kwargs)
    raise RuntimeError('Task 생성 중 실패')
try:
    with patch.object(issues, '_spawn_tasks', fail_spawn):
        automation.sync()
    raise AssertionError('실패 누락')
except RuntimeError:
    pass
assert issues.get_issue(rollback['ref'])['approval'] is None
assert issues.get_issue(rollback['ref'])['children'] == []

# 두 sync가 겹쳐도 승인과 하위 Task는 한 번만 만든다.
errors = []
def run_sync():
    try:
        automation.sync()
    except Exception as error:
        errors.append(error)
threads = [threading.Thread(target=run_sync) for _ in range(2)]
for t in threads: t.start()
for t in threads: t.join(10)
assert not errors and all(not t.is_alive() for t in threads)
assert len(issues.get_issue(rollback['ref'])['children']) == 2
with db.connect() as c:
    assert c.execute('SELECT COUNT(*) FROM decisions WHERE issue_id=?', (rollback['id'],)).fetchone()[0] == 1

# Auto 실행 ON은 기존 선행 조건을 통과한 첫 Task만 등록한다.
auto_settings.update_settings(admin, 'PLAN', {'auto_execute': True})
automation.sync()
ap_jobs = [j for j in jobs.list_jobs() if j['ref'].startswith('PLAN-')]
assert ap_jobs and all(j['mode'] == 'execute' and j['approval_version'] == 1 for j in ap_jobs)
assert approved['children'][0]['ref'] in [j['ref'] for j in ap_jobs]
assert approved['children'][1]['ref'] not in [j['ref'] for j in ap_jobs]
auto_settings.update_settings(admin, 'PLAN', {'auto_execute': False})
automation.sync()
assert not [j for j in jobs.list_jobs() if j['ref'].startswith('PLAN-')]
assert issues.get_issue(new['ref'])['approval']['verdict'] == 'approve'
print('OK — 완료 검토 연결·시점·프로젝트/Goal 격리·최신 판·사람 결정·OFF·구조 차단·감사·롤백·중복 경합·Auto 실행')

# 실제 쓰기 잠금을 잡은 변경과 sync를 겹쳐 오래된 설정·판·결정으로 승인하지 않음을 검사한다.
def race_before_sync(module, name, action):
    locked, release = threading.Event(), threading.Event()
    original = getattr(module, name)
    errors = []
    owner_id = []
    def pause(*args, **kwargs):
        result = original(*args, **kwargs)
        if threading.get_ident() == owner_id[0] and not locked.is_set():
            locked.set()
            if not release.wait(5):
                raise AssertionError('경합 검사 해제 시간 초과')
        return result
    def change():
        owner_id.append(threading.get_ident())
        try:
            action()
        except Exception as error:
            errors.append(error)
    def sync():
        try:
            automation.sync()
        except Exception as error:
            errors.append(error)
    with patch.object(module, name, pause):
        writer = threading.Thread(target=change)
        writer.start()
        try:
            assert locked.wait(5)
            reader = threading.Thread(target=sync)
            reader.start()
        finally:
            release.set()
        writer.join(10); reader.join(10)
        assert not errors and not writer.is_alive() and not reader.is_alive(), errors

race_off = reviewed()
race_before_sync(auto_settings, '_settings', lambda: auto_settings.update_settings(admin, 'PLAN', {'auto_plan_approve': False}))
assert issues.get_issue(race_off['ref'])['approval'] is None
auto_settings.update_settings(admin, 'PLAN', {'auto_plan_approve': True})
race_plan = reviewed()
race_before_sync(issues, '_event', lambda: issues.post_plan(agent_actor, race_plan['ref'], valid_plan))
assert issues.get_issue(race_plan['ref'])['approval'] is None
race_human = reviewed()
race_before_sync(issues, '_record_plan_decision', lambda: issues.decide(admin, race_human['ref'], 'approve_notes', '먼저 저장한 사람 조건', plan_version=1))
assert issues.get_issue(race_human['ref'])['approval']['actor'] == 'human:admin'
assert len(issues.get_issue(race_human['ref'])['children']) == 2
print('OK — OFF·새 판·사람 결정의 쓰기 트랜잭션과 sync 경합')

# 이전 자동 결정이 있는 새 검토 판은 승인하되 기존 Task는 복제하지 않는다.
again = reviewed()
automation.sync()
with db.connect() as c:
    rid = c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at) VALUES(?,'review','running','human:admin',?)", (again['id'], db.now_iso())).lastrowid
issues.post_plan(agent_actor, again['ref'], valid_plan)
with db.connect() as c:
    c.execute("UPDATE runs SET status='ok',ended_at=? WHERE id=?", (db.now_iso(), rid))
automation.sync()
latest = issues.get_issue(again['ref'])
assert latest['approval']['plan_version'] == 2 and not latest['approval']['stale']
assert len(latest['children']) == 2
print('OK — 새 검토 판의 자동 결정과 기존 Task 중복 방지')

# 별도 Plan 위임으로 승인한 Task도 결과 위임으로 완료할 수 있다.
issues.create_project(admin, 'FLOW', '전체 흐름', '', '/fake')
auto_settings.update_settings(admin, 'FLOW', {'auto_plan_approve': True, 'auto_approve': True})
flow = reviewed(project='FLOW')
automation.sync()
flow_parent = issues.get_issue(flow['ref'])
assert flow_parent['approval']['actor'].startswith('human:auto/delegation/')
flow_task = flow_parent['children'][0]['ref']
with db.connect() as c:
    iid = issues._find(c, flow_task)['id']
    rid = c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at,ended_at) VALUES(?,'execute','ok','human:admin',?,?)", (iid,db.now_iso(),db.now_iso())).lastrowid
    issues._set_status(c, admin, issues._find(c, flow_task), 'in_progress', data={'run_id':rid})
issues.link_commit(admin, flow_task, 'c' * 40)
issues.set_status(admin, flow_task, 'in_review')
with db.connect() as c:
    commit = c.execute("SELECT MAX(id) FROM events WHERE issue_id=? AND kind='commit'", (iid,)).fetchone()[0]
    status = c.execute("SELECT MAX(id) FROM events WHERE issue_id=? AND kind='status'", (iid,)).fetchone()[0]
    c.execute("INSERT INTO task_execution_results VALUES(?,1,'unmerged',?,?)", (rid,commit,status))
with patch.object(orchestrate, 'settings', return_value={'auto_merge':False}):
    automation.sync()
assert issues.get_issue(flow_task)['status'] == 'done'
assert issues.get_issue(flow['ref'])['status'] != 'done'
print('OK — 독립 Plan 승인과 Task 결과 승인 연속 동작')

role_before = auto_settings.get_settings('NS')
assert not role_before['token_exhaustion_fallback']
for field in ('orchestrator_provider_order', 'troubleshooter_provider_order', 'provider_order'):
    before = auto_settings.get_settings('NS')
    after = auto_settings.update_settings(admin, 'NS', {field: ['codex', 'claude']})
    assert after[field] == ['codex', 'claude']
    for other in auto_settings.ORDER_FIELDS:
        if other != field:
            assert after[other] == before[other]
    assert not after['auto_review'] and not after['auto_execute']
    for value in ([], ['claude'], ['claude', 'claude'], ['codex', 'other'], None, 'codex'):
        try:
            auto_settings.update_settings(admin, 'NS', {field: value})
            raise AssertionError((field, value))
        except issues.StoreError as error:
            assert error.status == 400
    assert auto_settings.get_settings('NS') == after
for value in (1, 0, 'true', None):
    try:
        auto_settings.update_settings(admin, 'NS', {'token_exhaustion_fallback': value})
        raise AssertionError(value)
    except issues.StoreError as error:
        assert error.status == 400
fallback = auto_settings.update_settings(admin, 'NS', {'token_exhaustion_fallback': True})
assert fallback['token_exhaustion_fallback'] and not fallback['auto_execute']
db.init()
assert auto_settings.get_settings('NS') == fallback
with db.connect() as c:
    event = c.execute(
        "SELECT e.* FROM project_auto_settings_events e JOIN projects p ON p.id=e.project_id WHERE p.key='NS' ORDER BY e.id DESC LIMIT 1").fetchone()
    assert json.loads(event['after_json']) == fallback
    assert json.loads(event['before_json'])['token_exhaustion_fallback'] is False
print('OK — 세 역할 독립 저장·유효성·이관 기본 OFF·감사·영속성')
