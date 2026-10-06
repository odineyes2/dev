"""재실행 worktree 최신화 — 옛 worktree를 이어 쓸 때 서버가 최신 main을 먼저 병합한다(DEV-81-1·DEV-40-2).
무충돌이면 그대로, 충돌이면 병합 중으로 두고 프롬프트에 해결 지시, 커밋 안 된 변경은 보존 커밋, 충돌 미해결이면 완료 거부."""
import os, subprocess, sys, tempfile
from pathlib import Path

os.environ["DEV_DATA_DIR"] = tempfile.mkdtemp()
os.environ.pop("NTFY_TOPIC", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import execute  # noqa: E402

tmp = Path(tempfile.mkdtemp())
repo = tmp / "repo"


def git(cwd, *a, check=True):
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    assert not check or r.returncode == 0, r.stderr
    return r.stdout.strip()


def head(cwd):
    return git(cwd, "rev-parse", "HEAD")


def main_commit(name, text):
    (repo / name).write_text(text)
    git(repo, "add", "."); git(repo, "commit", "-qm", "main " + name)


repo.mkdir()
git(repo, "init", "-q", "-b", "main")
(repo / "db.py").write_text("M = [\n    'one',\n]\n")
git(repo, "add", "."); git(repo, "commit", "-qm", "init")

# 1) base가 그대로면 아무것도 안 한다
wt = execute.prepare_worktree(str(repo), "R-1")
start, conflicts = execute.refresh_worktree(wt, "R-1")
assert start == head(wt) and conflicts == []

# 2) Task 커밋 뒤 main이 다른 파일로 앞서면 무충돌 병합 — 시작점은 병합 전 HEAD
(wt / "task.py").write_text("t\n"); git(wt, "add", "."); git(wt, "commit", "-qm", "task (R-1)")
before = head(wt)
main_commit("other.py", "o\n")
start, conflicts = execute.refresh_worktree(wt, "R-1")
assert start == before and conflicts == [] and (wt / "other.py").exists()
assert git(wt, "merge-base", "--is-ancestor", "main", "HEAD", check=False) == ""
assert git(wt, "log", "-1", "--format=%an") == "dev orchestrator"
# Task 변경만 범위 검사에 잡힌다(병합해 온 other.py는 아님)
assert git(wt, "diff", "--name-only", "main...HEAD").splitlines() == ["task.py"]

# 3) 커밋 안 된 변경 + 같은 줄 충돌 → 보존 커밋 뒤 병합 중, 충돌 파일을 알려 준다
(wt / "db.py").write_text("M = [\n    'one',\n    'task',\n]\n")   # 커밋 안 함(실패한 Codex 실행처럼)
main_commit("db.py", "M = [\n    'one',\n    'other',\n]\n")
before = head(wt)
start, conflicts = execute.refresh_worktree(wt, "R-1")
assert start == before and conflicts == ["db.py"], conflicts
assert "보존" in git(wt, "log", "-1", "--format=%s") and "(R-1)" in git(wt, "log", "-1", "--format=%s")
assert git(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False)
notice = execute.conflict_notice(conflicts)
assert "`db.py`" in notice and "main" in notice
cmd = ["claude", "-p", "원래", "--x"]
assert execute.with_notice(cmd, "claude", notice)[2] == notice + "원래" and execute.with_notice(cmd, "codex", notice)[-1] == notice + "--x"
assert execute.with_notice(cmd, "claude", "") is cmd

# 3-1) 실행이 충돌을 못 풀고 끝나 다시 시작하면 병합 중 상태를 그대로 알려 준다(보존 커밋을 시도하다 막히지 않는다)
again, still = execute.refresh_worktree(wt, "R-1")
assert again == head(wt) and still == ["db.py"]

# 4) 완료 전 검사 — 병합 중이거나 충돌 표시가 커밋에 남으면 거부, 해결하면 통과
assert "병합이 끝나지 않았어요" in execute.merge_unfinished(wt)
git(wt, "add", "db.py")
git(wt, "commit", "-qm", "충돌 표시 채로 커밋 (R-1)")
assert "`db.py`" in execute.merge_unfinished(wt)
(wt / "db.py").write_text("M = [\n    'one',\n    'other',\n    'task',\n]\n")
git(wt, "commit", "-qam", "충돌 해결 (R-1)")
assert execute.merge_unfinished(wt) is None
assert execute.conflict_markers("+++ b/x.py\n+=======\n+++ b/y.py\n+a = '======='\n") == ["x.py"]
print("ok")
