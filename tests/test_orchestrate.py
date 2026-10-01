"""오케스트레이터 병합(DEV-40-1) — 임시 저장소로: 통과·충돌·더러운 작업 폴더·병합 뒤 테스트 실패(revert)·선행 미병합·기본 꺼짐."""
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

# 기본 꺼짐
assert orchestrate.settings("DEV") == {}

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

print("ok")
