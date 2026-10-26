"""기존 Issue 선행 관계의 수정 권한·순환·기록을 실제 HTTP 앱과 MCP 함수에서 검사한다."""
import os
import sys
import tempfile
import subprocess
from pathlib import Path

os.environ["DEV_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as A  # noqa: E402
import auth  # noqa: E402
import db  # noqa: E402
import issues  # noqa: E402
import mcp_tools  # noqa: E402
import execute  # noqa: E402

auth._client = httpx.AsyncClient(transport=httpx.MockTransport(
    lambda req: httpx.Response(200, json={"user": {"id": 1, "username": "admin", "role": "admin"}})))
human_actor = {"kind": "human", "name": "admin"}

with TestClient(A.app) as client:
    client.cookies.set("ns_session", "adm")
    def request(method, path, **kwargs):
        return client.request(method, path, headers={"X-Requested-With": "dev"}, **kwargs)

    def good(method, path, **kwargs):
        response = request(method, path, **kwargs)
        assert response.status_code == 200, response.text
        return response.json()

    good("POST", "/api/projects", json={"key": "NS", "name": "NS"})
    good("POST", "/api/projects", json={"key": "DEV", "name": "Dev"})
    refs = [good("POST", "/api/issues", json={"project": "NS", "title": str(i)})["ref"] for i in range(15)]
    a, b, c = refs[:3]
    other = good("POST", "/api/issues", json={"project": "DEV", "title": "other"})["ref"]
    task = good("POST", "/api/issues", json={"project": "NS", "parent": a, "title": "task"})["ref"]
    agent_key = good("POST", "/api/agents", json={"name": "agent", "model": "gpt-x"})["key"]
    def agent_patch(ref, refs):
        return client.patch(f"/api/issues/{ref}", headers={"Authorization": f"Bearer {agent_key}"}, json={"blocked_by": refs})

    good("PATCH", f"/api/issues/{a}", json={"blocked_by": [b]})
    detail = good("GET", f"/api/issues/{a}")
    assert [d["ref"] for d in detail["blocked_by"]] == [b]
    assert any(e["kind"] == "edit" and "추가" in e["body"] for e in detail["events"])
    assert agent_patch(a, [c]).status_code == 403
    owned = issues.create_issue({"kind": "agent", "id": 1}, "NS", "owned")
    assert agent_patch(task, [b]).status_code == 403
    assert agent_patch(owned["ref"], [b]).status_code == 200
    for bad in ([a], refs[3:14], "bad", None):
        assert request("PATCH", f"/api/issues/{a}", json={"blocked_by": bad}).status_code == 400
    cross = good("POST", "/api/issues", json={"project": "NS", "title": "cross", "blocked_by": [other]})["ref"]
    assert good("GET", f"/api/issues/{cross}")["blocked_by"] == [{"ref": other, "status": "backlog"}]
    assert request("PATCH", f"/api/issues/{other}", json={"blocked_by": [cross]}).status_code == 400
    assert other not in [d["ref"] for d in good("GET", "/api/projects/NS/dep-candidates")["issues"]]
    assert other in [d["ref"] for d in good("GET", "/api/projects/NS/dep-candidates?all_projects=true")["issues"]]
    assert request("GET", "/api/projects/UNKNOWN/dep-candidates?all_projects=true").status_code == 404
    with db.connect() as conn:
        conn.execute("UPDATE issues SET status='in_review' WHERE id=?", (issues.get_issue(refs[13])["id"],))
        conn.execute("UPDATE issues SET status='done' WHERE id=?", (issues.get_issue(refs[14])["id"],))
    assert request("PATCH", f"/api/issues/{a}", json={"blocked_by": [refs[14]]}).status_code == 400
    assert refs[13] in [d["ref"] for d in good("GET", "/api/projects/NS/dep-candidates?include_review=true")["issues"]]
    assert refs[13] not in [d["ref"] for d in good("GET", "/api/projects/NS/dep-candidates")["issues"]]
    good("PATCH", f"/api/issues/{a}", json={"blocked_by": [b, refs[13]]})
    good("PATCH", f"/api/issues/{a}", json={"blocked_by": [b]})
    good("PATCH", f"/api/issues/{task}", json={"blocked_by": [b]})
    assert request("PATCH", f"/api/issues/{b}", json={"blocked_by": [a]}).status_code == 400
    good("PATCH", f"/api/issues/{b}", json={"blocked_by": [c]})
    assert request("PATCH", f"/api/issues/{c}", json={"blocked_by": [a]}).status_code == 400
    assert [d["ref"] for d in good("GET", f"/api/issues/{a}")["blocked_by"]] == [b]
    good("PATCH", f"/api/issues/{a}", json={"blocked_by": []})
    assert any("제거" in e["body"] for e in good("GET", f"/api/issues/{a}")["events"])
    assert not good("GET", f"/api/issues/{a}")["blocked_by"]
    assert issues.set_blocked_by(human_actor, a, [b])["blocked_by"][0]["ref"] == b
    good("PATCH", f"/api/issues/{a}", json={"blocked_by": [other]})
    assert [d["ref"] for d in good("GET", f"/api/issues/{a}")["blocked_by"]] == [other]
    assert request("PATCH", f"/api/issues/{other}", json={"blocked_by": [a]}).status_code == 400
    assert agent_patch(owned["ref"], [other]).status_code == 200
    assert other in [d["ref"] for d in good("GET", f"/api/issues/{owned['ref']}")["blocked_by"]]
    good("POST", f"/api/issues/{other}/status", json={"status": "closed"})
    assert good("GET", f"/api/issues/{a}")["blocked_by"] == [{"ref": other, "status": "closed"}]
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM issue_deps d JOIN issues b ON b.id=d.blocked_by_id WHERE d.issue_id=? AND b.status<>'done'", (issues.get_issue(a)["id"],)).fetchone()[0] == 1
    assert other not in [d["ref"] for d in good("GET", "/api/projects/NS/dep-candidates?all_projects=true")["issues"]]
    assert request("PATCH", f"/api/issues/{b}", json={"blocked_by": [other]}).status_code == 400
    assert request("DELETE", f"/api/issues/{other}").status_code == 204
    assert not good("GET", f"/api/issues/{a}")["blocked_by"]
    assert not good("GET", f"/api/issues/{cross}")["blocked_by"]

    # MCP 도구도 같은 저장소 검사를 사용한다.
    original_actor = mcp_tools._actor
    mcp_tools._actor = lambda: human_actor
    try:
        mcp_create = mcp_tools.create_issue.fn if hasattr(mcp_tools.create_issue, "fn") else mcp_tools.create_issue
        mcp_update = mcp_tools.update_issue.fn if hasattr(mcp_tools.update_issue, "fn") else mcp_tools.update_issue
        new_other = good("POST", "/api/issues", json={"project": "DEV", "title": "MCP predecessor"})["ref"]
        created = mcp_create(project="NS", title="MCP dependent", blocked_by=[new_other])
        assert created["blocked_by"] == [{"ref": new_other, "status": "backlog"}]
        result = mcp_update(ref=a, blocked_by=[new_other])
        assert result
        assert [d["ref"] for d in good("GET", f"/api/issues/{a}")["blocked_by"]] == [new_other]
    finally:
        mcp_tools._actor = original_actor

print("issue dependency edit: ok")

# 부모의 선행 Issue는 Task 완료와 실제 base 병합까지 기다린다.
repo = Path(tempfile.mkdtemp())
def git(*args):
    result = subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
                            cwd=repo, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr

git("init", "-q", "-b", "main")
(repo / "file.txt").write_text("base", encoding="utf-8")
git("add", "file.txt")
git("commit", "-qm", "base")
issues.create_project(human_actor, "PD", "parent dependency", "", str(repo))
before = issues.create_issue(human_actor, "PD", "before")
prior_task = issues.create_issue(human_actor, "PD", "prior task", parent=before["ref"])
after = issues.create_issue(human_actor, "PD", "after", blocked_by=[before["ref"]])
issues.post_plan(human_actor, after["ref"], "approved plan")
issues.decide(human_actor, after["ref"], "approve", plan_version=1)
task = issues.create_issue(human_actor, "PD", "later task", parent=after["ref"])
other_repo = Path(tempfile.mkdtemp())
subprocess.run(["git", "init", "-q", "-b", "main", str(other_repo)], check=True)
(other_repo / "file.txt").write_text("other base", encoding="utf-8")
subprocess.run(["git", "-C", str(other_repo), "add", "file.txt"], check=True)
subprocess.run(["git", "-C", str(other_repo), "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "base"], check=True)
issues.create_project(human_actor, "PX", "cross-project dependency", "", str(other_repo))
cross_after = issues.create_issue(human_actor, "PX", "after PD", blocked_by=[before["ref"]])
issues.post_plan(human_actor, cross_after["ref"], "approved plan")
issues.decide(human_actor, cross_after["ref"], "approve", plan_version=1)
cross_task = issues.create_issue(human_actor, "PX", "later task", parent=cross_after["ref"])
def reason(wait=True):
    return execute.blocked_reason(issues.get_issue(task["ref"]), issues.get_issue(after["ref"]), wait=wait)
def cross_reason():
    return execute.blocked_reason(issues.get_issue(cross_task["ref"]), issues.get_issue(cross_after["ref"]))

assert before["ref"] in reason() and reason(wait=False) is None
assert before["ref"] in cross_reason()
with db.connect() as conn:
    assert conn.execute("SELECT COUNT(*) FROM issues i WHERE i.id=? AND NOT EXISTS(SELECT 1 FROM issue_deps d JOIN issues b ON b.id=d.blocked_by_id WHERE d.issue_id=i.id AND b.status<>'done')", (cross_after["id"],)).fetchone()[0] == 0
git("checkout", "-qb", execute.branch_name(prior_task["ref"]))
(repo / "file.txt").write_text("changed", encoding="utf-8")
git("commit", "-qam", "prior task")
git("checkout", "-q", "main")
issues.set_status(human_actor, prior_task["ref"], "done")
issues.set_status(human_actor, before["ref"], "in_review")
assert "병합을 기다려요" in reason()
assert "병합을 기다려요" in cross_reason()
git("merge", "--ff-only", execute.branch_name(prior_task["ref"]))
assert reason() is None
assert cross_reason() is None
issues.set_status(human_actor, prior_task["ref"], "backlog")
assert before["ref"] in reason()
assert before["ref"] in cross_reason()
issues.set_status(human_actor, before["ref"], "done")
assert reason() is None
assert cross_reason() is None
with db.connect() as conn:
    assert conn.execute("SELECT COUNT(*) FROM issues i WHERE i.id=? AND NOT EXISTS(SELECT 1 FROM issue_deps d JOIN issues b ON b.id=d.blocked_by_id WHERE d.issue_id=i.id AND b.status<>'done')", (cross_after["id"],)).fetchone()[0] == 1
issues.set_status(human_actor, before["ref"], "closed")
assert reason() is None
assert cross_reason() is None
print("parent dependency eligibility: ok")
