"""Claude에게 검토 맡기기(DEV-13) — 가짜 claude(파이썬 스크립트)로.
- 사람만, 한 번에 하나(바쁘면 대기열), 요청은 타임라인 댓글, 도는 동안 review_running, 실패(종료 코드·시간 초과)는 댓글로.
- 실제 명령에 안전장치(--restricted, 쓰기·명령 도구 차단, dev MCP만)가 들어 있다."""
import os, sys, tempfile, time
from pathlib import Path

os.environ["DEV_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as A, auth, review  # noqa: E402

auth._client = httpx.AsyncClient(transport=httpx.MockTransport(
    lambda req: httpx.Response(200, json={"user": {"id": 1, "username": "admin", "role": "admin"}})))
H = {"X-Requested-With": "dev"}
fake = Path(tempfile.mkdtemp()) / "fake_claude.py"
fake.write_text("import json, sys, time\nprint(json.dumps({'result': 'args ' + str(sys.argv[1:]), 'total_cost_usd': 0.5, 'usage': {'input_tokens': 10, 'cache_read_input_tokens': 90, 'output_tokens': 7}}) if len(sys.argv) > 4 else 'args ' + str(sys.argv[1:]))\ntime.sleep(float(sys.argv[2]))\nsys.exit(int(sys.argv[3]))\n", "utf-8")
behave = {"sleep": "1", "code": "0"}

# 실제 명령의 안전장치 — 가짜로 바꾸기 전에 확인
real = review.command_for("NS-1")
assert "--restricted" in real and "--strict-mcp-config" in real and real[real.index("--mcp-config") + 1].endswith(".mcp.json")
blocked = real[real.index("--disallowedTools") + 1:real.index("--no-session-persistence")]
assert {"Bash", "Edit", "Write"} <= set(blocked)
allowed = real[real.index("--allowedTools") + 1:real.index("--disallowedTools")]
assert all(t in ("Read", "Grep", "Glob") or t.startswith("mcp__dev__") for t in allowed) and "mcp__dev__post_plan" in allowed
assert "검토만" in review.prompt_for("NS-1") and "NS-1" in review.prompt_for("NS-1")
review.command_for = lambda ref: [sys.executable, str(fake), ref, behave["sleep"], behave["code"], *(["json"] if behave.get("json") else [])]

with TestClient(A.app) as c:
    c.cookies.set("ns_session", "adm")
    c.post("/api/projects", json={"key": "NS", "name": "nightshift"}, headers=H)
    c.post("/api/issues", json={"project": "NS", "title": "검토할 이슈"}, headers=H)
    c.post("/api/issues", json={"project": "NS", "title": "다른 이슈"}, headers=H)
    key = c.post("/api/agents", json={"name": "a"}, headers=H).json()["key"]
    c.cookies.clear()
    assert c.post("/api/issues/NS-1/review", headers={"Authorization": f"Bearer {key}"}).status_code == 403   # 에이전트는 못 맡김
    c.cookies.set("ns_session", "adm")
    assert c.post("/api/issues/NS-9/review", headers=H).status_code == 404

    behave.update(sleep="2", code="0")
    r = c.post("/api/issues/NS-1/review", headers=H)
    assert r.status_code == 200 and r.json()["ref"] == "NS-1", r.text
    it = c.get("/api/issues/NS-1").json()
    assert it["review_running"] and it["review_busy"] and "검토를 맡겼어요" in it["events"][-1]["body"]
    other = c.get("/api/issues/NS-2").json()
    assert other["review_busy"] and not other["review_running"]
    q = c.post("/api/issues/NS-2/review", headers=H).json()   # 한 번에 하나 — 바쁘면 줄에 선다(DEV-43)
    assert q["queued"] and q["position"] == 1 and c.get("/api/issues/NS-2").json()["job"]["id"] == q["job_id"], q
    assert c.delete(f"/api/jobs/{q['job_id']}", headers=H).status_code == 200 and c.get("/api/jobs").json()["jobs"] == []
    for _ in range(60):
        if not review.running_ref():
            break
        time.sleep(0.2)
    it = c.get("/api/issues/NS-1").json()
    assert not it["review_running"] and "끝나지 못했어요" not in it["events"][-1]["body"]
    log = sorted((Path(os.environ["DEV_DATA_DIR"]) / "reviews").glob("NS-1-*.log"))[-1].read_text("utf-8")
    assert "args" in log and "{" not in log    # 토큰을 못 읽는 출력도 ok, 로그는 원문
    r1 = review.list_runs("NS-1")[0]
    assert r1["status"] == "ok" and r1["mode"] == "review" and r1["input_tokens"] is None and r1["ended_at"] and r1["log_file"].startswith("NS-1-")

    # JSON 출력 — 토큰·비용을 기록하고, 로그에는 result 글만
    behave.update(sleep="0", code="0", json=True)
    c.post("/api/issues/NS-1/review", headers=H)
    for _ in range(60):
        if not review.running_ref():
            break
        time.sleep(0.2)
    time.sleep(0.3)
    r2 = review.list_runs("NS-1")[0]
    assert r2["status"] == "ok" and (r2["input_tokens"], r2["output_tokens"], r2["cost_usd"]) == (100, 7, 0.5), r2
    assert "{" not in (Path(os.environ["DEV_DATA_DIR"]) / "reviews" / r2["log_file"]).read_text("utf-8").splitlines()[0]
    behave["json"] = False

    # 실패 — 종료 코드를 댓글로
    behave.update(sleep="0", code="3")
    c.post("/api/issues/NS-2/review", headers=H)
    for _ in range(60):
        if not review.running_ref():
            break
        time.sleep(0.2)
    time.sleep(0.3)
    last = c.get("/api/issues/NS-2").json()["events"][-1]["body"]
    assert "끝나지 못했어요" in last and "종료 코드 3" in last, last
    r3 = review.list_runs("NS-2")[0]
    assert r3["status"] == "failed" and r3["exit_code"] == 3

    # 시간 초과 — 멈추고 댓글
    behave.update(sleep="5", code="0")
    review.TIMEOUT_SEC = 1
    c.post("/api/issues/NS-2/review", headers=H)
    for _ in range(60):
        if not review.running_ref():
            break
        time.sleep(0.2)
    time.sleep(0.3)
    assert "끝나지 않아 멈췄어요" in c.get("/api/issues/NS-2").json()["events"][-1]["body"]
    assert review.list_runs("NS-2")[0]["status"] == "timeout" and len(review.list_runs("NS-2")) == 2
print("OK")
