"""Harness 편집 저장·이력(DEV-132-2) — 1·2층 규칙 파일을 사람이 고치면 저장소 base 브랜치에 그 파일만 커밋하고 diff를 돌려준다.

허용 경로는 고정 목록뿐이고(.env 등은 닿을 수 없다), 심볼릭 링크·실행 중 Task·읽은 뒤 바뀐 파일은 거절한다. push는 하지 않는다.
"""
import hashlib
import re
from pathlib import Path

import config
import db
import execute
import issues
import orchestrate
import prompts

MAX_FILE = 256 * 1024
COMMON = "common"   # 1층(dev의 harness/COMMON.md)과 공통 디자인(dev의 docs/DESIGN.md)
COMMON_PATHS = ("harness/COMMON.md", "docs/DESIGN.md")
PROJECT_RE = re.compile(r"^(AGENTS\.md|CLAUDE\.md|docs/project/0[0-5]_[A-Za-z0-9._-]+\.md)$")
PROMPTS = "prompts"   # 4층(dev의 harness/prompts/*.md) — prompts.SLOTS에 있는 파일만
PROMPTS_DIR = "harness/prompts/"
EDITOR = ["-c", "user.name=dev harness editor", "-c", "user.email=harness@dev.local"]


def _prompt_name(path: str) -> str:
    """harness/prompts/<파일>이고 알려진 프롬프트 파일이면 파일명, 아니면 빈 문자열."""
    name = path[len(PROMPTS_DIR):] if path.startswith(PROMPTS_DIR) else ""
    return name if name in prompts.SLOTS else ""


def _target(scope: str, path: str) -> tuple[Path, str]:
    """(저장소 경로, 검증된 상대 경로). 허용 밖이면 400."""
    if not isinstance(path, str) or "\\" in path or path.startswith("/") or ".." in path.split("/"):
        raise issues.StoreError("허용되지 않은 경로예요.", 400)
    if scope == COMMON:
        if path not in COMMON_PATHS:
            raise issues.StoreError("허용되지 않은 경로예요.", 400)
        repo = config.REPO_ROOT
    elif scope == PROMPTS:
        if not _prompt_name(path):
            raise issues.StoreError("허용되지 않은 경로예요.", 400)
        repo = config.REPO_ROOT
    else:
        project = next((p for p in issues.list_projects() if p["key"] == scope), None)
        if not project:
            raise issues.StoreError("프로젝트를 찾을 수 없어요.", 404)
        if not PROJECT_RE.match(path):
            raise issues.StoreError("허용되지 않은 경로예요.", 400)
        if not project["local_path"] or not Path(project["local_path"]).is_dir():
            raise issues.StoreError("프로젝트 저장소를 찾을 수 없어요.", 409)
        repo = Path(project["local_path"])
    # 경로 중 하나라도 심볼릭 링크면 저장소 밖을 건드릴 수 있다
    cur = repo
    for part in path.split("/"):
        cur = cur / part
        if cur.is_symlink():
            raise issues.StoreError("심볼릭 링크는 편집할 수 없어요.", 400)
    if not cur.resolve().is_relative_to(repo.resolve()):
        raise issues.StoreError("허용되지 않은 경로예요.", 400)
    return repo, path


def blob_sha(repo, path: str) -> str:
    """base 브랜치 끝에서 그 파일의 blob(없으면 빈 문자열) — 화면이 읽은 시점의 표지."""
    r = execute._run_git(repo, "rev-parse", "--verify", "--quiet", f"{execute.BASE_BRANCH}:{path}")
    return r.stdout.strip() if r.returncode == 0 else ""


def _running(scope: str) -> bool:
    """실행 중 run이 있나 — 공통 파일은 모든 프로젝트에 영향을 주므로 어떤 실행이든, 프로젝트 파일은 그 프로젝트만."""
    sql = ("SELECT 1 FROM runs r JOIN issues i ON i.id=r.issue_id JOIN projects p ON p.id=i.project_id "
           "WHERE r.status='running'")
    with db.connect() as c:
        if scope in (COMMON, PROMPTS):
            return c.execute(sql).fetchone() is not None
        return c.execute(sql + " AND p.key=?", (scope,)).fetchone() is not None


def _diff(repo, sha: str, path: str) -> str:
    return execute._run_git(repo, "show", "--format=", "-p", sha, "--", path).stdout


def save(scope: str, path: str, content: str, base_sha: str) -> dict:
    if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_FILE:
        raise issues.StoreError("내용이 256KB를 넘거나 글이 아니에요.", 400)
    repo, path = _target(scope, path)
    if scope == PROMPTS:
        found = prompts.problems(_prompt_name(path), content)
        if found:
            raise issues.StoreError("프롬프트를 저장할 수 없어요:\n" + "\n".join(found), 400)
    with orchestrate._lock:
        if _running(scope):
            raise issues.StoreError("실행 중인 Task가 있어 저장할 수 없어요. 끝난 뒤 다시 저장해 주세요.", 409)
        branch = execute._run_git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if branch != execute.BASE_BRANCH:
            raise issues.StoreError(f"저장소가 {execute.BASE_BRANCH} 브랜치에 있지 않아요.", 409)
        if execute._run_git(repo, "status", "--porcelain", "--", path).stdout.strip():
            raise issues.StoreError("그 파일에 커밋하지 않은 변경이 있어요. 덮어쓰지 않아요.", 409)
        if (base_sha or "") != blob_sha(repo, path):
            raise issues.StoreError("파일이 읽은 뒤 바뀌었어요. 새로 불러온 다음 다시 고쳐 주세요.", 409)
        file = repo / path
        old = file.read_bytes() if file.exists() else None
        new = content.encode("utf-8")
        if old == new:
            raise issues.StoreError("바뀐 내용이 없어요.", 400)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(new)
        r = execute._server_git(repo, "add", "--", path)
        if r.returncode == 0:
            r = execute._server_git(repo, *EDITOR, "commit", "-q", "-m", f"Harness 편집: {path}", "--", path)
        if r.returncode:
            if old is None:
                execute._run_git(repo, "rm", "-q", "--cached", "--", path)
                file.unlink(missing_ok=True)
            else:
                file.write_bytes(old)
                execute._run_git(repo, "reset", "-q", "--", path)
            raise issues.StoreError(f"커밋하지 못했어요: {r.stderr.strip() or r.stdout.strip()}", 409)
        sha = execute._git(repo, "rev-parse", "HEAD")
    return {"scope": scope, "path": path, "commit": sha, "base_sha": blob_sha(repo, path), "diff": _diff(repo, sha, path)}


def history(scope: str, path: str, limit: int = 20) -> dict:
    repo, path = _target(scope, path)
    out = execute._run_git(repo, "log", f"-{max(1, min(limit, 50))}", "--format=%H%x1f%an%x1f%aI%x1f%s", execute.BASE_BRANCH, "--", path)
    commits = []
    for line in out.stdout.splitlines():
        sha, author, date, subject = (line.split("\x1f") + ["", "", "", ""])[:4]
        commits.append({"commit": sha, "author": author, "date": date, "subject": subject, "diff": _diff(repo, sha, path)})
    return {"scope": scope, "path": path, "base_sha": blob_sha(repo, path), "commits": commits}
