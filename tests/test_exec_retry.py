"""실행 끝 전체 검사 재시도 — 완료 등록 전에 Task worktree에서 전체 검사가 실패하면 실패 내용을 붙여 에이전트를 한 번 더 돌린다.
두 번째도 실패하면 완료 등록 없이 failed."""
import os, sys, tempfile
from unittest.mock import patch
from pathlib import Path

os.environ["DEV_DATA_DIR"] = tempfile.mkdtemp()
os.environ.pop("NTFY_TOPIC", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import db, issues, review, execute, orchestrate  # noqa: E402

human = {"kind": "human", "id": 1, "name": "admin", "model": None}
db.init()
issues.create_project(human, "NS", "nightshift")
issues.create_issue(human, "NS", "Task 부모")
task = issues.create_issue(human, "NS", "Task 하나", parent="NS-1")["ref"]
cmd = [sys.executable, "-c", "pass"]


def run(results):
    log_path, run_id = review.begin(human, issues.get_issue(task), "execute")
    calls, prompts = list(results), []
    real = review.subprocess.Popen
    def popen(c, **kw):
        prompts.append(c[2])
        return real(cmd, **kw)
    with patch.object(orchestrate, "run_tests", side_effect=lambda cwd: calls.pop(0)), \
         patch.object(review.subprocess, "Popen", side_effect=popen), patch.object(execute, "register_completion") as reg:
        status = review.run_headless(human, task, log_path, run_id, [cmd[0], "-p", "원래 지시"], None, None, 30, "실행")
    return status, prompts, reg.called


# 처음 실패 → 실패 내용을 붙여 재시도 → 통과하면 완료 등록
status, prompts, registered = run(["test_x.py: 종료 코드 1", None])
assert status == "ok" and registered and len(prompts) == 2, (status, prompts)
assert prompts[0] == "원래 지시" and prompts[1].startswith("원래 지시") and "test_x.py: 종료 코드 1" in prompts[1]

# 두 번 다 실패 → 완료 등록 없이 failed
status, prompts, registered = run(["a 실패", "b 실패"])
assert status == "failed" and not registered and len(prompts) == 2

# 통과하면 재시도 없음
status, prompts, registered = run([None])
assert status == "ok" and registered and len(prompts) == 1
print("ok")

# 바꿀 파일 밖 변경 경고 — tests/ 아래 .py만 허용, 목록이 없으면 검사하지 않는다(NS-32-1 캡처 PNG).
body = "**바꿀 파일**: server/app_parts/03-enhance.py, `server/model_registry.py`, static/\n\n**확인**: ..."
changed = ["server/app_parts/03-enhance.py", "server/model_registry.py", "static/app.js", "tests/test_new.py", "tests/shots/a.png", "README.md", "server/other.py"]
assert execute.out_of_scope({"body": body}, changed) == ["tests/shots/a.png", "README.md", "server/other.py"]
assert execute.out_of_scope({"body": "파일 목록 없음"}, changed) == []
print("ok scope")

# 에이전트가 스스로 on_hold로 돌리면(외부 조건·질문) 실패가 아니다 — 검사·완료 등록을 하지 않고 on_hold를 둔다(NS-31-1).
agent = {"kind": "agent", "id": 1, "name": "claude", "model": "m"}
log_path, run_id = review.begin(human, issues.get_issue(task), "execute")
seen = len(issues.get_issue(task)["events"])
real = review.subprocess.Popen
def ask_human(c, **kw):
    issues.set_status(agent, task, "on_hold", "공식 문서를 볼 수 없어요")
    return real(cmd, **kw)
with patch.object(orchestrate, "run_tests", side_effect=AssertionError("held면 검사하지 않는다")), \
     patch.object(review.subprocess, "Popen", side_effect=ask_human), patch.object(execute, "register_completion") as reg:
    status = review.run_headless(human, task, log_path, run_id, [cmd[0], "-p", "지시"], None, None, 30, "실행")
current = issues.get_issue(task)
assert status == "held" and not reg.called and current["status"] == "on_hold", (status, current["status"])
assert not any("끝나지 못했어요" in (e["body"] or "") for e in current["events"][seen:])
assert review.list_runs(task)[0]["status"] == "ok"
print("ok held")

# 바꿀 파일이 없는(조사 전용) Task는 실행을 맡기지 않는다 — 커밋이 없어 매번 실패한다(NS-31-1).
assert "바꿀 파일이 없어" in execute.scope_reason({"body": "**바꿀 파일**: 없음(읽기 전용 조사, 결과는 NS-31 댓글)", "project_key": "NS"}, [])
assert execute.scope_reason({"body": "**바꿀 파일**: server/a.py", "project_key": "NS"}, []) is None
print("ok no-file task")

# 웹 정책 — 검토만 웹(허용 도메인), 실행은 오프라인. 검토는 비밀 파일 읽기 금지.
rc = review.command_for("NS-1")
assert "WebSearch" in rc and "WebFetch(domain:docs.runpod.io)" in rc and "WebFetch" not in rc[rc.index("--disallowedTools"):]
assert rc[rc.index("--tools") + 1] == "Read,Grep,Glob,WebSearch,WebFetch" and "Read(**/.env)" in rc and "Read(**/.mcp.json)" in rc
assert 'web_search="cached"' in review.codex_command_for("NS-1")
ec = execute.command_for("NS-1-1", "NS-1", "mock")
assert not any(t.startswith(("WebFetch", "WebSearch")) for t in ec[ec.index("--allowedTools") + 1:ec.index("--disallowedTools")])
assert {"WebFetch", "WebSearch"} <= set(ec[ec.index("--disallowedTools") + 1:])
assert 'web_search="disabled"' in execute.codex_command_for("NS-1-1", "NS-1") and "sandbox_workspace_write.network_access=false" in execute.codex_command_for("NS-1-1", "NS-1")
print("ok web policy")
