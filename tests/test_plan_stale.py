"""계획서 기준 커밋(plans.base_sha)과 낡음 알림 — 계획서가 쓰인 뒤 다른 이슈가 Task의 파일을 바꿨으면 실행 프롬프트 앞에 알린다.
같은 부모의 Task 커밋·병합, Task 파일 밖 변경, 기준이 없는 옛 계획서는 알리지 않는다."""
import os, subprocess, sys, tempfile
from pathlib import Path

os.environ["DEV_DATA_DIR"] = tempfile.mkdtemp()
os.environ.pop("NTFY_TOPIC", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import db, execute, issues  # noqa: E402

repo = Path(tempfile.mkdtemp()) / "repo"


def git(*a):
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def commit(name, subject):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(path.read_text() + "x\n" if path.exists() else "x\n")
    git("add", "."); git("commit", "-qm", subject)
    return git("rev-parse", "--short", "HEAD")


repo.mkdir(parents=True)
git("init", "-q", "-b", "main")
commit("server/db.py", "init")

me = {"kind": "human", "id": 1, "name": "admin", "model": None}
db.init()
issues.create_project(me, "PS", "stale", "", str(repo))
parent = issues.create_issue(me, "PS", "부모")["ref"]
plan = issues.post_plan(me, parent, "계획")
assert plan["base_sha"] == git("rev-parse", "HEAD")
issues.decide(me, parent, "approve", plan_version=1)
task = issues.create_issue(me, "PS", "Task", parent=parent, body="바꿀 파일: server/db.py, static/ | 확인: 검사")["ref"]


def stale():
    issue = issues.get_issue(task)
    return execute.stale_commits(str(repo), issue, issues.get_issue(issue["parent_ref"]))


assert stale() == []
commit("README.md", "다른 파일 (Q-1-1)")                       # Task 파일 밖
commit("server/db.py", "같은 부모의 앞 Task (PS-1-1)")            # 이 부모의 Task
commit("static/app.js", "Merge relay/PS-1-2 (PS-1-2)")             # 이 부모의 병합
assert stale() == []
other = commit("server/db.py", "다른 이슈의 마이그레이션 (Q-2-1)")
also = commit("static/x.css", "relay/PS-10-1 — PS-10의 Task (PS-10-1)")   # 접두어만 같은 다른 부모
found = stale()
assert [line.split()[0] for line in found] == [also, other], found
notice = execute.stale_notice(found, 1)
assert "v1" in notice and other in notice and "on_hold" in notice and "blocked" in notice

# 새 판(새 기준)을 승인하면 그 뒤 커밋만 본다
issues.post_plan(me, parent, "계획 v2")
issues.decide(me, parent, "approve", plan_version=2)
assert stale() == []

# 기준이 없는 옛 계획서·저장소가 없는 프로젝트는 보지 않는다
with db.connect() as c:
    c.execute("UPDATE plans SET base_sha=''")
commit("server/db.py", "또 다른 이슈 (Q-3-1)")
assert stale() == []
assert execute.base_sha("") == "" and execute.base_sha(str(repo.parent)) == ""
print("ok")
