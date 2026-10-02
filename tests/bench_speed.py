"""속도 측정(DEV-42) — 임시 데이터 폴더에 이슈 수백 개·Task 10개짜리 부모·git 브랜치를 만들고 엔드포인트별·구간별 ms 표를 낸다.
nightshift 확인은 가짜(즉시 응답)라 운영의 auth 구간(nightshift 왕복)은 여기서 안 보인다 — 운영은 Server-Timing·timing.log로 본다.
python tests/bench_speed.py [반복 횟수, 기본 20]"""
import os, statistics, subprocess, sys, tempfile, time
from pathlib import Path

os.environ["DEV_DATA_DIR"] = tempfile.mkdtemp()
os.environ["DEV_AUTO_REVIEW"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as A, auth, db, execute, issues  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 20
repo = Path(tempfile.mkdtemp()) / "repo"
repo.mkdir()


def git(*a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, check=True, capture_output=True)


git("init", "-q", "-b", execute.BASE_BRANCH)
(repo / "a.txt").write_text("a\n"); git("add", "."); git("commit", "-qm", "init")

db.init()
me = {"kind": "human", "name": "admin"}
issues.create_project(me, "BN", "bench", "", str(repo))
for i in range(300):
    issues.create_issue(me, "BN", f"이슈 {i}", f"본문 {i}\n" * 20)
for i in range(1, 30):   # 타임라인이 긴 이슈
    issues.add_comment(me, "BN-1", f"댓글 {i}")
parent = issues.create_issue(me, "BN", "Task 10개짜리 부모", status="triage")["ref"]
issues.post_plan(me, parent, "## Tasks\n" + "\n".join(f"{i}. 할 일 {i}" for i in range(1, 11)))
issues.decide(me, parent, "approve", "", plan_version=1)
task = f"{parent}-1"
for k in range(1, 11):   # Task마다 커밋 하나 있는 relay 브랜치
    git("checkout", "-q", "-b", execute.branch_name(f"{parent}-{k}"))
    (repo / f"t{k}.txt").write_text("t\n"); git("add", "."); git("commit", "-qm", f"t{k}")
    git("checkout", "-q", execute.BASE_BRANCH)

auth._client = httpx.AsyncClient(transport=httpx.MockTransport(
    lambda req: httpx.Response(200, json={"user": {"id": 1, "username": "admin", "role": "admin"}})))

ENDPOINTS = [
    ("health", "/api/health"),
    ("auth/me", "/api/auth/me"),
    ("projects", "/api/projects"),
    ("목록 50개", "/api/issues?limit=50"),
    ("목록 Task 포함 500", "/api/issues"),
    ("jobs", "/api/jobs"),
    ("이슈 상세(댓글 많음)", "/api/issues/BN-1"),
    ("부모 상세(Task 10)", f"/api/issues/{parent}"),
    ("Task 상세(git)", f"/api/issues/{task}"),
    ("화면 index", "/"),
    ("화면 app.js", "/app.js"),
]


def parse(h: str) -> dict:
    out = {}
    for part in h.split(","):
        name, _, dur = part.strip().partition(";dur=")
        out[name] = float(dur)
    return out


rows = []
with TestClient(A.app) as c:
    c.cookies.set("ns_session", "adm")
    for label, url in ENDPOINTS:
        c.get(url)   # 첫 호출(데우기)은 빼고
        totals, spans = [], {}
        for _ in range(N):
            t = time.perf_counter()
            r = c.get(url)
            totals.append((time.perf_counter() - t) * 1000)
            assert r.status_code == 200, (url, r.status_code)
            assert "total;dur=" in r.headers.get("server-timing", ""), url
            for k, v in parse(r.headers["server-timing"]).items():
                spans.setdefault(k, []).append(v)
        med = {k: statistics.median(v) for k, v in spans.items()}
        detail = " ".join(f"{k} {v:.1f}" for k, v in med.items() if k != "total")
        rows.append((label, statistics.median(totals), max(totals), med["total"], detail))

print(f"\n반복 {N}회 중앙값(ms) — 이슈 {300 + 2 + 10}개, Task 10개 부모, git 브랜치 10개\n")
print("| 엔드포인트 | 왕복 | 최대 | 서버 total | 구간(Server-Timing) |")
print("|---|---:|---:|---:|---|")
for label, med, mx, total, detail in rows:
    print(f"| {label} | {med:.1f} | {mx:.1f} | {total:.1f} | {detail} |")
