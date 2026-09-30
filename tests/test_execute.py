"""실행 모드(DEV-23) — 임시 git 저장소로: worktree·브랜치, 시작 조건, 권한·상한 인자, push가 실제로 막히는지."""
import os, subprocess, sys, tempfile
from pathlib import Path

os.environ["DEV_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import execute, issues  # noqa: E402

tmp = Path(tempfile.mkdtemp())
repo, bare = tmp / "repo", tmp / "origin.git"


def git(cwd, *a, check=True):
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    assert not check or r.returncode == 0, r.stderr
    return r


repo.mkdir(); bare.mkdir()
git(bare, "init", "-q", "--bare")
git(repo, "init", "-q", "-b", "main")
(repo / "a.txt").write_text("a\n"); git(repo, "add", "."); git(repo, "commit", "-qm", "init")
git(repo, "remote", "add", "origin", str(bare))
git(repo, "push", "-q", "origin", "main")


def rejects(fn, *a):
    try:
        fn(*a)
    except issues.StoreError as e:
        assert e.status == 409, e.status
        return str(e)
    raise AssertionError("거절되어야 해요")


# worktree·브랜치
assert "local_path" in rejects(execute.prepare_worktree, "", "T-1")
wt = execute.prepare_worktree(str(repo), "T-1")
assert wt.is_dir() and (wt / "a.txt").exists()
assert git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "relay/T-1"
assert execute.prepare_worktree(str(repo), "T-1") == wt   # 있으면 이어서
assert execute.branch_info(str(repo), "T-1") == {"branch": "relay/T-1", "base": "main", "commits": 0, "diff_stat": ""}
assert execute.branch_info(str(repo), "T-9") is None

# 시작 조건 — 추적 파일이 더럽거나 base가 아니면 거절
(repo / "a.txt").write_text("dirty\n")
assert "커밋하지 않은" in rejects(execute.prepare_worktree, str(repo), "T-2")
git(repo, "checkout", "-q", "--", "a.txt")
git(repo, "checkout", "-q", "-b", "other")
assert "other" in rejects(execute.prepare_worktree, str(repo), "T-2")
git(repo, "checkout", "-q", "main")
assert not execute.worktree_path("T-2").exists()

# 결과 확인 — worktree에서 커밋하면 브랜치 정보에 잡힌다(main은 그대로)
(wt / "b.txt").write_text("b\n"); git(wt, "add", "."); git(wt, "commit", "-qm", "b (T-1)")
info = execute.branch_info(str(repo), "T-1")
assert info["commits"] == 1 and "b.txt" in info["diff_stat"], info
assert not (repo / "b.txt").exists()

# push 차단 — 안전한 환경에서는 원격으로 못 나간다(보통 환경에서는 나간다)
env = execute.safe_env(str(repo))
assert "GH_TOKEN" not in env and env["GIT_TERMINAL_PROMPT"] == "0"
blocked = subprocess.run(["git", "push", "origin", "HEAD"], cwd=wt, env=env, capture_output=True, text=True)
assert blocked.returncode != 0 and "relay/T-1" not in git(bare, "branch", "--list").stdout
assert git(repo, "config", "remote.origin.pushurl", check=False).stdout.strip() == ""   # 저장소 설정은 안 건드림
subprocess.run(["git", "push", "-q", "origin", "HEAD"], cwd=wt, capture_output=True, text=True, check=True)
assert "relay/T-1" in git(bare, "branch", "--list").stdout   # 대조군: 환경만 다르면 나간다

# 명령 — 권한·상한
cmd = execute.command_for("T-1", "T-0", "/x/.mcp.json")
assert "--dangerously-skip-permissions" not in cmd and cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
assert cmd[cmd.index("--max-budget-usd") + 1] == "2.0" and cmd[cmd.index("--output-format") + 1] == "json" and "--restricted" not in cmd
allowed = cmd[cmd.index("--allowedTools") + 1:cmd.index("--disallowedTools")]
blocked_tools = cmd[cmd.index("--disallowedTools") + 1:cmd.index("--max-budget-usd")]
assert "Bash" not in allowed and "Bash(git push:*)" not in allowed and "Bash(python tests/*)" in allowed
assert {"Bash(git push:*)", "Bash(git checkout:*)", "Bash(rm:*)", "WebFetch"} <= set(blocked_tools)
assert not set(allowed) & set(blocked_tools) and "mcp__dev__post_plan" not in allowed
assert "T-1" in cmd[2] and "T-0" in cmd[2] and "relay/T-1" in cmd[2]
print("OK")
