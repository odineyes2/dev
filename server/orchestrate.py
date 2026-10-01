"""
오케스트레이터(DEV-40) — 실행이 통과한 Task 브랜치를 base 브랜치에 자동으로 합친다.
`<데이터폴더>/orchestrate.json`에서 프로젝트의 `auto_merge`가 켜진 곳만 동작한다(기본 꺼짐).

순서: 작업 폴더 확인(base 체크아웃·깨끗) → 선행 Task 병합 확인 → `merge-tree`로 충돌 재확인 → `git merge --no-ff`
→ 병합한 결과로 `python tests/test_*.py` → 실패하면 `git revert -m 1`(이력 보존, reset은 쓰지 않음).
결과는 Task 상태로: 작업 폴더·선행 문제는 on_hold, 충돌·테스트 실패는 changes_requested, 성공은 그대로(in_review)에 댓글.
재시작·health 확인은 DEV-40-2, 상위 이슈 정리는 DEV-40-3.
"""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import config
import issues
from execute import BASE_BRANCH, _run_git, branch_name

SETTINGS = config.DATA_DIR / "orchestrate.json"
GIT_ID = ["-c", "user.name=dev orchestrator", "-c", "user.email=orchestrator@dev.local"]
_lock = threading.Lock()   # ponytail: 프로세스 전체에 병합 하나씩 — 프로젝트별 잠금은 동시 병합이 필요해지면


def settings(project_key: str) -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8")).get(project_key) or {}
    except (OSError, ValueError):
        return {}


def _git(repo, *args):
    return _run_git(repo, *GIT_ID, *args)


def _merged(repo, ref) -> bool:
    return _git(repo, "merge-base", "--is-ancestor", branch_name(ref), BASE_BRANCH).returncode == 0


def run_tests(repo) -> str | None:
    """repo의 tests/test_*.py를 하나씩 돌린다. 실패한 첫 파일과 출력 끝부분(없으면 None)."""
    for t in sorted(Path(repo, "tests").glob("test_*.py")):
        r = subprocess.run([sys.executable, str(t)], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=600)
        if r.returncode:
            return f"{t.name}: {(r.stdout + r.stderr).strip()[-1500:]}"
    return None


def merge(repo: str, ref: str, after: list[str] = ()) -> tuple[str, str]:
    """relay/<ref>를 base에 병합한다. (Task가 갈 상태, 메모) — 성공이면 ("merged", 병합 커밋)."""
    head = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if head != BASE_BRANCH:
        return "on_hold", f"저장소가 {BASE_BRANCH}가 아니라 {head}에 있어 병합 보류 — {BASE_BRANCH}로 돌려 놓은 뒤 재개해 주세요."
    if _git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        return "on_hold", "작업 폴더에 커밋 안 된 변경이 있어 병합 보류 — 정리 후 재개해 주세요."
    waiting = [p for p in after if not _merged(repo, p)]
    if waiting:
        return "on_hold", f"선행 Task({', '.join(waiting)})가 아직 병합되지 않아 병합 보류."
    branch = branch_name(ref)
    r = _git(repo, "merge-tree", "--write-tree", "--name-only", "--no-messages", BASE_BRANCH, branch)
    if r.returncode:
        files = r.stdout.split()[1:]
        return "changes_requested", f"{BASE_BRANCH}와 충돌해서 병합하지 않았어요: {', '.join(files) or r.stderr.strip()}"
    r = _git(repo, "merge", "--no-ff", "--no-edit", "-m", f"Merge {branch} ({ref})", branch)
    if r.returncode:
        _git(repo, "merge", "--abort")
        return "changes_requested", f"병합에 실패해서 되돌렸어요: {(r.stdout + r.stderr).strip()[-800:]}"
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    failed = run_tests(repo)
    if failed:
        r = _git(repo, "revert", "-m", "1", "--no-edit", sha)
        how = "revert 커밋으로 되돌렸어요" if not r.returncode else f"revert도 실패했어요 — 직접 확인해 주세요({r.stderr.strip()})"
        return "changes_requested", f"병합 뒤 테스트가 실패해 {how}.\n\n```\n{failed}\n```"
    return "merged", sha


def handle(actor: dict, ref: str) -> str | None:
    """실행이 끝난 Task를 병합한다. auto_merge가 꺼졌거나 Task가 in_review가 아니면 아무것도 안 한다(None)."""
    issue = issues.get_issue(ref)
    if issue["status"] != "in_review" or not settings(issue["project_key"]).get("auto_merge"):
        return None
    repo = next((p["local_path"] for p in issues.list_projects() if p["key"] == issue["project_key"]), "")
    with _lock:
        status, note = merge(repo, ref, [b["ref"] for b in issue["blocked_by"]])
    if status == "merged":
        issues.add_comment(actor, ref, f"🔀 `{branch_name(ref)}`를 {BASE_BRANCH}에 병합했어요(`{note[:7]}`) — 병합 뒤 테스트 통과.")
    else:
        issues.set_status(actor, ref, status, f"🔀 자동 병합: {note}")
    return status
