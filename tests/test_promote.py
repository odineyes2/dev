"""상위 이슈 승격(DEV-40-3) — 하위 Task가 전부 병합되면 상위가 in_review와 요약 노트, Task 줄 상태 문구."""
import json, os, subprocess, sys, tempfile
from pathlib import Path

os.environ["DEV_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import db, issues, orchestrate  # noqa: E402

repo = Path(tempfile.mkdtemp())
me = {"kind": "human", "name": "admin"}


def git(*a):
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def branch(ref, name):
    git("checkout", "-q", "-b", f"relay/{ref}", "main")
    (repo / name).write_text(ref)
    git("add", "."); git("commit", "-qm", ref); git("checkout", "-q", "main")


git("init", "-q", "-b", "main")
(repo / "a.txt").write_text("a\n"); git("add", "."); git("commit", "-qm", "init")
db.init()
issues.create_project(me, "TP", "test", local_path=str(repo))
p = issues.create_issue(me, "TP", "상위")["ref"]
t1 = issues.create_issue(me, "TP", "하나", parent=p)["ref"]
t2 = issues.create_issue(me, "TP", "둘", parent=p)["ref"]

# 첫 Task만 병합 — 상위는 그대로
branch(t1, "b.txt")
issues.set_status(me, t1, "in_review", "목록 화면에서 b 확인")
assert orchestrate.merge(str(repo), t1)[0] == "merged"
issues.add_comment(me, t1, "🔁 재시작이 필요 없는 변경이라 재시작하지 않았어요.")
assert not orchestrate.promote_parent(me, t1)
assert issues.get_issue(p)["status"] == "backlog"

# 둘째가 in_review지만 아직 병합 전 — 상위는 그대로
branch(t2, "c.txt")
WARN = "⚠️ 서버 파일이 바뀌었지만(server/a.py) 자동 재시작 설정이 없어 재시작하지 않았어요 — 운영 서버를 직접 재시작해야 반영돼요."
issues.set_status(me, t2, "in_review", "설정 화면에서 c 확인" + "." * 700 + "\n\n🔁 " + WARN)   # NS-60: 600자 뒤 경고
assert not orchestrate.promote_parent(me, t2)

# 둘 다 병합 → 상위 in_review, 노트에 병합 커밋·재시작 결과·확인할 곳
assert orchestrate.merge(str(repo), t2)[0] == "merged"
with db.connect() as c:
    iid = issues.get_issue(t2)['id']
    rid = c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at) VALUES(?,'execute','ok','human:admin',?)", (iid, db.now_iso())).lastrowid
    c.execute("INSERT INTO execution_completion(run_id,issue_id,ref,actor,task_sha,auto_merge,phase,owner_event,cfg_json,updated_at) VALUES(?,?,?,'human:admin',?,1,'restart_requested',0,'{}',?)", (rid, iid, t2, git('rev-parse', 'relay/' + t2), db.now_iso()))
for phase in orchestrate.ACTIVE_PHASES + ('failed', 'held', 'abandoned'):
    with db.connect() as c:
        c.execute('UPDATE execution_completion SET phase=? WHERE run_id=?', (phase, rid))
    assert not orchestrate.promote_parent(me, t2), phase
    assert issues.get_issue(p)['status'] == 'backlog'
with db.connect() as c:
    c.execute("UPDATE execution_completion SET phase='complete' WHERE run_id=?", (rid,))
assert orchestrate.promote_parent(me, t2)
par = issues.get_issue(p)
assert par["status"] == "in_review"
note = par["events"][-1]["body"]
sha1 = git("log", "-1", "--merges", "--format=%h", f"--grep=({t1})", "main")
assert sha1 in note and "재시작하지 않았어요" in note and "목록 화면에서 b 확인" in note and "설정 화면에서 c 확인" in note, note
assert note.startswith("**⚠️ 직접 확인할 것**\n- **" + t2 + "**: " + WARN), note   # 잘리지 않고 맨 위
assert not orchestrate.promote_parent(me, t2)   # 이미 올라가 있으면 다시 안 함

# Task 줄 상태 문구
ev = lambda kind, body, to=None: {"kind": kind, "body": body, "data": {"to": to} if to else {}}   # noqa: E731
st = lambda *es, **kw: orchestrate.merge_state({"events": list(es), **kw})   # noqa: E731
assert st(ev("comment", "🔀 병합했어요")) == "병합됨"
assert st(ev("comment", "🔀 병합했어요"), ev("comment", "⏳ 끝나면 재시작")) == "재시작 대기"
assert st(ev("comment", "⏳ 끝나면 재시작"), ev("comment", "🔁 재시작했어요")) == "병합됨"
assert st(ev("status", "🔀 자동 병합: 병합 뒤 테스트가 실패해 revert 커밋으로 되돌렸어요.", "changes_requested")) == "되돌림"
assert st(ev("status", "🔀 자동 병합: 선행 Task가 아직 병합되지 않아 병합 보류.", "on_hold")) == "병합 대기"
assert st(ev("comment", "🔀 병합했어요"), ev("comment", "🛠 실행을 맡겼어요")) is None
assert st(status="in_review", parent_ref=p, project_key="TP") == "병합 대기"   # 기본 켜짐
assert st(status="in_review", parent_ref=p, project_key="TP", ref="TP-99-9") == "병합 대기"   # 브랜치가 base에 없음
assert st(status="in_review", parent_ref=p, project_key="TP", ref=t1) == "병합됨"   # 흔적이 없어도 실제로 병합됐으면(NS-32-2)
orchestrate.SETTINGS.write_text(json.dumps({"TP": {"auto_merge": False}}))
assert st(status="in_review", parent_ref=p, project_key="TP") is None

# 수정 요청 뒤 새 판(DEV-86): v1 Task가 다 끝나도 v2가 결정 대기면 올리지 않고, v2 승인은 새 Task를 만든다.
q = issues.create_issue(me, "TP", "두 판")["ref"]
issues.post_plan(me, q, "## Tasks\n1. 첫 판 일 | 파일: a.txt")
issues.decide(me, q, "approve", plan_version=1)
first = issues.get_issue(q)["children"]
assert len(first) == 1
issues.set_status(me, first[0]["ref"], "done")
issues.set_status(me, q, "triage")
issues.post_plan(me, q, "## Tasks\n1. 수정 요청 반영 | 파일: a.txt")   # v2 — 아직 결정 전
assert not orchestrate.promote_parent(me, first[0]["ref"]) and issues.get_issue(q)["status"] == "triage"
issues.decide(me, q, "approve", plan_version=2)
kids = issues.get_issue(q)["children"]
assert len(kids) == 2 and kids[-1]["title"] == "수정 요청 반영" and kids[-1]["status"] == "backlog", kids
assert "하위 Task 1개를 만들었어요" in issues.get_issue(q)["events"][-1]["body"]
issues.decide(me, q, "approve", plan_version=2)   # 같은 판 재승인은 다시 만들지 않는다
assert len(issues.get_issue(q)["children"]) == 2

print("ok")
