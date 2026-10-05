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
# 상위 실행기의 원격 차단은 유지하고, 이 테스트가 만든 로컬 bare에만 전송한다.
git(repo, "push", "-q", str(bare), "main")


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
# 끝 커밋이 그대로면 git은 끝 해시 읽기 한 번뿐(DEV-42-3) — 위에서 커밋이 늘어난 것은 캐시가 새 해시에 비워졌다는 뜻
calls = []
orig_git = execute._git
execute._git = lambda cwd, *a: calls.append(a[0]) or orig_git(cwd, *a)
assert execute.branch_info(str(repo), "T-1") == info and calls == ["for-each-ref"], calls
execute._git = orig_git

# push 차단 — 안전한 환경에서는 원격으로 못 나간다(보통 환경에서는 나간다)
env = execute.safe_env(str(repo))
assert "GH_TOKEN" not in env and env["GIT_TERMINAL_PROMPT"] == "0"
blocked = subprocess.run(["git", "push", "origin", "HEAD"], cwd=wt, env=env, capture_output=True, text=True)
assert blocked.returncode != 0 and "relay/T-1" not in git(bare, "branch", "--list").stdout
assert git(repo, "config", "--local", "remote.origin.pushurl", check=False).stdout.strip() == ""   # 저장소 설정은 안 건드림
subprocess.run(["git", "push", "-q", str(bare), "HEAD"], cwd=wt, capture_output=True, text=True, check=True)
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

# ---- 시작 API — 조건 검사·runs 기록·worktree에서 실행 ----
import time  # noqa: E402
import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as A, auth, db, jobs, review, orchestrate  # noqa: E402

auth._client = httpx.AsyncClient(transport=httpx.MockTransport(
    lambda req: httpx.Response(200, json={"user": {"id": 1, "username": "admin", "role": "admin"}})))
H = {"X-Requested-With": "dev"}
fake = tmp / "fake_claude.py"
fake.write_text("import json, os, sys, time\ntime.sleep(float(sys.argv[1]))\n"
                "print(json.dumps({'result': 'cwd=' + os.getcwd() + ' cfg=' + os.environ.get('GIT_CONFIG_COUNT', '-'), 'usage': {'output_tokens': 5}, 'total_cost_usd': 0.1}))\n"
                "sys.exit(int(sys.argv[2]))\n", "utf-8")
behave = {"sleep": "2", "code": "0"}
execute.command_for = lambda ref, parent, mcp: [sys.executable, str(fake), behave["sleep"], behave["code"]]


def wait_idle():
    """도는 것도 시작할 대기 항목도 없을 때까지(두 번 연속 확인 — 끝남과 다음 시작 사이 틈)."""
    idle = 0
    for _ in range(120):
        time.sleep(0.2)
        idle = idle + 1 if not jobs.busy() and not any(not j['note'] for j in jobs.list_jobs()) else 0
        if idle >= 2:
            break


db.init()
orchestrate.SETTINGS.write_text('{"EX":{"auto_merge":false}}')
# Fake agent commits and links its result like the authorized Claude path.
original_register = execute.register_completion
def linked_completion(actor, ref, cwd, run_id, note):
    git(cwd, 'commit', '--allow-empty', '-qm', 'fake implementation ' + str(run_id))
    issues.link_commit(actor, ref, git(cwd, 'rev-parse', 'HEAD').stdout.strip())
    return original_register(actor, ref, cwd, run_id, note)
execute.register_completion = linked_completion
me = {"kind": "human", "name": "admin"}
issues.create_project(me, "EX", "ex", "", str(repo))
issues.create_issue(me, "EX", "부모", status="triage")
issues.post_plan(me, "EX-1", "## Tasks\n1. 먼저\n2. 나중 | 선행: 1")
issues.decide(me, "EX-1", "approve", "", plan_version=1)   # EX-1-1(먼저), EX-1-2(나중, 선행 EX-1-1)
issues.create_issue(me, "EX", "미승인 부모", status="triage"); issues.post_plan(me, "EX-2", "계획")
issues.create_issue(me, "EX", "미승인 Task", parent="EX-2")

with TestClient(A.app) as c:
    c.cookies.set("ns_session", "adm")
    key = c.post("/api/agents", json={"name": "a"}, headers=H).json()["key"]
    post = lambda ref: c.post(f"/api/issues/{ref}/execute", headers=H)
    assert c.post("/api/issues/EX-1-1/execute", headers={"Authorization": f"Bearer {key}"}).status_code == 403
    assert post("EX-9").status_code == 404
    assert "Task" in post("EX-1").json()["detail"]                    # 부모(Task 아님)
    assert "승인되지 않았어요" in post("EX-2-1").json()["detail"]          # 계획서 미승인
    r = post("EX-1-2"); assert r.status_code == 200 and r.json()["queued"] and "EX-1-1" in r.json()["note"], r.text   # 선행 미완료 → 줄에서 대기
    assert not execute.worktree_path("EX-1-2").exists() and review.list_runs("EX-1-2") == []
    assert issues.get_issue('EX-1-2')['status'] == 'waiting'

    r = post("EX-1-1"); assert r.status_code == 200 and r.json()["started"], r.text   # 앞의 대기 항목이 막혀도 뒤 항목은 돈다
    assert execute.worktree_path("EX-1-1").is_dir() and review.running_ref() == "EX-1-1"
    assert issues.get_issue('EX-1-1')['status'] == 'in_progress'
    assert post("EX-1-2").json()["job_id"] == jobs.list_jobs()[0]["id"] and len(jobs.list_jobs()) == 1   # 중복으로 안 넣음
    for terminal in ('done', 'closed'):
        assert "실행 중" in rejects(issues.set_status, me, "EX-1-1", terminal)
    assert issues.get_issue('EX-1-1')['status'] == 'in_progress'
    wait_idle()
    assert review.list_runs("EX-1-2") == []
    issues.set_status(me, "EX-1-1", "done")
    jobs.pump()
    wait_idle()                                                         # 선행 done → EX-1-1이 끝나면 EX-1-2가 이어서 돈다
    assert review.list_runs("EX-1-2")[0]["status"] == "ok" and jobs.list_jobs() == []
    run = review.list_runs("EX-1-1")[0]
    assert run["mode"] == "execute" and run["status"] == "ok" and run["output_tokens"] == 5 and run["cost_usd"] == 0.1, run
    log = (Path(os.environ["DEV_DATA_DIR"]) / "reviews" / run["log_file"]).read_text("utf-8")
    cwd = log.split("cwd=")[1].split(" cfg=")[0]
    assert os.path.samefile(cwd, execute.worktree_path("EX-1-1")), cwd    # worktree에서 실행
    assert "cfg=2" in log, log                                          # remote 1개 pushurl + credential.helper — 안전한 환경으로 실행됨
    assert any("실행을 맡겼어요" in e["body"] for e in issues.get_issue("EX-1-1")["events"])

    behave.update(sleep="0", code="3")
    issues.set_status(me, "EX-1-2", "changes_requested", "고쳐 주세요")
    post("EX-1-2"); wait_idle()
    last = review.list_runs("EX-1-2")[0]
    assert last["status"] == "failed" and last["exit_code"] == 3
    assert issues.get_issue('EX-1-2')['status'] == 'changes_requested'
    assert "실행 작업이 끝나지 못했어요" in issues.get_issue("EX-1-2")["events"][-1]["body"]
    assert len(review.list_runs("EX-1-2")) == 2 and execute.worktree_path("EX-1-2").is_dir()   # 같은 worktree에서 이어서
    assert post("EX-1-1").status_code == 409                            # done인 Task는 다시 못 맡김
# Claude 경로도 구현 종료 시에는 진행 중이며 병합 검사 후에만 검토 상태가 된다.
from unittest.mock import patch
orchestrate.SETTINGS.write_text('{"EX":{"auto_merge":true}}')
auto_task = issues.create_issue(me, 'EX', 'automatic completion', parent='EX-1')
behave.update(sleep='0.1', code='0')
def inspect_merge(path):
    if 'merge-worktrees' not in str(path):   # 완료 등록 전 Task worktree 전체 검사
        return None
    assert issues.get_issue(auto_task['ref'])['status'] == 'in_progress'
    assert orchestrate.completion(auto_task['ref'])['phase'] == 'checking'
def with_scratch(actor, ref, cwd, run_id, note):   # 에이전트가 지우지 못한 미추적 임시 파일(NS-32-2)은 완료를 막지 않는다
    (Path(cwd) / '_scratch.py').write_text('x')
    return linked_completion(actor, ref, cwd, run_id, note)
execute.register_completion = with_scratch
with patch.object(orchestrate, 'run_tests', side_effect=inspect_merge) as checked:
    execute.start(me, auto_task['ref'])
    wait_idle()
    assert checked.call_count == 1   # Task worktree 검사만 — base가 그대로라 같은 트리의 시험 병합 검사는 생략
assert issues.get_issue(auto_task['ref'])['status'] == 'in_review'
assert orchestrate.completion(auto_task['ref'])['phase'] == 'complete'
assert '_scratch.py' in orchestrate.completion(auto_task['ref'])['note']
orchestrate.SETTINGS.write_text('{"EX":{"auto_merge":false}}')
execute.register_completion = original_register
print("OK")
