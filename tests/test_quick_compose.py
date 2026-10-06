"""빠른 발행(DEV-93) — 이슈를 만들 때 assignee_agent_id를 받아 저장하고, 없는 에이전트면 아무것도 만들지 않는다."""
import os, sys, tempfile
from pathlib import Path

os.environ["DEV_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as A, attachments, auth, issues  # noqa: E402

auth._client = httpx.AsyncClient(transport=httpx.MockTransport(
    lambda req: httpx.Response(200, json={"user": {"id": 1, "username": "admin", "role": "admin"}})))
H = {"X-Requested-With": "dev"}

with TestClient(A.app) as c:
    c.cookies.set("ns_session", "adm")
    post = lambda url, **kw: c.post(url, headers=H, **kw)
    assert post("/api/projects", json={"key": "DEV", "name": "dev"}).status_code == 200
    aid = post("/api/agents", json={"name": "claude"}).json()["agent"]["id"]

    # 주면 저장, 안 주면 null
    r = post("/api/issues", json={"project": "DEV", "title": "", "body": "빠른 발행", "assignee_agent_id": aid})
    assert r.status_code == 200, r.text
    assert issues.get_issue(r.json()["ref"])["assignee_agent_id"] == aid
    r = post("/api/issues", json={"project": "DEV", "body": "지정 없음"})
    assert r.status_code == 200 and r.json()["assignee_agent_id"] is None, r.text

    # 없는 id는 404, 숫자 아니면 400 — 둘 다 이슈를 만들지 않고 번호도 쓰지 않는다
    before = len(issues.list_issues("DEV"))
    assert post("/api/issues", json={"project": "DEV", "body": "x", "assignee_agent_id": 9999}).status_code == 404
    assert post("/api/issues", json={"project": "DEV", "body": "x", "assignee_agent_id": "abc"}).status_code == 400
    assert len(issues.list_issues("DEV")) == before

    # 첨부와 함께 원자적으로 — 에이전트가 없으면 첨부도 묶이지 않는다
    att = post("/api/attachments", files={"file": ("a.md", b"hello")}).json()
    body = {"project": "DEV", "body": "첨부", "attachment_ids": [att["id"]], "assignee_agent_id": 9999}
    assert post("/api/issues", json=body).status_code == 404
    assert attachments.get({"kind": "human", "name": "admin"}, att["id"])["issue_id"] is None
    body["assignee_agent_id"] = aid
    r = post("/api/issues", json=body)
    assert r.status_code == 200, r.text
    full = issues.get_issue(r.json()["ref"])
    assert full["assignee_agent_id"] == aid and len(full["attachments"]) == 1

print("ok")
