"""오케스트레이터 병합(DEV-40-1) — 임시 저장소로: 통과·충돌·더러운 작업 폴더·병합 뒤 테스트 실패(revert)·선행 미병합·기본 꺼짐.
재시작(DEV-40-2) — 가짜 pm2·health로: 서버 변경만 재시작·화면만은 생략·실행 중 작업 대기·대기 초과 on_hold·health 실패 복구."""
import os, subprocess, sys, tempfile
from pathlib import Path

os.environ["DEV_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import orchestrate  # noqa: E402

repo = Path(tempfile.mkdtemp())


def git(*a):
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def branch(ref, files):
    git("checkout", "-q", "-b", f"relay/{ref}", "main")
    for name, text in files.items():
        (repo / name).parent.mkdir(exist_ok=True)
        (repo / name).write_text(text)
    git("add", "."); git("commit", "-qm", ref); git("checkout", "-q", "main")


git("init", "-q", "-b", "main")
(repo / "tests").mkdir()
(repo / "tests" / "test_ok.py").write_text("print('ok')\n")
(repo / "a.txt").write_text("a\n")
git("add", "."); git("commit", "-qm", "init")

# 기본 켜짐
assert orchestrate.settings("DEV") == {"auto_merge": True}

# 통과 — --no-ff 병합 커밋
branch("T-1", {"b.txt": "b\n"})
status, sha = orchestrate.merge(str(repo), "T-1")
assert status == "merged", sha
assert git("rev-parse", "HEAD") == sha and len(git("rev-list", "--parents", "-n1", "HEAD").split()) == 3
assert (repo / "b.txt").exists()

# 선행 미병합 → on_hold, 선행이 병합돼 있으면 통과
branch("T-2", {"c.txt": "c\n"})
branch("T-3", {"d.txt": "d\n"})
status, note = orchestrate.merge(str(repo), "T-3", ["T-2"])
assert status == "on_hold" and "T-2" in note, note
assert orchestrate.merge(str(repo), "T-2", ["T-1"])[0] == "merged"
assert orchestrate.merge(str(repo), "T-3", ["T-2"])[0] == "merged"

# 충돌 → changes_requested, main은 그대로
branch("T-4", {"a.txt": "branch\n"})
(repo / "a.txt").write_text("main\n"); git("commit", "-qam", "main edit")
before = git("rev-parse", "HEAD")
status, note = orchestrate.merge(str(repo), "T-4")
assert status == "changes_requested" and "a.txt" in note, note
assert git("rev-parse", "HEAD") == before and not git("status", "--porcelain")

# 더러운 작업 폴더 → on_hold, 병합 안 함
branch("T-5", {"e.txt": "e\n"})
(repo / "a.txt").write_text("dirty\n")
status, note = orchestrate.merge(str(repo), "T-5")
assert status == "on_hold" and "커밋 안 된" in note, note
assert git("rev-parse", "HEAD") == before
git("checkout", "--", "a.txt")

# base가 아닌 브랜치 → on_hold
git("checkout", "-q", "relay/T-5")
assert orchestrate.merge(str(repo), "T-5")[0] == "on_hold"
git("checkout", "-q", "main")

# 병합 뒤 테스트 실패 → revert 커밋, 내용은 이전 상태
branch("T-6", {"tests/test_bad.py": "raise SystemExit('boom')\n", "f.txt": "f\n"})
status, note = orchestrate.merge(str(repo), "T-6")
assert status == "changes_requested" and "test_bad.py" in note and "boom" in note, note
assert git("log", "-1", "--format=%s").startswith("Revert")
assert not (repo / "f.txt").exists() and not git("diff", before, "HEAD", "--stat")

# ---- 재시작·health(DEV-40-2) — 가짜 pm2(로그 파일에 한 줄)와 가짜 health·busy 서버 ----
import json, threading  # noqa: E401,E402
from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402

state = {"health": 200, "busy": 0}   # busy: 앞으로 몇 번 더 "바쁨"이라고 답할지


class Fake(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/busy":
            body = json.dumps({"busy": state["busy"] > 0}).encode()
            state["busy"] = max(0, state["busy"] - 1)
            code = 200
        else:
            body, code = b"{}", state["health"]
        self.send_response(code); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_port}"
log = Path(tempfile.mkdtemp()) / "pm2.log"
fake_pm2 = log.with_name("fake_pm2.py")
fake_pm2.write_text("import sys\nopen(sys.argv[1], 'a').write(sys.argv[2] + '\\n')\n")
cfg = {"pm2_app": "app", "restart_cmd": f'"{sys.executable}" "{fake_pm2}" "{log}" {{app}}',
       "health_url": url + "/health", "health_seconds": 1, "poll_seconds": 0.05}


def restarts():
    return log.read_text().count("app") if log.exists() else 0


def merged(ref, files):
    branch(ref, files)
    status, sha = orchestrate.merge(str(repo), ref)
    assert status == "merged", sha
    return sha


# 화면 파일만 → 재시작 안 함
status, note = orchestrate.deploy(str(repo), cfg, merged("T-7", {"static/x.js": "1\n"}))
assert status == "merged" and restarts() == 0, note

# 서버 파일 → 재시작 한 번, health 통과
status, note = orchestrate.deploy(str(repo), cfg, merged("T-8", {"server/x.py": "1\n"}))
assert status == "merged" and restarts() == 1 and "health" in note, note

# 실행 중 작업 → 기다렸다가 재시작, 대기 안내
said = []
state["busy"] = 3
status, note = orchestrate.deploy(str(repo), {**cfg, "busy_url": url + "/busy"}, merged("T-9", {"server/y.py": "1\n"}), said.append)
assert status == "merged" and restarts() == 2 and said and "재시작할 예정" in said[0], (note, said)

# 끝까지 안 끝남 → 재시작 없이 on_hold
state["busy"] = 10 ** 6
status, note = orchestrate.deploy(str(repo), {**cfg, "busy_url": url + "/busy", "wait_minutes": 0.002}, merged("T-10", {"server/z.py": "1\n"}))
assert status == "on_hold" and restarts() == 2, note
state["busy"] = 0

# health 실패 → revert 커밋, 다시 재시작해 복구 시도
state["health"] = 500
before = git("rev-parse", "HEAD")
sha = merged("T-11", {"server/w.py": "1\n"})
status, note = orchestrate.deploy(str(repo), cfg, sha)
assert status == "changes_requested" and "revert" in note and restarts() == 4, note
assert git("log", "-1", "--format=%s").startswith("Revert") and not git("diff", before, "HEAD", "--stat")
state["health"] = 200
srv.shutdown()

print("ok")

# 병합 뒤 테스트는 NTFY 비밀값 없이 돌아간다(진짜 알림이 나가지 않게)
os.environ["NTFY_TOPIC"] = "should-not-leak"
(repo / "tests" / "test_env.py").write_text("import os,sys; sys.exit(1 if os.environ.get('NTFY_TOPIC') else 0)\n")
assert orchestrate.run_tests(str(repo)) is None
print("ok env")
