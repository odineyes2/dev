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
    for i, sql in enumerate(db.MIGRATIONS[:-1], 1):
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
        added = (0,) if table == 'project_auto_settings_events' else ()
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

# 자동 승인 자체는 provider 호출 없이 완료된 검토에만 적용한다.
from unittest.mock import patch
import threading
jobs._threads = 1
auto_settings.update_settings(admin, 'AUTO', {'auto_review': False, 'auto_execute': False})
issues.create_project(admin, 'AP', '계획 승인', '', str(Path(os.environ['DEV_DATA_DIR'])))
agent_actor = {'kind': 'agent', 'id': agent['id']}
valid_plan = ('## Tasks\n'
              '1. 먼저 | 파일: server/db.py | 확인: 임시 DB 검사 | 선행: 없음\n'
              '2. 다음 | 파일: server/issues.py | 확인: 권한 검사 | 선행: 1')


def reviewed(body=valid_plan, status='ok', labels=None, project='AP'):
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
enabled = auto_settings.update_settings(admin, 'AP', {'auto_approve': True})
assert enabled['auto_approve'] and not enabled['auto_review'] and not enabled['auto_execute']
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
auto_settings.update_settings(admin, 'AP', {'auto_approve': False})
automation.sync()
assert issues.get_issue(off['ref'])['approval'] is None
auto_settings.update_settings(admin, 'AP', {'auto_approve': True})
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
    assert len([e for e in issue['events'] if e['data'].get('auto_approve_blocked')]) == 1

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
auto_settings.update_settings(admin, 'AP', {'auto_execute': True})
automation.sync()
ap_jobs = [j for j in jobs.list_jobs() if j['ref'].startswith('AP-')]
assert ap_jobs and all(j['mode'] == 'execute' and j['approval_version'] == 1 for j in ap_jobs)
assert approved['children'][0]['ref'] in [j['ref'] for j in ap_jobs]
assert approved['children'][1]['ref'] not in [j['ref'] for j in ap_jobs]
auto_settings.update_settings(admin, 'AP', {'auto_execute': False})
automation.sync()
assert not [j for j in jobs.list_jobs() if j['ref'].startswith('AP-')]
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
race_before_sync(auto_settings, '_settings', lambda: auto_settings.update_settings(admin, 'AP', {'auto_approve': False}))
assert issues.get_issue(race_off['ref'])['approval'] is None
auto_settings.update_settings(admin, 'AP', {'auto_approve': True})
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
