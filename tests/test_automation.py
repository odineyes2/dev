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
import app as A, auth, auto_settings, config, db, issues, jobs

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
