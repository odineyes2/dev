"""NS-60 뒤 보강 — 계획서 '사람이 할 일'이 Done을 막는지, 새 라우트 무검사 경고, 끝난 Task worktree 정리."""
import os, subprocess, sys, tempfile
from pathlib import Path

os.environ["DEV_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import db, execute, issues  # noqa: E402

repo = Path(tempfile.mkdtemp())
me = {"kind": "human", "name": "admin"}
agent = {"kind": "agent", "id": 1, "name": "claude-main"}


def git(*a, cwd=repo):
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


git("init", "-q", "-b", "main")
(repo / "a.py").write_text("a\n"); git("add", "."); git("commit", "-qm", "init")
db.init()
issues.create_project(me, "TP", "test", local_path=str(repo))

# 1) 계획서 절 파싱 — 목록 기호·체크 칸·번호를 떼고, 다음 절에서 멈춘다
body = "## Tasks\n1. 일 | 파일: a.py\n\n## 사람이 할 일\n- [ ] 운영 재시작\n2. 로그인해서 확인\n- 없음\n\n## 크기\n작음"
assert issues.human_checks(body) == ["운영 재시작", "로그인해서 확인"], issues.human_checks(body)
assert issues.human_checks("## Tasks\n1. 일") == []

# 2) 확인 없이는 Done 거절(set_status·complete_tree 둘 다), 확인하면 통과
p = issues.create_issue(me, "TP", "상위")["ref"]
issues.post_plan(agent, p, body)
assert issues.get_issue(p)["human_checks"] == ["운영 재시작", "로그인해서 확인"]
for call in (lambda **k: issues.set_status(me, p, "done", **k), lambda **k: issues.complete_tree(me, p, **k)):
    try:
        call()
        raise AssertionError("확인 없이 Done이 됐다")
    except issues.StoreError as e:
        assert e.status == 409 and "운영 재시작" in str(e), e
assert issues.set_status(me, p, "closed", "중복")["status"] == "closed"   # 닫기(안 하기로 함)는 막지 않는다
q = issues.create_issue(me, "TP", "둘째")["ref"]
issues.post_plan(agent, q, body)
assert issues.complete_tree(me, q, confirm_checks=True)[0]["status"] == "done"
r = issues.create_issue(me, "TP", "계획서 없음")["ref"]
assert issues.set_status(me, r, "done")["status"] == "done"

# 3) 새 라우트를 더했는데 tests/에 /api/ 요청이 없으면 경고
wt = Path(tempfile.mkdtemp()) / "w"
git("worktree", "add", "-q", str(wt), "-b", "relay/TP-9")
(wt / "a.py").write_text('a\n@app.get("/api/x")\ndef x(): pass\n'); git("commit", "-qam", "route", cwd=wt)
assert execute.untested_routes(wt)
(wt / "tests").mkdir(); (wt / "tests" / "test_x.py").write_text('assert c.get("/api/x").status_code == 200\n')
git("add", ".", cwd=wt); git("commit", "-qm", "test", cwd=wt)
assert not execute.untested_routes(wt)

# 4) worktree 정리 — Done인 Task만, 커밋 안 된 추적 변경이 있으면 둔다, 브랜치는 남긴다
execute.WORKTREE_DIR.mkdir(parents=True, exist_ok=True)
t_done = issues.create_issue(me, "TP", "끝난 Task", parent=r)["ref"]
t_dirty = issues.create_issue(me, "TP", "변경 남은 Task", parent=r)["ref"]
t_open = issues.create_issue(me, "TP", "진행 중 Task", parent=r)["ref"]
for t in (t_done, t_dirty, t_open):
    git("worktree", "add", "-q", str(execute.worktree_path(t)), "-b", f"relay/{t}")
(execute.worktree_path(t_done) / "junk.png").write_text("x")   # 미추적 산출물은 지운다
(execute.worktree_path(t_dirty) / "a.py").write_text("changed\n")
for t in (t_done, t_dirty):
    issues.set_status(me, t, "done")
assert execute.prune_worktrees() == [t_done]
assert not execute.worktree_path(t_done).exists() and execute.worktree_path(t_dirty).exists() and execute.worktree_path(t_open).exists()
assert git("branch", "--list", f"relay/{t_done}")
print("ok")
