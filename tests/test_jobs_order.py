"""대기열 꺼낼 순서와 파일 겹침 대기 — 시작한 부모의 Task를 먼저, 그와 파일이 겹치는 다른 부모의 Task는 기다린다.
진행 중인 부모가 사람을 기다리면(on_hold·changes_requested) 예약을 푼다. 검토는 실행 뒤에."""
import os, sys, tempfile
from pathlib import Path

os.environ["DEV_DATA_DIR"] = tempfile.mkdtemp()
os.environ.pop("NTFY_TOPIC", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import db, issues, jobs  # noqa: E402

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

# A가 다 끝나면 예약 없음
status(a2, "done")
assert plan() == ([2, 3, 4, 1], {})

assert jobs._overlap({"static"}, {"static/x.js"}) == ["static"] and jobs._overlap({"a.py"}, {"ab.py"}) == []
with db.connect() as c:
    assert jobs.schedule(c, []) == ([], {})
print("ok")
