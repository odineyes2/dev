"""Claude 맡기기 대기열(DEV-43) — 가짜 claude로: 순차 실행·중복 방지·취소·재시작 후 이어감. 선행 대기 건너뛰기는 test_execute."""
import os, sys, tempfile, time
from pathlib import Path

os.environ["DEV_DATA_DIR"] = tempfile.mkdtemp()
os.environ["DEV_AUTO_REVIEW"] = "1"   # 옛 설정이 남아 있어도 자동 검토하지 않는다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import db, issues, jobs, review  # noqa: E402

fake = Path(tempfile.mkdtemp()) / "fake_claude.py"
fake.write_text("import time\ntime.sleep(0.5)\nprint('ok')\n", "utf-8")
review.command_for = lambda ref: [sys.executable, str(fake)]


def wait_idle():
    idle = 0
    for _ in range(100):
        time.sleep(0.2)
        idle = idle + 1 if not jobs.busy() and not jobs.list_jobs() else 0
        if idle >= 2:
            return
    raise AssertionError("대기열이 비지 않아요")


db.init()
me = {"kind": "human", "name": "admin"}
issues.create_project(me, "JQ", "q", "", "/nowhere/q")
for t in ("하나", "둘", "셋", "넷"):
    issues.create_issue(me, "JQ", t)

try:
    jobs.enqueue({"kind": "agent", "id": 1}, "JQ-1", "review")
    raise AssertionError("에이전트는 못 넣어요")
except issues.StoreError as e:
    assert e.status == 403

assert jobs.enqueue(me, "JQ-1", "review")["started"]          # 아무것도 안 돌면 바로 시작
r2 = jobs.enqueue(me, "JQ-2", "review"); r3 = jobs.enqueue(me, "JQ-3", "review"); r4 = jobs.enqueue(me, "JQ-4", "review")
assert (r2["position"], r3["position"], r4["position"]) == (1, 2, 3), (r2, r3, r4)
assert jobs.enqueue(me, "JQ-2", "review")["job_id"] == r2["job_id"] and len(jobs.list_jobs()) == 3   # 중복 클릭은 하나만
assert jobs.enqueue(me, "JQ-1", "review")["started"] and len(jobs.list_jobs()) == 3                # 도는 중인 것도 다시 안 넣음
jobs.cancel(me, r3["job_id"])                                                                      # 취소한 것은 안 돎
try:
    jobs.cancel(me, r3["job_id"]); raise AssertionError("두 번은 못 취소")
except issues.StoreError as e:
    assert e.status == 404
wait_idle()

runs = {ref: review.list_runs(ref) for ref in ("JQ-1", "JQ-2", "JQ-3", "JQ-4")}
assert [len(runs[r]) for r in runs] == [1, 1, 0, 1], runs
assert all(x[0]["status"] == "ok" for x in runs.values() if x)
assert runs["JQ-1"][0]["ended_at"] <= runs["JQ-2"][0]["started_at"] <= runs["JQ-2"][0]["ended_at"] <= runs["JQ-4"][0]["started_at"]   # 하나씩 차례로
with db.connect() as c:
    assert c.execute("SELECT run_id FROM jobs WHERE id=?", (r2["job_id"],)).fetchone()[0] == runs["JQ-2"][0]["id"]

# 재시작 — 줄은 DB에 남고, 서버가 다시 뜨면(db.init 뒤 펌프) 이어서 돈다
with db.connect() as c:
    c.execute("INSERT INTO jobs(issue_id, mode, actor, status, created_at) VALUES(?, 'review', 'human:admin', 'queued', ?)",
              (issues.get_issue("JQ-3")["id"], db.now_iso()))
db.init()
assert len(jobs.list_jobs()) == 1
jobs.pump(); wait_idle()
assert review.list_runs("JQ-3")[0]["status"] == "ok"

# 지워진 이슈의 대기 항목은 같이 사라진다
jobs.enqueue(me, "JQ-4", "review"); jobs.enqueue(me, "JQ-2", "review")
issues.delete_issue(me, "JQ-2")
wait_idle()

# 이슈 발행은 저장만 한다. 검토는 사람이 도구를 골라 직접 요청한다.
import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as A, auth  # noqa: E402
auth._client = httpx.AsyncClient(transport=httpx.MockTransport(
    lambda req: httpx.Response(200, json={"user": {"id": 1, "username": "admin", "role": "admin"}})))
H = {"X-Requested-With": "dev"}
fake.write_text("import time\ntime.sleep(2)\nprint('ok')\n", "utf-8")   # 첫 검토가 도는 동안 뒤 것이 줄에 남게
with TestClient(A.app) as c:
    c.cookies.set("ns_session", "adm")
    top = c.post("/api/issues", json={"project": "JQ", "title": "발행"}, headers=H).json()
    assert "job" not in top and top["ref"] == "JQ-5", top
    top2 = c.post("/api/issues", json={"project": "JQ", "title": "발행 둘"}, headers=H).json()
    assert "job" not in top2 and top2["ref"] == "JQ-6", top2
    task = c.post("/api/issues", json={"project": "JQ", "title": "Task", "parent": "JQ-5"}, headers=H).json()
    assert "job" not in task and task["parent_ref"] == "JQ-5"
    key = c.post("/api/agents", json={"name": "a"}, headers=H).json()["key"]
    c.cookies.clear()
    r = c.post("/api/issues", json={"project": "JQ", "title": "에이전트 발행"}, headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200 and "job" not in r.json(), r.text
    assert jobs.list_jobs() == []
    wait_idle()
    assert [len(review.list_runs(ref)) for ref in ("JQ-5", "JQ-6", task["ref"], r.json()["ref"])] == [0, 0, 0, 0]
    c.cookies.set("ns_session", "adm")
    assert c.post("/api/issues/JQ-5/review", headers=H).json()["started"]
    wait_idle()
    assert review.list_runs("JQ-5")[0]["provider"] == "claude"
print("OK")
