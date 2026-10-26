"""재시작 가드의 병합 diff·적용 목록·워커 선택을 임시 Git 저장소에서 검사한다."""

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import restart_guard  # noqa: E402


def git(repo, *args):
    result = subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@local", *args],
                            cwd=repo, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def fake_command(output="", returncode=0):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, returncode, output, "hidden database error")

    return run, calls


with tempfile.TemporaryDirectory() as temporary:
    repo = Path(temporary)
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "initial")
    git(repo, "checkout", "-qb", "feature")
    migration_dir = repo / "backend" / "migrations"
    migration_dir.mkdir(parents=True)
    (migration_dir / "025_issue_depth_length.sql").write_text("SELECT 1;\n", encoding="utf-8")
    (migration_dir / "026_issue_estimated_tokens.sql").write_text("SELECT 2;\n", encoding="utf-8")
    (repo / "backend" / "research").mkdir()
    (repo / "backend" / "research" / "worker.py").write_text("pass\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "feature")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--no-ff", "-qm", "merge feature", "feature")
    sha = git(repo, "rev-parse", "HEAD")

    cfg = {"migrations": {"glob": "backend/migrations/*.sql", "applied_cmd": "read-only-db-query"}}
    run, calls = fake_command("025_issue_depth_length\n")
    result = restart_guard.check_migrations(str(repo), cfg, sha, command_runner=run)
    assert result.status == "pending" and result.files == ("backend/migrations/026_issue_estimated_tokens.sql",)
    assert "마이그레이션 적용 필요" in result.message and "cutover 후 재시작" in result.message
    assert len(calls) == 1 and calls[0][0] == "read-only-db-query"
    assert calls[0][1]["timeout"] == restart_guard.COMMAND_TIMEOUT

    run, _ = fake_command("025_issue_depth_length\n026_issue_estimated_tokens\n")
    assert restart_guard.check_migrations(str(repo), cfg, sha, command_runner=run).status == "clear"

    for output, code in [("garbled version!\n", 0), ("", 1), ("025_issue_depth_length\n\n", 0)]:
        run, _ = fake_command(output, code)
        result = restart_guard.check_migrations(str(repo), cfg, sha, command_runner=run)
        assert result.status == "failed" and "확인 실패" in result.message
        assert "hidden database error" not in result.message

    assert restart_guard.check_migrations(str(repo), {"migrations": {"glob": "backend/migrations/*.sql"}}, sha).status == "failed"
    run, calls = fake_command("not consulted")
    assert restart_guard.check_migrations(str(repo), {}, sha, command_runner=run).status == "clear"
    assert not calls

    git(repo, "checkout", "-qb", "no-migration")
    (repo / "backend" / "research" / "worker.py").write_text("pass  # changed\n", encoding="utf-8")
    git(repo, "commit", "-qam", "worker only")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--no-ff", "-qm", "merge worker", "no-migration")
    new_sha = git(repo, "rev-parse", "HEAD")
    run, calls = fake_command("bad output")
    assert restart_guard.check_migrations(str(repo), cfg, new_sha, command_runner=run).status == "clear"
    assert not calls

    workers = {"workers": [{"app": "moai-research-worker", "when": ["backend/research/*"]},
                           {"app": "moai-issue-worker", "when": ["backend/issues/*"]}]}
    assert restart_guard.worker_apps(["backend/research/worker.py"], workers) == ("moai-research-worker",)
    assert restart_guard.worker_apps(["README.md"], workers) == ()
    assert restart_guard.check_worker_busy(str(repo), {}).status == "unconfigured"
    for output, status, count in [("0\n", "idle", 0), ("2\n", "busy", 2), ("NaN\n", "failed", None)]:
        run, _ = fake_command(output)
        result = restart_guard.check_worker_busy(str(repo), {"worker_busy_cmd": "read-only-job-count"}, command_runner=run)
        assert (result.status, result.count) == (status, count)

print("restart guard checks passed")
