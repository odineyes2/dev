"""대기열 꺼낼 순서와 파일 겹침 대기 — 시작한 부모의 Task를 먼저, 그와 파일이 겹치는 다른 부모의 Task는 기다린다.
진행 중인 부모가 사람을 기다리면(on_hold·changes_requested) 예약을 푼다. 검토는 실행 뒤에."""
import os, sys, tempfile
from pathlib import Path

os.environ["DEV_DATA_DIR"] = tempfile.mkdtemp()
os.environ.pop("NTFY_TOPIC", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import db, issues, jobs, review  # noqa: E402

me = {"kind": "human", "id": 1, "name": "admin", "model": None}
db.init()
issues.create_project(me, "OQ", "order", "", "/nowhere")
a, b, c_, r = (issues.create_issue(me, "OQ", t)["ref"] for t in ("A", "B", "C", "검토할 것"))
a1 = issues.create_issue(me, "OQ", "A1", parent=a, body="바꿀 파일: server/db.py")["ref"]
a2 = issues.create_issue(me, "OQ", "A2", parent=a, body="바꿀 파일: server/db.py, static/")["ref"]
b1 = issues.create_issue(me, "OQ", "B1", parent=b, body="바꿀 파일: static/app.js")["ref"]      # A2의 static/ 아래
c1 = issues.create_issue(me, "OQ", "C1", parent=c_, body="바꿀 파일: README.md")["ref"]         # 겹치지 않음


def iid(ref):
    return issues.get_issue(ref)["id"]


def status(ref, s):
    with db.connect() as c:
        c.execute("UPDATE issues SET status=? WHERE id=?", (s, iid(ref)))


# 대기열에 넣은 순서: 검토, B1, C1, A2
queue = [{"id": n, "issue_id": iid(ref), "mode": mode} for n, (ref, mode) in
         enumerate([(r, "review"), (b1, "execute"), (c1, "execute"), (a2, "execute")], 1)]


def plan():
    with db.connect() as c:
        ordered, notes = jobs.schedule(c, queue)
    return [j["id"] for j in ordered], notes


# 아직 아무 부모도 시작 안 함 — 실행 먼저, 검토 뒤(같은 묶음 안은 넣은 순서), 기다림 없음
assert plan() == ([2, 3, 4, 1], {})

# A1이 실행돼 끝남(done) → A는 진행 중. A2가 맨 앞, A2와 겹치는 B1은 기다리고 C1은 진행
with db.connect() as c:
    c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at) VALUES(?,'execute','ok','human:admin',?)", (iid(a1), db.now_iso()))
status(a1, "done")
order, notes = plan()
assert order == [4, 2, 3, 1], order
assert list(notes) == [2] and a in notes[2] and "static/app.js" in notes[2], notes

# A의 남은 Task가 사람을 기다리면 예약을 푼다
status(a2, "changes_requested")
assert plan() == ([2, 3, 4, 1], {})
status(a2, "waiting")
assert list(plan()[1]) == [2]

# 다른 프로젝트의 같은 상대 경로는 예약된 파일과 겹치지 않는다.
issues.create_project(me, "OX", "other", "", "/nowhere2")
x = issues.create_issue(me, "OX", "X")["ref"]
x1 = issues.create_issue(me, "OX", "X1", parent=x, body="바꿀 파일: static/app.js")["ref"]
cross_project = {"id": 5, "issue_id": iid(x1), "mode": "execute"}
with db.connect() as c:
    ordered, notes = jobs.schedule(c, queue + [cross_project])
assert [j["id"] for j in ordered] == [4, 2, 3, 5, 1]
assert list(notes) == [2] and a in notes[2] and 5 not in notes, notes

# A가 다 끝나면 예약 없음
status(a2, "done")
assert plan() == ([2, 3, 4, 1], {})

assert jobs._overlap({"static"}, {"static/x.js"}) == ["static"] and jobs._overlap({"a.py"}, {"ab.py"}) == []
with db.connect() as c:
    assert jobs.schedule(c, []) == ([], {})
print("ok")

# 검토 프롬프트(Task 4) — 같은 프로젝트의 진행 중인 이슈와 남은 Task 파일을 알린다. 사람을 기다리는 이슈도 넣는다.
assert "진행 중인 다른 이슈" not in review.prompt_for(r)          # A가 다 끝남
status(a2, "changes_requested")
prompt = review.prompt_for(r)
assert f"{a} A — 남은 Task 파일: server/db.py, static/" in prompt, prompt[-600:]
assert "진행 중인 다른 이슈" not in review.prompt_for(a)          # 자기 자신은 빼고
assert "진행 중인 다른 이슈" not in review.prompt_for(issues.create_issue(me, "OX", "다른 프로젝트")["ref"])
assert "진행 중인 다른 이슈" in review.codex_command_for(r)[-1]
print("ok review")

# DEV-145 — 계획서만 있고 실행 전인 이슈도 계획 Task 파일과 함께 알린다. 거절된 계획은 빼고, 실행 대기열 예약에는 넣지 않는다.
status(a2, "done")
p, q, s = (issues.create_issue(me, "OQ", t)["ref"] for t in ("P", "Q", "S"))
issues.post_plan(me, p, "## 목표\nP\n\n## Tasks\n1. P1 | 파일: server/review.py, tests/test_p.py | 확인: python tests/test_p.py | 선행: 없음\n")
prompt = review.prompt_for(r)
assert f"{p} P — 실행 전 계획(승인 대기), 계획 Task 파일: server/review.py, tests/test_p.py" in prompt, prompt[-800:]
assert f"{p} P" not in review.prompt_for(p)          # 자기 자신은 빼고
with db.connect() as c:
    assert iid(p) not in jobs._reservations(c, keep_stalled=True) and iid(p) not in jobs._reservations(c)
issues.decide(me, p, "approve", plan_version=1)          # 승인하면 하위 Task가 생기고 그 본문의 파일을 쓴다
assert f"{p} P — 실행 전 계획(승인됨), 계획 Task 파일: server/review.py, tests/test_p.py" in review.prompt_for(r)
issues.post_plan(me, q, "## Tasks\n1. Q1 | 파일: static/q.js | 확인: 없음 | 선행: 없음\n")
issues.decide(me, q, "reject", note="안 함", plan_version=1)
issues.post_plan(me, s, "## 목표\n파일 없는 계획\n")
prompt = review.prompt_for(r)
assert f"{q} Q" not in prompt and f"{s} S" not in prompt, prompt[-800:]
assert f"{p} P" not in review.prompt_for(issues.create_issue(me, "OX", "다른 프로젝트 2")["ref"])
print("ok review planned")
