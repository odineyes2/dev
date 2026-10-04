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

    # 설명 — 기존 생성 호출, 여러 줄 원문, 부분 수정, 삭제와 입력 제한을 검사한다.
    projects = ok(a("GET", "/api/projects"))["projects"]
    assert all(p["description"] == "" for p in projects)
    description = "  제품 의도\n대상 고객과 핵심 가치\n"
    described = ok(human("POST", "/api/projects", json={
        "key": "DOC", "name": "설명 검사", "description": description}))
    assert described["description"] == description
    assert ok(human("PATCH", "/api/projects/doc", json={"name": "이름 수정"}))["description"] == description
    assert a("PATCH", "/api/projects/DOC", json={"description": "권한 없음"}).status_code == 403
    boundary = "가" * issues.MAX_TEXT
    assert ok(human("PATCH", "/api/projects/DOC", json={"description": boundary}))["description"] == boundary
    assert human("PATCH", "/api/projects/DOC", json={"name": "저장되면 안 됨", "description": boundary + "가"}).status_code == 400
    stored = next(p for p in ok(a("GET", "/api/projects"))["projects"] if p["key"] == "DOC")
    assert stored["description"] == boundary and stored["name"] == "이름 수정"
    assert human("POST", "/api/projects", json={"key": "LONG", "name": "긴 설명", "description": boundary + "가"}).status_code == 400
    assert not any(p["key"] == "LONG" for p in issues.list_projects())
    assert ok(human("PATCH", "/api/projects/DOC", json={"description": ""}))["description"] == ""
    assert ok(human("PATCH", "/api/projects/DOC", json={"description": None}))["description"] == ""
    assert human("PATCH", "/api/projects/MISSING", json={"description": "설명"}).status_code == 404
    # 저장소의 기존 위치 인자 호출도 호환한다.
    legacy = issues.create_project({"kind": "human", "name": "admin"}, "LEGACY", "기존 호출", "repo", "path")
    assert legacy["description"] == "" and legacy["repo_url"] == "repo" and legacy["local_path"] == "path"

    # 종류는 Labels와 독립되며 비활성화 후에도 기존 연결을 보존한다.
    catalog = ok(a('GET', '/api/issue-types'))['types']
    assert [t['name'] for t in catalog] == ['버그 수정', '문서 생성', 'UI/UX', '기능 추가']
    assert a('POST', '/api/issue-types', json={'name': '금지'}).status_code == 403
    assert a('PATCH', '/api/issue-types/1', json={'active': False}).status_code == 403
    custom = ok(human('POST', '/api/issue-types', json={'name': '테스트 종류'}))
    tid = custom['id']
    assert human('POST', '/api/issue-types', json={'name': '테스트 종류'}).status_code == 409
    typed = ok(human('POST', '/api/issues', json={'project': 'DOC', 'title': '종류 검사', 'type_ids': [tid, 1, tid], 'labels': ['ui']}))
    assert typed['type_ids'] == [1, tid] and typed['labels'] == ['ui']
    assert all(t['source'] == 'human' for t in typed['types'])
    for bad in ([9999], [True], ['1'], [-1], '1', None):
        before = issues.get_issue(typed['ref'])
        assert human('PATCH', f"/api/issues/{typed['ref']}", json={'title': '롤백', 'type_ids': bad}).status_code == 400
        assert issues.get_issue(typed['ref']) == before
    ok(human('PATCH', f'/api/issue-types/{tid}', json={'name': '수정된 종류', 'active': False}))
    assert tid not in [t['id'] for t in ok(a('GET', '/api/issue-types'))['types']]
    assert tid in [t['id'] for t in ok(a('GET', '/api/issue-types?include_inactive=true'))['types']]
    preserved = ok(a('GET', f"/api/issues/{typed['ref']}"))
    assert preserved['types'][-1]['name'] == '수정된 종류' and not preserved['types'][-1]['active']
    ok(human('PATCH', f"/api/issues/{typed['ref']}", json={'type_ids': [tid, 2]}))
    assert human('POST', '/api/issues', json={'project': 'DEV', 'title': '비활성', 'type_ids': [tid]}).status_code == 400
    ok(human('PATCH', f"/api/issues/{typed['ref']}", json={'type_ids': []}))
    assert human('PATCH', f"/api/issues/{typed['ref']}", json={'type_ids': [tid]}).status_code == 400
    ok(human('DELETE', f"/api/issues/{typed['ref']}"), 204)
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
    assert t1["ref"] == "NS-1-1" and t1["reporter"].startswith("agent:")
    assert a("POST", "/api/issues", json={"project": "DEV", "title": "x", "parent": "NS-1"}).status_code == 400

    # 목록 — 본문 없음, 필터
    lst = ok(a("GET", "/api/issues?project=NS"))["issues"]
    assert {x["ref"] for x in lst} == {"NS-1", "NS-2", "NS-1-1"} and all("body" not in x for x in lst)
    assert [x["ref"] for x in ok(a("GET", "/api/issues?project=ns&parent=none&status=backlog"))["issues"]] == ["NS-2", "NS-1"]
    assert [x["ref"] for x in ok(a("GET", "/api/issues?parent=NS-1"))["issues"]] == ["NS-1-1"]
    assert [x["ref"] for x in ok(a("GET", "/api/issues?q=복사"))["issues"]] == ["NS-1"]
    assert a("GET", "/api/issues?status=weird").status_code == 400

    # 권한 — 에이전트는 사람이 쓴 본문을 못 고치고, 자기 Task는 고친다
    assert a("PATCH", "/api/issues/NS-1", json={"body": "바꿔치기"}).status_code == 403
    assert ok(a("PATCH", "/api/issues/NS-1-1", json={"title": "Task: 우클릭 메뉴"}))["title"] == "Task: 우클릭 메뉴"
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
    assert full["plan"]["version"] == 2 and full["body"] == "사람이 쓴 지시" and [ch["ref"] for ch in full["children"]] == ["NS-1-1"]
    kinds = [(e["kind"], e["actor"].split(":")[0]) for e in full["events"]]
    assert kinds == [("edit", "agent"), ("claim", "agent"), ("claim", "agent"), ("claim", "agent"), ("claim", "agent"),
                     ("plan", "agent"), ("plan", "agent"), ("status", "agent"), ("comment", "agent"), ("commit", "agent"),
                     ("status", "agent"), ("status", "human"), ("status", "human")], kinds
    ev = full["events"]
    assert ev[0]["data"]["model"] == "claude-opus-5-5" and ev[9]["data"]["sha"] == "1261ce1"
    assert ev[10]["body"] == "확인 부탁해요" and ev[10]["data"]["from"] == "in_progress" and "model" not in ev[11]["data"]
    assert ok(a("GET", "/api/issues/NS-1-1"))["parent_ref"] == "NS-1"

    # 사람은 지운다 — 하위 이슈는 최상위로
    assert human("DELETE", "/api/issues/NS-1").status_code == 204
    assert a("GET", "/api/issues/NS-1").status_code == 404
    assert ok(a("GET", "/api/issues/NS-1-1"))["parent_ref"] is None
    assert ok(human("POST", "/api/issues", json={"project": "NS", "title": "번호는 재사용 안 함"}))["ref"] == "NS-3"

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
    # 관리자 위임을 켜도 REST 에이전트의 설정·결정 권한은 늘어나지 않는다.
    assert ok(human('PATCH', '/api/projects/DEV/auto-settings', json={'auto_approve': True}))['auto_approve']
    assert a('PATCH', '/api/projects/DEV/auto-settings', json={'auto_approve': True}).status_code == 403
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
    ok(human('PATCH', '/api/projects/DEV/auto-settings', json={'auto_approve': False}))

    # 목록 Action 근거는 부모가 검색·상태·페이지 밖에 있어도 같으며 본문은 노출하지 않는다.
    ap = ok(human("POST", "/api/issues", json={"project": "DOC", "title": "Action 부모", "status": "triage"}))
    ac = ok(human("POST", "/api/issues", json={"project": "DOC", "title": "Action 자식", "parent": ap['ref']}))
    def action_row(ref):
        return next(x for x in issues.list_issues(project="DOC") if x['ref'] == ref)
    base = action_row(ac['ref'])
    assert 'body' not in base and base['approval'] is None
    assert base['action_context'] == {'plan_version': None, 'approval': None, 'has_children': False,
        'has_execution': False, 'has_active_job': False, 'parent': {'plan_version': None,
        'approval': None, 'has_children': True, 'has_execution': False, 'has_active_job': False}}
    assert issues.list_issues(project='MISSING') == []
    ok(a('POST', f"/api/issues/{ap['ref']}/plans", json={'body': 'Action 계획'}))
    assert action_row(ac['ref'])['action_context']['parent']['plan_version'] == 1
    assert action_row(ac['ref'])['action_context']['parent']['approval'] is None
    ok(dec(ap['ref'], 'approve_notes', '조건 유지', 1))
    context = action_row(ac['ref'])['action_context']
    assert context['parent']['approval'] == {'verdict': 'approve_notes', 'plan_version': 1, 'stale': False}
    filtered = ok(a('GET', '/api/issues', params={'project': 'DOC', 'status': 'backlog', 'q': 'Action 자식', 'limit': 1}))
    assert filtered['issues'][0]['action_context'] == context and not filtered['has_more']
    all_rows = issues.list_issues(project='DOC')
    index = next(i for i, row in enumerate(all_rows) if row['ref'] == ac['ref'])
    assert issues.list_issues(project='DOC', limit=1, offset=index)[0]['action_context'] == context
    ok(a('POST', f"/api/issues/{ap['ref']}/plans", json={'body': 'Action 새 판'}))
    stale = action_row(ac['ref'])['action_context']['parent']
    assert stale['plan_version'] == 2 and stale['approval']['stale']
    assert dec(ap['ref'], 'approve', '', 1).status_code == 409
    ok(dec(ap['ref'], 'approve', '', 2))
    assert not action_row(ac['ref'])['action_context']['parent']['approval']['stale']
    with db.connect() as cx:
        # 검토 기록은 실행 이력이 아니며 실패한 실행도 재실행 버튼 대상에서 제외한다.
        cx.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at) VALUES(?,'review','ok','human:admin',?)", (ac['id'], db.now_iso()))
    assert not action_row(ac['ref'])['action_context']['has_execution']
    with db.connect() as cx:
        cx.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at) VALUES(?,'execute','failed','human:admin',?)", (ac['id'], db.now_iso()))
        job = cx.execute("INSERT INTO jobs(issue_id,mode,status,actor,created_at) VALUES(?,'execute','queued','human:admin',?)", (ac['id'], db.now_iso())).lastrowid
    assert action_row(ac['ref'])['action_context']['has_execution']
    assert action_row(ac['ref'])['action_context']['has_active_job']
    with db.connect() as cx:
        cx.execute("UPDATE jobs SET status='started', run_id=(SELECT MAX(id) FROM runs WHERE issue_id=?) WHERE id=?", (ac['id'], job))
    assert not action_row(ac['ref'])['action_context']['has_active_job']
    with db.connect() as cx:
        cx.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (job,))
    assert not action_row(ac['ref'])['action_context']['has_active_job']

    # 승인하면 계획서의 Tasks가 하위 이슈 + 선후관계로(DEV-20) — 한 번만, 메모는 Task에 실린다
    p = ok(human("POST", "/api/issues", json={"project": "DEV", "title": "쪼갤 일", "status": "triage"}))["ref"]
    plan = "## 방향\n가\n\n## Tasks\n1. 서버 | 파일: a.py | 확인: 테스트\n2. 화면 | 파일: b.js | 선행: 1\n3) 문서 | 선행: 1, 2\n\n## 정해야 할 것\n1. 이건 Task가 아님"
    ok(a("POST", f"/api/issues/{p}/plans", json={"body": plan}))
    r = ok(dec(p, "approve_notes", "화면은 다크모드도", 1))
    kids = r["children"]
    assert [k["ref"] for k in kids] == [f"{p}-1", f"{p}-2", f"{p}-3"]   # Task 번호는 부모 아래에서 센다
    assert [k["title"] for k in kids] == ["서버", "화면", "문서"] and "하위 Task 3개" in r["events"][-1]["body"], kids
    with db.connect() as cx:
        deps = {(x[0], x[1]) for x in cx.execute("SELECT issue_id, blocked_by_id FROM issue_deps")}
    ids = {k["title"]: cx_id for k in kids for cx_id in [ok(a("GET", f"/api/issues/{k['ref']}"))["id"]]}
    assert {(ids["화면"], ids["서버"]), (ids["문서"], ids["서버"]), (ids["문서"], ids["화면"])} <= deps
    assert "화면은 다크모드도" in ok(a("GET", f"/api/issues/{kids[0]['ref']}"))["body"] and "a.py" in ok(a("GET", f"/api/issues/{kids[0]['ref']}"))["body"]
    assert len(ok(dec(p, "approve", "", 1))["children"]) == 3   # 다시 승인해도 늘지 않는다
    assert issues.parse_tasks("## Tasks\n") == [] and issues.parse_tasks("Tasks 없음") == []

    # Tasks 절이 없는 계획서도 승인하면 이슈 자체가 Task 하나로 — 그 Task를 다시 승인해도 손자는 없다
    q = ok(human("POST", "/api/issues", json={"project": "DEV", "title": "작은 일", "status": "triage"}))["ref"]
    ok(a("POST", f"/api/issues/{q}/plans", json={"body": "## 방향\n작다"}))
    r = ok(dec(q, "approve", "", 1))
    assert [k["title"] for k in r["children"]] == ["작은 일"] and "하위 Task 1개" in r["events"][-1]["body"], r["children"]
    assert len(ok(dec(q, "approve", "", 1))["children"]) == 1
    kid = r["children"][0]["ref"]
    ok(a("POST", f"/api/issues/{kid}/plans", json={"body": "세부"}))
    assert ok(dec(kid, "approve", "", 1))["children"] == []

    # 최종 목표(goal 라벨) — 에이전트는 자기가 만든 것이라도 상태·제목·본문·라벨을 못 바꾸고, 댓글만 남긴다. 완료는 사람만.
    g = ok(a("POST", "/api/issues", json={"project": "DEV", "title": "최종 목표", "body": "원문", "labels": ["goal"]}))
    for body in ({"status": "in_review"}, {"status": "on_hold"}, {"status": "done"}):
        assert a("POST", f"/api/issues/{g['ref']}/status", json=body).status_code == 403, body
    for body in ({"body": "다시 씀"}, {"title": "다른 제목"}, {"labels": []}, {"type_ids": [1]}):
        assert a("PATCH", f"/api/issues/{g['ref']}", json=body).status_code == 403, body
    ok(a("POST", f"/api/issues/{g['ref']}/comments", json={"body": "진행 상황"}))
    assert ok(human("POST", f"/api/issues/{g['ref']}/status", json={"status": "in_progress"}))["status"] == "in_progress"

    # 묶음 전체 완료(DEV-44) — Task에서 눌러도 상위에서 눌러도 최상위+모든 하위가 done, closed는 그대로, 사람만, 실행 중이면 통째로 거부
    def tree():
        top = ok(human("POST", "/api/issues", json={"project": "DEV", "title": "묶음", "labels": ["goal"]}))["ref"]
        ks = [ok(human("POST", "/api/issues", json={"project": "DEV", "title": f"T{n}", "parent": top}))["ref"] for n in range(3)]
        ok(human("POST", f"/api/issues/{ks[2]}/status", json={"status": "closed", "note": "거절"}))
        return top, ks
    st = lambda ref: ok(a("GET", f"/api/issues/{ref}"))["status"]
    for pick in (lambda top, ks: ks[0], lambda top, ks: top):
        top, ks = tree()
        assert a("POST", f"/api/issues/{top}/complete-tree", json={}).status_code == 403
        r = ok(human("POST", f"/api/issues/{pick(top, ks)}/complete-tree", json={}))["issues"]
        assert sorted(x["ref"] for x in r) == sorted([top, ks[0], ks[1]]) and all(x["status"] == "done" and x["closed_at"] for x in r), r
        assert [st(x) for x in (top, *ks)] == ["done", "done", "done", "closed"]
        ev = ok(a("GET", f"/api/issues/{ks[1]}"))["events"][-1]
        assert ev["kind"] == "status" and ev["data"]["to"] == "done" and f"{pick(top, ks)}에서 한 번에" in ev["body"], ev
    top, ks = tree()
    with db.connect() as cx:
        cx.execute("INSERT INTO runs(issue_id, mode, status, actor, started_at) VALUES(?,?,?,?,?)",
                   (ok(a("GET", f"/api/issues/{ks[1]}"))["id"], "execute", "running", "human:admin", db.now_iso()))
    r = human("POST", f"/api/issues/{ks[0]}/complete-tree", json={})
    assert r.status_code == 409 and ks[1] in r.json()["detail"], r.text
    assert [st(x) for x in (top, *ks)] == ["backlog", "backlog", "backlog", "closed"]   # 아무것도 안 바뀜

    # 일반 종결은 선택한 하위 트리만 동기화하고 재요청의 이벤트를 중복하지 않는다.
    status = lambda ref, value: human('POST', f'/api/issues/{ref}/status', json={'status': value, 'note': '사람 메모'})
    for terminal in ('done', 'closed'):
        top, ks = tree()
        grand = ok(human('POST', '/api/issues', json={'project': 'DEV', 'title': '손자', 'parent': ks[0]}))['ref']
        ok(status(grand, 'closed' if terminal == 'done' else 'done'))
        assert a('POST', f'/api/issues/{top}/status', json={'status': terminal}).status_code == 403
        ok(status(ks[0], terminal))
        assert st(top) == 'backlog' and st(ks[1]) == 'backlog' and st(ks[2]) == 'closed'
        assert st(grand) == terminal
        with db.connect() as cx:
            cx.execute("UPDATE issues SET claimed_by='agent:1', lease_until=? WHERE id=?", ('2099-01-01', issues.get_issue(ks[1])['id']))
        ok(status(top, terminal))
        for ref in (top, *ks, grand):
            item = issues.get_issue(ref)
            assert item['status'] == terminal and item['closed_at'] and item['claimed_by'] is None and item['lease_until'] is None
        ev = issues.get_issue(ks[1])['events'][-1]
        assert ev['data'] == {'from': 'backlog', 'to': terminal, 'tree_from': top} and ev['body'] == '사람 메모'
        before = {ref: issues.get_issue(ref) for ref in (top, *ks, grand)}
        ok(status(top, terminal))
        assert before == {ref: issues.get_issue(ref) for ref in before}
        ok(status(grand, 'backlog'))
        ok(status(top, terminal))
        assert st(grand) == terminal and issues.get_issue(top)['events'] == before[top]['events']

    # 실행 중 후손이 있으면 결정·상태·이벤트 모두 롤백한다.
    top, ks = tree()
    ok(a('POST', f'/api/issues/{top}/plans', json={'body': '거절 검사'}))
    with db.connect() as cx:
        run_id = cx.execute("INSERT INTO runs(issue_id, mode, status, actor, started_at) VALUES(?,?,?,?,?)",
                            (issues.get_issue(ks[0])['id'], 'execute', 'running', 'human:admin', db.now_iso())).lastrowid
    before = {ref: issues.get_issue(ref) for ref in (top, *ks)}
    for terminal in ('done', 'closed'):
        assert status(top, terminal).status_code == 409
        assert before == {ref: issues.get_issue(ref) for ref in before}
    assert dec(top, 'reject', '거절 메모', 1).status_code == 409
    assert before == {ref: issues.get_issue(ref) for ref in before}
    with db.connect() as cx:
        cx.execute("UPDATE runs SET status='ok' WHERE id=?", (run_id,))
    ok(dec(top, 'reject', '거절 메모', 1))
    assert all(st(ref) == 'closed' for ref in before)
    assert issues.get_issue(ks[0])['events'][-1]['body'] == '거절 (계획서 v1): 거절 메모'

    # 중간 이벤트 쓰기 실패도 이미 바꾼 부모와 결정을 되돌린다.
    from unittest.mock import patch
    top, ks = tree()
    ok(a('POST', f'/api/issues/{top}/plans', json={'body': '원자성 검사'}))
    before = {ref: issues.get_issue(ref) for ref in (top, *ks)}
    original_event = issues._event
    def fail_child(cx, issue_id, *args, **kwargs):
        if issue_id == before[ks[0]]['id']:
            raise issues.StoreError('event failed', 409)
        return original_event(cx, issue_id, *args, **kwargs)
    with patch.object(issues, '_event', side_effect=fail_child):
        assert status(top, 'done').status_code == 409
        assert dec(top, 'reject', '실패', 1).status_code == 409
    assert before == {ref: issues.get_issue(ref) for ref in before}

    # 프로젝트 삭제 — 빈 프로젝트, 사람 권한, 확인 후 내용 변경, 실행 보호와 FK 정리.
    ok(human('POST', '/api/projects', json={'key': 'DEL', 'name': '삭제 검사',
                                          'repo_url': 'https://example.com/repo', 'local_path': '/untouched'}))
    check = lambda: ok(human('GET', '/api/projects/del/delete-check'))
    remove = lambda token: human('DELETE', '/api/projects/DEL', json={'confirmation_token': token})
    empty = check()
    assert empty['issue_count'] == 0 and empty['can_delete'] and not empty['confirmation_required']
    assert a('GET', '/api/projects/DEL/delete-check').status_code == 403
    assert a('DELETE', '/api/projects/DEL', json={}).status_code == 403
    item = ok(human('POST', '/api/issues', json={'project': 'DEL', 'title': '삭제 대상', 'labels': ['goal']}))
    task = ok(human('POST', '/api/issues', json={'project': 'DEL', 'title': 'Task', 'parent': item['ref']}))
    ok(human('POST', f"/api/issues/{task['ref']}/status", json={'status': 'closed'}))
    assert remove(empty['confirmation_token']).status_code == 409
    assert human('DELETE', '/api/projects/DEL').status_code == 409
    initial = check()
    assert initial['issue_count'] == 2 and initial['confirmation_required'] and initial['warning']
    ok(human('POST', f"/api/issues/{item['ref']}/comments", json={'body': '확인 후 새 기록'}))
    assert remove(initial['confirmation_token']).status_code == 409
    ok(a('POST', f"/api/issues/{item['ref']}/claim"))
    assert not check()['can_delete'] and remove(check()['confirmation_token']).status_code == 409
    with db.connect() as cx:
        cx.execute("UPDATE issues SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?", (item['id'],))
        rid = cx.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at) VALUES(?,'review','running','human:admin',?)",
                         (item['id'], db.now_iso())).lastrowid
    assert not check()['can_delete'] and remove(check()['confirmation_token']).status_code == 409
    with db.connect() as cx:
        cx.execute("UPDATE runs SET status='ok' WHERE id=?", (rid,))
        cx.execute("INSERT INTO jobs(issue_id,mode,status,actor,created_at) VALUES(?,'review','queued','human:admin',?)", (item['id'], db.now_iso()))
        cx.execute("INSERT INTO issue_deps(issue_id,blocked_by_id) VALUES(?,?)", (d1['id'], item['id']))
        cx.execute("INSERT INTO plans(issue_id,version,body,author,created_at) VALUES(?,1,'plan','human:admin',?)", (item['id'], db.now_iso()))
        cx.execute("INSERT INTO decisions(issue_id,gate,plan_version,verdict,actor,created_at) VALUES(?,'plan',1,'approve','human:admin',?)", (item['id'], db.now_iso()))
    ready = check()
    assert ready['can_delete'] and ready['external_dependency_count'] == 1
    # 삭제 도중 실패하면 이슈와 연쇄 기록까지 되돌린다.
    with db.connect() as cx:
        cx.execute("CREATE TRIGGER fail_project_delete BEFORE DELETE ON projects WHEN OLD.key='DEL' BEGIN SELECT RAISE(ABORT, 'test rollback'); END")
    import sqlite3
    try:
        issues.delete_project({'kind': 'human', 'name': 'admin'}, 'DEL', ready['confirmation_token'])
        assert False, '삭제 실패를 예상한다'
    except sqlite3.IntegrityError:
        pass
    assert check() == ready
    with db.connect() as cx:
        cx.execute('DROP TRIGGER fail_project_delete')
    # 시작이 먼저 커밋되면 삭제는 거부한다.
    with db.connect() as cx:
        cx.execute("UPDATE runs SET status='running' WHERE id=?", (rid,))
    assert remove(ready['confirmation_token']).status_code == 409
    with db.connect() as cx:
        cx.execute("UPDATE runs SET status='ok' WHERE id=?", (rid,))
    assert remove(check()['confirmation_token']).status_code == 204
    assert human('GET', '/api/projects/DEL/delete-check').status_code == 404
    assert human('DELETE', '/api/projects/DEL').status_code == 404
    assert ok(human('GET', '/api/issues/DEV-1'))['id'] == d1['id']
    with db.connect() as cx:
        for table in ('issues', 'plans', 'events', 'decisions', 'runs', 'jobs'):
            column = 'id' if table == 'issues' else 'issue_id'
            assert cx.execute(f'SELECT COUNT(*) FROM {table} WHERE {column} IN (?,?)', (item['id'], task['id'])).fetchone()[0] == 0
        assert cx.execute('SELECT COUNT(*) FROM issue_deps WHERE blocked_by_id=?', (item['id'],)).fetchone()[0] == 0
        assert not cx.execute('PRAGMA foreign_key_check').fetchall()
    ok(human('POST', '/api/projects', json={'key': 'EMPTY', 'name': '빈 프로젝트'}))
    assert human('DELETE', '/api/projects/EMPTY').status_code == 204

    # 삭제가 먼저 잠금을 잡으면 늦게 시작한 작업은 FK 검증에서 거부된다.
    import threading
    from concurrent.futures import ThreadPoolExecutor
    ok(human('POST', '/api/projects', json={'key': 'RACE', 'name': '동시성'}))
    race = ok(human('POST', '/api/issues', json={'project': 'RACE', 'title': '대상'}))
    race_token = ok(human('GET', '/api/projects/RACE/delete-check'))['confirmation_token']
    locked, starting, release = threading.Event(), threading.Event(), threading.Event()
    original_deletion = issues._project_deletion
    def paused_deletion(cx, key):
        result = original_deletion(cx, key)
        locked.set()
        assert release.wait(5)
        return result
    def late_start():
        starting.set()
        with db.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            cx.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at) VALUES(?,'review','running','human:admin',?)", (race['id'], db.now_iso()))
    with ThreadPoolExecutor(max_workers=2) as pool:
        with patch.object(issues, '_project_deletion', side_effect=paused_deletion):
            deletion = pool.submit(issues.delete_project, {'kind': 'human', 'name': 'admin'}, 'RACE', race_token)
            assert locked.wait(5)
            start = pool.submit(late_start)
            assert starting.wait(5)
            release.set()
            deletion.result(timeout=5)
            try:
                start.result(timeout=5)
                assert False, '삭제된 이슈의 실행 기록은 만들 수 없다'
            except sqlite3.IntegrityError:
                pass
    assert human('GET', '/api/projects/RACE/delete-check').status_code == 404

    # 쓰기 요청의 형식 오류
    assert c.post("/api/issues", content=b"not json", headers={**H, "content-type": "application/json"}).status_code == 400
# 자동 분류는 사람 선택과 수정 판을 보존한다.
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
human_actor = {"kind": "human", "name": "admin"}
auto_actor = {"kind": "agent", "id": 1, "name": "claude"}
def untyped():
    return issues.create_issue(human_actor, "DEV", "자동 분류 검사")["ref"]
def rejects(fn, status=400):
    try:
        fn()
        raise AssertionError("must reject")
    except issues.StoreError as e:
        assert e.status == status
ref = untyped()
rev = issues.get_issue(ref)["type_revision"]
for ids in ([], [99999], [True]):
    rejects(lambda: issues.classify_issue(auto_actor, ref, ids, rev))
rejects(lambda: issues.classify_issue(human_actor, ref, [1], rev), 403)
# 이벤트 기록 실패는 연결까지 롤백한다.
with patch.object(issues, "_event", side_effect=issues.StoreError("event failed")):
    rejects(lambda: issues.classify_issue(auto_actor, ref, [1], rev))
assert issues.get_issue(ref)["type_ids"] == []
# 동시 저장에서는 하나만 성공한다.
with ThreadPoolExecutor(max_workers=2) as pool:
    results = list(pool.map(lambda ids: issues.classify_issue(auto_actor, ref, ids, rev), ([1, 2, 1], [3])))
assert sum(r["saved"] for r in results) == 1
current = issues.get_issue(ref)
assert all(t["source"] == "agent" for t in current["types"])
with db.connect() as conn:
    assert all(r[0] == "agent:1" for r in conn.execute("SELECT actor FROM issue_type_links WHERE issue_id=?", (current["id"],)))
assert not issues.classify_issue(auto_actor, ref, [4], rev)["saved"]
issues.update_issue(human_actor, ref, {"type_ids": current["type_ids"]})
assert all(t["source"] == "human" for t in issues.get_issue(ref)["types"])
ref = untyped()
rev = issues.get_issue(ref)["type_revision"]
issues.update_issue(human_actor, ref, {"type_ids": [1]})
issues.update_issue(human_actor, ref, {"type_ids": []})
rejects(lambda: issues.classify_issue(auto_actor, ref, [2], rev), 409)
rev = issues.get_issue(ref)["type_revision"]
issues.update_issue(human_actor, ref, {"type_ids": []})
rejects(lambda: issues.classify_issue(auto_actor, ref, [2], rev), 409)
rejects(lambda: issues.update_issue(auto_actor, ref, {"type_ids": [2]}), 403)
ref = issues.create_issue(human_actor, "DEV", "목표", labels=["goal"])["ref"]
rejects(lambda: issues.classify_issue(auto_actor, ref, [1], 0), 403)

# MCP 계약에서도 카탈로그 조회와 복수 저장, 오류 전달을 확인한다.
import asyncio
from fastmcp import Client
from fastmcp.exceptions import ToolError
import mcp_tools
async def check_classification_mcp():
    ref = untyped()
    with patch.object(mcp_tools, "_actor", return_value=auto_actor):
        async with Client(mcp_tools.mcp) as client:
            catalog = (await client.call_tool("list_issue_types", {})).data
            issue = (await client.call_tool("get_issue", {"ref": ref})).data
            args = {"ref": ref, "type_ids": [t["id"] for t in catalog[:2]], "expected_revision": issue["type_revision"]}
            assert (await client.call_tool("classify_issue", args)).data["saved"]
            assert not (await client.call_tool("classify_issue", args)).data["saved"]
            try:
                await client.call_tool("classify_issue", {**args, "ref": untyped(), "type_ids": []})
                raise AssertionError("empty classification must reject")
            except ToolError:
                pass
asyncio.run(check_classification_mcp())
print("OK")
