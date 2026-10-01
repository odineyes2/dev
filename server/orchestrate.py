"""
오케스트레이터(DEV-40) — 실행이 통과한 Task 브랜치를 base 브랜치에 자동으로 합친다.
`<데이터폴더>/orchestrate.json`에서 프로젝트의 `auto_merge`가 켜진 곳만 동작한다(기본 꺼짐).

순서: 작업 폴더 확인(base 체크아웃·깨끗) → 선행 Task 병합 확인 → `merge-tree`로 충돌 재확인 → `git merge --no-ff`
→ 병합한 결과로 `python tests/test_*.py` → 실패하면 `git revert -m 1`(이력 보존, reset은 쓰지 않음).
결과는 Task 상태로: 작업 폴더·선행 문제는 on_hold, 충돌·테스트 실패는 changes_requested, 성공은 그대로(in_review)에 댓글.

병합 뒤 재시작(DEV-40-2): 바뀐 파일이 `restart_when` 패턴에 걸릴 때만 `restart_cmd`를 돌린다(화면 파일만이면 생략).
`busy_url`이 있으면 그 응답 `{"busy": true}`가 풀릴 때까지 최대 `wait_minutes` 기다리고, 넘으면 재시작 없이 on_hold.
재시작 뒤 `health_url`이 `health_seconds` 안에 200이 아니면 병합을 revert하고 한 번 더 재시작해 복구, changes_requested.
상위 이슈 정리는 DEV-40-3.

`data/orchestrate.json` 예시(프로젝트 키별, 빠진 값은 DEFAULTS):
    {"DEV": {"auto_merge": true, "pm2_app": "dev", "health_url": "http://127.0.0.1:8300/api/health",
             "restart_when": ["server/*", "ecosystem.config.js"]},
     "NS":  {"auto_merge": false, "pm2_app": "nightshift", "health_url": "http://127.0.0.1:8000/api/health",
             "restart_when": ["server/*", "ecosystem.config.js"], "busy_url": "http://127.0.0.1:8000/api/jobs/busy"}}
"""
import fnmatch
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import config
import issues
from execute import BASE_BRANCH, _run_git, branch_name

SETTINGS = config.DATA_DIR / "orchestrate.json"
GIT_ID = ["-c", "user.name=dev orchestrator", "-c", "user.email=orchestrator@dev.local"]
_lock = threading.Lock()   # ponytail: 프로세스 전체에 병합 하나씩 — 프로젝트별 잠금은 동시 병합이 필요해지면
DEFAULTS = {"restart_when": ["server/*", "ecosystem.config.js"], "restart_cmd": "npx pm2 restart ecosystem.config.js --only {app} --update-env",
            "health_seconds": 60, "wait_minutes": 60, "poll_seconds": 30}


def settings(project_key: str) -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8")).get(project_key) or {}
    except (OSError, ValueError):
        return {}


def _get(url, timeout=5):
    """(상태 코드, 본문) — 연결 실패는 (0, "")."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (OSError, ValueError):
        return 0, ""


def _busy(cfg) -> bool:
    if not cfg.get("busy_url"):
        return False
    code, body = _get(cfg["busy_url"])
    try:
        return code != 200 or bool(json.loads(body).get("busy"))
    except (ValueError, AttributeError):
        return True   # 모르면 바쁜 것으로 본다 — 작업 서브프로세스를 고아로 만들지 않게


def _healthy(cfg) -> bool:
    if not cfg.get("health_url"):
        return True
    deadline = time.monotonic() + cfg["health_seconds"]
    while True:
        if _get(cfg["health_url"])[0] == 200:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)


def _restart(repo, cfg) -> str | None:
    """재시작 명령을 돌린다. 실패하면 출력 끝부분."""
    r = subprocess.run(cfg["restart_cmd"].format(app=cfg.get("pm2_app", "")), shell=True, cwd=repo, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=300)
    return ((r.stdout + r.stderr).strip()[-800:] or f"종료 코드 {r.returncode}") if r.returncode else None


def deploy(repo: str, cfg: dict, sha: str, say=lambda msg: None) -> tuple[str, str]:
    """병합 커밋 sha 뒤 필요하면 재시작하고 health를 본다. (Task가 갈 상태, 메모) — 문제없으면 ("merged", 메모).
    ponytail: dev가 자기 자신(pm2_app=dev)을 재시작하면 이 스레드도 죽어 health 확인·복구를 못 한다 — 상태를 DB에 적고 뜬 뒤 이어 하기는 필요해지면."""
    cfg = {**DEFAULTS, **cfg}
    files = _git(repo, "diff", "--name-only", f"{sha}^1", sha).stdout.split()
    hits = [f for f in files if any(fnmatch.fnmatch(f, p) for p in cfg["restart_when"])]
    if not hits or not cfg.get("pm2_app"):
        return "merged", "재시작이 필요 없는 변경이라 재시작하지 않았어요."
    if _busy(cfg):
        say(f"⏳ 실행 중인 작업이 있어 끝나면 `{cfg['pm2_app']}`를 재시작할 예정이에요(최대 {cfg['wait_minutes']}분).")
        deadline = time.monotonic() + cfg["wait_minutes"] * 60
        while _busy(cfg):
            if time.monotonic() >= deadline:
                return "on_hold", f"실행 중인 작업이 {cfg['wait_minutes']}분 안에 안 끝나 `{cfg['pm2_app']}`를 재시작하지 않았어요 — 병합은 됐으니 작업이 끝난 뒤 직접 재시작해 주세요."
            time.sleep(cfg["poll_seconds"])
    err = _restart(repo, cfg)
    if not err and _healthy(cfg):
        return "merged", f"`{cfg['pm2_app']}`를 재시작했고 health 확인을 통과했어요({', '.join(hits[:5])} 변경)."
    why = f"재시작 명령이 실패했어요: {err}" if err else f"재시작 뒤 {cfg['health_seconds']}초 안에 health가 200이 아니었어요"
    r = _git(repo, "revert", "-m", "1", "--no-edit", sha)
    if r.returncode:
        return "changes_requested", f"{why}. revert도 실패했어요 — 직접 확인해 주세요({r.stderr.strip()})."
    back = "복구됐어요" if not _restart(repo, cfg) and _healthy(cfg) else "그래도 health가 안 돌아왔어요 — 바로 확인해 주세요"
    return "changes_requested", f"{why}. 병합을 revert 커밋으로 되돌리고 다시 재시작했어요 — {back}."


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
    cfg = settings(issue["project_key"])
    if issue["status"] != "in_review" or not cfg.get("auto_merge"):
        return None
    repo = next((p["local_path"] for p in issues.list_projects() if p["key"] == issue["project_key"]), "")
    with _lock:
        status, note = merge(repo, ref, [b["ref"] for b in issue["blocked_by"]])
        if status == "merged":
            issues.add_comment(actor, ref, f"🔀 `{branch_name(ref)}`를 {BASE_BRANCH}에 병합했어요(`{note[:7]}`) — 병합 뒤 테스트 통과.")
            status, note = deploy(repo, cfg, note, lambda msg: issues.add_comment(actor, ref, msg))
    if status == "merged":
        issues.add_comment(actor, ref, f"🔁 {note}")
    else:
        issues.set_status(actor, ref, status, f"🔀 자동 병합: {note}")
    return status


def promote_parent(actor: dict, ref: str) -> bool:
    """Task가 병합된 뒤(DEV-40-3) — 형제 Task가 전부 base에 들어갔으면(사람이 done/closed로 끝낸 것도) 상위 이슈를 in_review로
    올리고 무엇이 반영됐고 어디서 확인하는지 한 번에 요약한다. 올렸으면 True."""
    parent_ref = issues.get_issue(ref)["parent_ref"]
    if not parent_ref:
        return False
    parent = issues.get_issue(parent_ref)
    if parent["status"] in ("in_review", "done", "closed") or "goal" in parent["labels"]:   # goal은 사용자만 닫는다
        return False
    repo = next((p["local_path"] for p in issues.list_projects() if p["key"] == parent["project_key"]), "")
    lines, checks = [], []
    for ch in parent["children"]:
        finished = ch["status"] in ("done", "closed")
        if not finished and not (ch["status"] == "in_review" and _merged(repo, ch["ref"])):
            return False
        full = issues.get_issue(ch["ref"])
        sha = _git(repo, "log", "-1", "--merges", "--fixed-strings", f"--grep=({ch['ref']})", "--format=%h", BASE_BRANCH).stdout.strip()
        restart = next((e["body"][2:].strip() for e in reversed(full["events"]) if e["kind"] == "comment" and e["body"].startswith("🔁")), "")
        lines.append(f"- **{ch['ref']}** {ch['title']} — " + (f"병합 `{sha}`" if sha else ch["status"]) + (f" · {restart}" if restart else ""))
        how = next((e["body"] for e in reversed(full["events"]) if e["kind"] == "status" and e["data"].get("to") == "in_review" and e["body"]), "")
        if how:
            checks.append(f"- **{ch['ref']}**: {how[:600]}")
    note = (f"🔀 하위 Task {len(lines)}개가 모두 {BASE_BRANCH}에 반영됐어요.\n\n" + "\n".join(lines)
            + ("\n\n**확인할 곳**\n" + "\n".join(checks) if checks else "") + "\n\n확인했으면 Done으로 바꿔 주세요.")
    issues.set_status(actor, parent_ref, "in_review", note)
    return True


def merge_state(issue: dict) -> str | None:
    """Task 줄의 상태 문구(DEV-40-3) — 오케스트레이터가 타임라인에 남긴 마지막 흔적으로 판단한다. 해당 없으면 None."""
    for e in reversed(issue.get("events", [])):
        b = e["body"] or ""
        if e["kind"] == "comment" and b.startswith("🛠"):   # 다시 실행을 맡겼으면 이전 병합 흔적은 지난 일
            break
        if e["kind"] == "comment" and b.startswith("⏳"):
            return "재시작 대기"
        if e["kind"] == "comment" and b.startswith(("🔀", "🔁")):
            return "병합됨"
        if e["kind"] == "status" and b.startswith("🔀 자동 병합"):
            return "되돌림" if "revert" in b else "병합 대기" if e["data"].get("to") == "on_hold" else None
    if issue.get("status") == "in_review" and issue.get("parent_ref") and settings(issue["project_key"]).get("auto_merge"):
        return "병합 대기"
    return None
