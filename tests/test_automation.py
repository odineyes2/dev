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
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    before = {t: [tuple(r) for r in c.execute(f'SELECT * FROM "{t}"')] for t in tables}
assert db.init() == len(db.MIGRATIONS)
with db.connect() as c:
    assert before == {t: [tuple(r) for r in c.execute(f'SELECT * FROM "{t}"')] for t in tables}
issues.create_project(admin, "NS", "격리 프로젝트")

with TestClient(A.app) as c:
    c.cookies.set("ns_session", "admin")
    def call(method, path="/api/projects/dev/auto-settings", **kw):
        return c.request(method, path, headers={"X-Requested-With": "dev"}, **kw)

    defaults = call("GET").json()
    assert all(defaults[k] is False for k in ("auto_review", "auto_execute", "auto_approve"))
    assert defaults["provider_order"] == ["claude", "codex"]
    assert not defaults["auto_approve_available"] and defaults["auto_approve_disabled_reason"]
    assert defaults["updated_by"] is None
    assert call("GET", "/api/projects/missing/auto-settings").status_code == 404
    assert call("PATCH", "/api/projects/missing/auto-settings", json={"auto_review": True}).status_code == 404
    r = call("PATCH", json={"auto_review": True, "provider_order": ["codex", "claude"]})
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["auto_review"] and not saved["auto_execute"] and saved["updated_by"] == "human:admin"
    assert auto_settings.get_settings("NS") == {**defaults, "project_key": "NS"}
    for bad in ({"auto_review": 1}, {"auto_execute": "true"}, {"auto_approve": None},
                {"auto_approve": True, "auto_review": False}, {"unknown": True},
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
