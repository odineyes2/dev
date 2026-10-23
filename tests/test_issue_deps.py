"""기존 Issue 선행 관계의 수정 권한·순환·기록을 실제 HTTP 앱과 MCP 함수에서 검사한다."""
import os
import sys
import tempfile
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
    for bad in ([a], [other], refs[3:14], "bad", None):
        assert request("PATCH", f"/api/issues/{a}", json={"blocked_by": bad}).status_code == 400
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
    assert request("PATCH", f"/api/issues/{a}", json={"blocked_by": [other]}).status_code == 400

    # MCP 도구도 같은 저장소 검사를 사용한다.
    original_actor = mcp_tools._actor
    mcp_tools._actor = lambda: human_actor
    try:
        result = mcp_tools.update_issue.fn(ref=a, blocked_by=[b]) if hasattr(mcp_tools.update_issue, "fn") else mcp_tools.update_issue(ref=a, blocked_by=[b])
        assert result
        assert [d["ref"] for d in good("GET", f"/api/issues/{a}")["blocked_by"]] == [b]
    finally:
        mcp_tools._actor = original_actor

print("issue dependency edit: ok")
