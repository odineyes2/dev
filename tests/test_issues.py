"""이슈 API — 프로젝트·번호(NS-1)·하위 이슈·계획서 판·타임라인·잡기(lease)·권한(에이전트는 done/closed·남의 본문·지우기 불가)."""
import os, sys, tempfile
from pathlib import Path

os.environ["DEV_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as A, auth, db, issues  # noqa: E402

auth._client = httpx.AsyncClient(transport=httpx.MockTransport(
    lambda req: httpx.Response(200, json={"user": {"id": 1, "username": "admin", "role": "admin"}})))
H = {"X-Requested-With": "dev"}

with TestClient(A.app) as c:
    c.cookies.set("ns_session", "adm")
    human = lambda m, url, **kw: c.request(m, url, headers=H, **kw)
    ka = human("POST", "/api/agents", json={"name": "claude", "model": "claude-opus-5-5"}).json()["key"]
    kb = human("POST", "/api/agents", json={"name": "codex", "model": "gpt-x"}).json()["key"]
    c.cookies.clear()
    agent = lambda key: (lambda m, url, **kw: c.request(m, url, headers={"Authorization": f"Bearer {key}"}, **kw))
    a, b = agent(ka), agent(kb)
    c.cookies.set("ns_session", "adm")

    def ok(r, code=200):
        assert r.status_code == code, (r.status_code, r.text)
        return r.json() if r.content else None

    # 프로젝트 — 사람만, 키 검사
    assert a("POST", "/api/projects", json={"key": "NS", "name": "x"}).status_code == 403
    assert human("POST", "/api/projects", json={"key": "ns1-", "name": "x"}).status_code == 400
    ok(human("POST", "/api/projects", json={"key": "ns", "name": "nightshift", "repo_url": "https://github.com/odineyes2/nightshift.git"}))
    ok(human("POST", "/api/projects", json={"key": "DEV", "name": "dev"}))
    assert human("POST", "/api/projects", json={"key": "NS", "name": "again"}).status_code == 409

    # 이슈 — 번호는 프로젝트마다
    i1 = ok(human("POST", "/api/issues", json={"project": "NS", "title": "보드 복사", "body": "사람이 쓴 지시", "priority": "high", "labels": ["board", "ui"]}))
    i2 = ok(human("POST", "/api/issues", json={"project": "NS", "title": "두 번째"}))
    d1 = ok(human("POST", "/api/issues", json={"project": "DEV", "title": "dev 첫 이슈"}))
    assert (i1["ref"], i2["ref"], d1["ref"]) == ("NS-1", "NS-2", "DEV-1") and i1["reporter"] == "human:admin" and i1["labels"] == ["board", "ui"]
    assert human("POST", "/api/issues", json={"project": "NS", "title": "x", "status": "done"}).status_code == 400
    assert human("POST", "/api/issues", json={"project": "NOPE", "title": "x"}).status_code == 404
    assert human("POST", "/api/issues", json={"project": "NS", "title": "x", "priority": "p0"}).status_code == 400

    # 에이전트가 하위 Task를 만든다 — 다른 프로젝트 부모는 안 됨
    t1 = ok(a("POST", "/api/issues", json={"project": "NS", "title": "Task: 메뉴", "parent": "NS-1"}))
    assert t1["ref"] == "NS-3" and t1["reporter"].startswith("agent:")
    assert a("POST", "/api/issues", json={"project": "DEV", "title": "x", "parent": "NS-1"}).status_code == 400

    # 목록 — 본문 없음, 필터
    lst = ok(a("GET", "/api/issues?project=NS"))["issues"]
    assert {x["ref"] for x in lst} == {"NS-1", "NS-2", "NS-3"} and all("body" not in x for x in lst)
    assert [x["ref"] for x in ok(a("GET", "/api/issues?project=ns&parent=none&status=backlog"))["issues"]] == ["NS-2", "NS-1"]
    assert [x["ref"] for x in ok(a("GET", "/api/issues?parent=NS-1"))["issues"]] == ["NS-3"]
    assert [x["ref"] for x in ok(a("GET", "/api/issues?q=복사"))["issues"]] == ["NS-1"]
    assert a("GET", "/api/issues?status=weird").status_code == 400

    # 권한 — 에이전트는 사람이 쓴 본문을 못 고치고, 자기 Task는 고친다
    assert a("PATCH", "/api/issues/NS-1", json={"body": "바꿔치기"}).status_code == 403
    assert ok(a("PATCH", "/api/issues/NS-3", json={"title": "Task: 우클릭 메뉴"}))["title"] == "Task: 우클릭 메뉴"
    assert ok(a("PATCH", "/api/issues/NS-1", json={"labels": ["board"]}))["labels"] == ["board"]
    assert a("DELETE", "/api/issues/NS-2").status_code == 403

    # 잡기 — 한 번에 하나, 같은 actor는 연장, 남이 놓기 불가, 사람은 놓기 가능
    ok(a("POST", "/api/issues/NS-1/claim"))
    assert b("POST", "/api/issues/NS-1/claim").status_code == 409
    ok(a("POST", "/api/issues/NS-1/claim", json={"minutes": 60}))   # 연장(이벤트 안 늘어남)
    assert b("POST", "/api/issues/NS-1/release").status_code == 403
    ok(human("POST", "/api/issues/NS-2/claim"))
    ok(human("POST", "/api/issues/NS-2/release"))
    # 만료된 점유는 풀린 것 — 남이 잡을 수 있다
    with db.connect() as conn:
        conn.execute("UPDATE issues SET lease_until='2000-01-01T00:00:00+00:00' WHERE number=1")
    assert ok(a("GET", "/api/issues/NS-1"))["claimed_by"] is None
    ok(b("POST", "/api/issues/NS-1/claim"))
    ok(b("POST", "/api/issues/NS-1/release"))
    ok(a("POST", "/api/issues/NS-1/claim"))

    # 계획서 판·댓글·커밋·상태
    assert ok(a("POST", "/api/issues/NS-1/plans", json={"body": "1. 메뉴\n2. 붙여넣기"}))["version"] == 1
    assert ok(a("POST", "/api/issues/NS-1/plans", json={"body": "v2"}))["version"] == 2
    assert [p["version"] for p in ok(a("GET", "/api/issues/NS-1/plans"))["plans"]] == [2, 1]
    ok(a("POST", "/api/issues/NS-1/status", json={"status": "in_progress"}))
    ok(a("POST", "/api/issues/NS-1/comments", json={"body": "진행 중이에요"}))
    assert a("POST", "/api/issues/NS-1/commits", json={"sha": "zz"}).status_code == 400
    ok(a("POST", "/api/issues/NS-1/commits", json={"sha": "1261CE1", "repo": "nightshift", "message": "보드 카드 복사"}))
    assert a("POST", "/api/issues/NS-1/status", json={"status": "done"}).status_code == 403
    r = ok(a("POST", "/api/issues/NS-1/status", json={"status": "in_review", "note": "확인 부탁해요"}))
    assert r["claimed_by"] is None   # in_review로 올리면 놓는다
    ok(human("POST", "/api/issues/NS-1/status", json={"status": "changes_requested", "note": "모바일도"}))
    r = ok(human("POST", "/api/issues/NS-1/status", json={"status": "done"}))
    assert r["closed_at"] and r["status"] == "done"
    assert a("POST", "/api/issues/NS-1/claim").status_code == 409   # 끝난 이슈는 못 잡음

    full = ok(a("GET", "/api/issues/ns-1"))
    assert full["plan"]["version"] == 2 and full["body"] == "사람이 쓴 지시" and [ch["ref"] for ch in full["children"]] == ["NS-3"]
    kinds = [(e["kind"], e["actor"].split(":")[0]) for e in full["events"]]
    assert kinds == [("edit", "agent"), ("claim", "agent"), ("claim", "agent"), ("claim", "agent"), ("claim", "agent"),
                     ("plan", "agent"), ("plan", "agent"), ("status", "agent"), ("comment", "agent"), ("commit", "agent"),
                     ("status", "agent"), ("status", "human"), ("status", "human")], kinds
    ev = full["events"]
    assert ev[0]["data"]["model"] == "claude-opus-5-5" and ev[9]["data"]["sha"] == "1261ce1"
    assert ev[10]["body"] == "확인 부탁해요" and ev[10]["data"]["from"] == "in_progress" and "model" not in ev[11]["data"]
    assert ok(a("GET", "/api/issues/NS-3"))["parent_ref"] == "NS-1"

    # 사람은 지운다 — 하위 이슈는 최상위로
    assert human("DELETE", "/api/issues/NS-1").status_code == 204
    assert a("GET", "/api/issues/NS-1").status_code == 404
    assert ok(a("GET", "/api/issues/NS-3"))["parent_ref"] is None
    assert ok(human("POST", "/api/issues", json={"project": "NS", "title": "번호는 재사용 안 함"}))["ref"] == "NS-4"

    # 나눠 읽기(DEV-7) — offset·limit, has_more, 겹치거나 빠지는 것 없이
    for n in range(120):
        ok(human("POST", "/api/issues", json={"project": "DEV", "title": f"page {n}"}))
    seen, off, pages = [], 0, []
    while True:
        r = ok(a("GET", f"/api/issues?project=DEV&limit=50&offset={off}"))
        pages.append((len(r["issues"]), r["has_more"]))
        seen += [i["ref"] for i in r["issues"]]; off += len(r["issues"])
        if not r["has_more"]:
            break
    assert pages == [(50, True), (50, True), (21, False)], pages   # DEV-1 + page 0~119
    assert len(seen) == len(set(seen)) == 121
    assert ok(a("GET", "/api/issues?project=DEV&limit=50&offset=500"))["issues"] == []

    # 제목 없는 이슈(DEV-8) — 본문만 있으면 만들어지고, 에이전트는 제목만 채울 수 있다(본문은 여전히 못 고침)
    assert human("POST", "/api/issues", json={"project": "DEV", "title": "", "body": ""}).status_code == 400
    nt = ok(human("POST", "/api/issues", json={"project": "DEV", "title": "", "body": "밤모드 버튼이 안 먹어요"}))
    dot = ok(human("POST", "/api/issues", json={"project": "DEV", "title": ".", "body": "로그인 화면 문구 고치기"}))
    assert nt["title_missing"] and dot["title_missing"] and not ok(a("GET", "/api/issues/DEV-1"))["title_missing"]
    assert a("PATCH", f"/api/issues/{nt['ref']}", json={"body": "바꿔치기"}).status_code == 403
    assert a("PATCH", f"/api/issues/{nt['ref']}", json={"title": "..."}).status_code == 400   # 글자 없는 제목은 안 됨
    r = ok(a("PATCH", f"/api/issues/{dot['ref']}", json={"title": "로그인 화면 문구 고치기"}))
    assert r["title"] == "로그인 화면 문구 고치기" and not r["title_missing"]
    assert a("PATCH", f"/api/issues/{dot['ref']}", json={"title": "또 바꾸기"}).status_code == 403   # 채운 뒤엔 다시 사람 것
    ev = ok(a("GET", f"/api/issues/{dot['ref']}"))["events"][-1]
    assert ev["kind"] == "edit" and ev["data"]["fields"] == ["title"] and ev["actor"].startswith("agent:")
    ok(human("PATCH", f"/api/issues/{nt['ref']}", json={"body": "사람은 본문을 고칠 수 있다"}))   # 제목 없이 본문만

    # 계획서 결정(DEV-14) — 사람만, 메모 규칙, 보던 판이 최신일 때만, stale, 거절은 닫음
    dec = lambda ref, verdict, note="", v=None: human("POST", f"/api/issues/{ref}/decision", json={"verdict": verdict, "note": note, "plan_version": v})
    t = ok(human("POST", "/api/issues", json={"project": "DEV", "title": "결정 시험", "status": "triage"}))["ref"]
    assert dec(t, "approve", "", 1).status_code == 409   # 계획서 없음
    ok(a("POST", f"/api/issues/{t}/plans", json={"body": "## 정해야 할 것\n1. 끌까 지울까"}))
    assert a("POST", f"/api/issues/{t}/decision", json={"verdict": "approve", "plan_version": 1}).status_code == 403
    assert dec(t, "approve", "", 9).status_code == 409   # 안 본 판
    assert dec(t, "approve_notes", "  ", 1).status_code == 400 and dec(t, "reject", "", 1).status_code == 400
    assert dec(t, "maybe", "x", 1).status_code == 400
    assert ok(a("GET", f"/api/issues/{t}"))["approval"] is None
    r = ok(dec(t, "approve_notes", "1번은 끄기로", 1))
    assert r["approval"] == {**r["approval"], "verdict": "approve_notes", "plan_version": 1, "stale": False, "note": "1번은 끄기로"} and r["status"] == "triage"
    assert r["events"][-1]["kind"] == "comment" and "1번은 끄기로" in r["events"][-1]["body"]
    _i = issues   # approved 필터·요약은 MCP가 쓰는 저장소 함수로 본다
    assert [x["ref"] for x in _i.list_issues(approved=True)] == [t]
    assert _i.list_issues(project="DEV", status="triage")[0]["approval"]["verdict"] == "approve_notes"
    ok(a("POST", f"/api/issues/{t}/plans", json={"body": "v2 — 방향 바뀜"}))   # 새 판 → 이전 결정 무효
    assert ok(a("GET", f"/api/issues/{t}"))["approval"]["stale"] is True and _i.list_issues(approved=True) == []
    assert dec(t, "approve", "", 1).status_code == 409
    assert ok(dec(t, "approve", "", 2))["approval"]["stale"] is False and [x["ref"] for x in _i.list_issues(approved=True)] == [t]
    r = ok(dec(t, "reject", "필요 없어짐", 2))
    assert r["status"] == "closed" and r["approval"]["verdict"] == "reject" and "필요 없어짐" in r["events"][-1]["body"]
    assert dec(t, "approve", "", 2).status_code == 409   # 끝난 이슈

    # 쓰기 요청의 형식 오류
    assert c.post("/api/issues", content=b"not json", headers={**H, "content-type": "application/json"}).status_code == 400
print("OK")
