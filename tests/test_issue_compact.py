"""간추린 이슈(DEV-137) — 작업에 필요한 것만 남기고 서버 알림·잡기 기록을 빼며, 검토·실행 프롬프트에 미리 들어가 get_issue를 다시 부르지 않게 한다."""
import json, os, sys, tempfile
from pathlib import Path

os.environ["DEV_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import db, issues, mcp_tools, review, execute  # noqa: E402

db.init()
me = {"kind": "human", "id": 0, "name": "admin", "model": None}
agent = {"kind": "agent", "id": 1, "name": "claude-main", "model": "m"}
issues.create_project(me, "CMP", "간추림", "", "/nowhere/cmp")
parent = issues.create_issue(me, "CMP", "부모", body="부모 본문")["ref"]
issues.post_plan(agent, parent, "## Tasks\n1. 할 일 | 파일: a.py | 확인: python tests/test_a.py | 선행: 없음")
task = issues.create_issue(me, "CMP", "할 일", parent=parent, body="**바꿀 파일**: a.py")["ref"]
issues.add_comment(me, task, "사람이 남긴 중요한 메모")
issues.add_comment(me, task, "🛠 Claude에게 실행을 맡겼어요 — 서버 알림")
issues.claim(agent, task)

full = issues.get_issue(task)
c = issues.compact_issue(full)
assert c["ref"] == task and c["body"] == "**바꿀 파일**: a.py" and c["parent_ref"] == parent
bodies = [n["body"] for n in c["notes"]]
assert "사람이 남긴 중요한 메모" in bodies and not any(b.startswith("🛠") for b in bodies), bodies
assert not any(k in c for k in ("events", "token_usage", "created_at", "claimed_by", "lease_until"))
p = issues.compact_issue(issues.get_issue(parent))
assert p["plan"]["body"].startswith("## Tasks") and p["children"] == [{"ref": task, "title": "할 일", "status": "backlog"}]
assert len(json.dumps(c, ensure_ascii=False)) < len(json.dumps(full, ensure_ascii=False))

# 프롬프트 앞머리에 Task와 부모가 함께 들어가고, 절차는 get_issue를 다시 부르지 말라고 한다
sec = review.issue_section(task)
items = json.loads(sec.split("```json\n", 1)[1].split("\n```", 1)[0])
assert [x["ref"] for x in items] == [task, parent]
prompt = execute.prompt_for(task, parent)
assert sec in prompt and "get_issue를 다시 부르지 않는다" in prompt and "mcp__dev__get_issue로 본문" not in prompt
assert review.issue_section("CMP-999") == ""   # 없는 이슈면 빈 글(일반 프롬프트로)

# MCP get_issue(compact=true)
mcp_tools._actor = lambda: agent
assert mcp_tools.get_issue(task, compact=True)["notes"] == issues.compact_issue(issues.get_issue(task))["notes"]
assert "events" in mcp_tools.get_issue(task)   # 기본은 예전 그대로 전체
print("OK issue compact")
