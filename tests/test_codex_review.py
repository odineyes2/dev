"""Codex에게 검토 맡기기 — 가짜 codex로 API, 명령 안전장치, 공용 실행 잠금을 확인한다."""
import os, sys, tempfile, time
from pathlib import Path

os.environ["DEV_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as A, auth, codex_review, review  # noqa: E402

auth._client = httpx.AsyncClient(transport=httpx.MockTransport(
    lambda req: httpx.Response(200, json={"user": {"id": 1, "username": "admin", "role": "admin"}})))
H = {"X-Requested-With": "dev"}

real = codex_review.command_for("NS-1")
assert real[1:6] == ["--no-daemon", "exec", "--sandbox", "read-only", "--skip-git-repo-check"], real
assert "검토만" in real[-1] and "NS-1" in real[-1] and "post_plan" in real[-1]

fake = Path(tempfile.mkdtemp()) / "fake_codex.py"
fake.write_text("import sys,time\nprint('codex result')\ntime.sleep(float(sys.argv[1]))\n", "utf-8")
codex_review.command_for = lambda ref: [sys.executable, str(fake), "1"]

with TestClient(A.app) as c:
    c.cookies.set("ns_session", "adm")
    c.post("/api/projects", json={"key": "NS", "name": "nightshift"}, headers=H)
    c.post("/api/issues", json={"project": "NS", "title": "Codex 검토"}, headers=H)
    key = c.post("/api/agents", json={"name": "a"}, headers=H).json()["key"]
    c.cookies.clear()
    assert c.post("/api/issues/NS-1/review/codex", headers={"Authorization": f"Bearer {key}"}).status_code == 403
    c.cookies.set("ns_session", "adm")
    assert c.post("/api/issues/NS-9/review/codex", headers=H).status_code == 404

    r = c.post("/api/issues/NS-1/review/codex", headers=H)
    assert r.status_code == 200 and r.json() == {"started": True, "ref": "NS-1", "runner": "codex"}, r.text
    it = c.get("/api/issues/NS-1").json()
    assert it["review_running"] and it["review_runner"] == "codex"
    assert "Codex에게 검토를 맡겼어요" in it["events"][-1]["body"]
    assert c.post("/api/issues/NS-1/review", headers=H).status_code == 409
    for _ in range(60):
        if not review.running_ref():
            break
        time.sleep(0.2)
    run = review.list_runs("NS-1")[0]
    assert run["status"] == "ok" and run["mode"] == "review" and run["runner"] == "codex", run
    log = (Path(os.environ["DEV_DATA_DIR"]) / "reviews" / run["log_file"]).read_text("utf-8")
    assert "codex result" in log
print("OK")
