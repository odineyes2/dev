"""
오케스트레이터(DEV-40) — 실행이 통과한 Task 브랜치를 base 브랜치에 자동으로 합친다.
`<데이터폴더>/orchestrate.json`에서 프로젝트별로 조정한다. `auto_merge`는 기본 켜짐(끄려면 false) — 재시작은 `pm2_app`을 적은 프로젝트만 한다.

순서: 작업 폴더 확인(base 체크아웃·깨끗) → 선행 Task 병합 확인 → 최신 base와 Task 커밋 고정
→ 임시 detached worktree에서 `git merge --no-ff` → `tests/test_*.py` 전체 검사
→ 원본 base·Task·작업 폴더가 그대로인지 확인 → 검사한 커밋을 `git merge --ff-only`로 운영에 반영.
충돌·검사 실패는 운영 사본을 변경하지 않는다. 재시작·health 실패의 revert 복구는 유지한다.
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
import tempfile
import threading
import time
import uuid

import db
import urllib.request
from pathlib import Path

import config
import issues
from execute import BASE_BRANCH, SECRET_ENV, _run_git, branch_name

SETTINGS = config.DATA_DIR / "orchestrate.json"
GIT_ID = ["-c", "user.name=dev orchestrator", "-c", "user.email=orchestrator@dev.local"]
PROCESS_ID = uuid.uuid4().hex
BOOT_SHA = _run_git(str(Path(__file__).resolve().parent.parent), "rev-parse", "HEAD").stdout.strip()
ACTIVE_PHASES = ("ready", "checking", "applying", "deployed", "restart_requested", "rollback_requested", "rollback_applied")
_lock = threading.RLock()   # 병합과 거절 롤백을 같은 순서로 직렬화한다.
DEFAULTS = {"restart_when": ["server/*", "ecosystem.config.js"], "restart_cmd": "npx pm2 restart ecosystem.config.js --only {app} --update-env",
            "health_seconds": 60, "wait_minutes": 60, "poll_seconds": 30}


def settings(project_key: str) -> dict:
    try:
        return {"auto_merge": True, **(json.loads(SETTINGS.read_text(encoding="utf-8")).get(project_key) or {})}
    except (OSError, ValueError):
        return {"auto_merge": True}


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
    """발견한 검사를 모두 돌리고 실패 시 종료 코드와 전체 출력을 보존한다.
    실패한 파일은 한 번 더 돌린다 — 브라우저 검사(test_ui)의 일시적 연결 끊김·타임아웃이 멀쩡한 Task를 떨어뜨리지 않게. 진짜 회귀는 두 번 다 실패한다."""
    def once(t):
        try:
            return subprocess.run([sys.executable, str(t)], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  env={**{k: v for k, v in os.environ.items() if k not in SECRET_ENV}, "PYTHONIOENCODING": "utf-8", "PYTHONFAULTHANDLER": "1"}, timeout=600)   # 테스트가 진짜 ntfy로 알림을 보내지 않게
        except subprocess.TimeoutExpired as error:
            def decoded(value):
                return value.decode('utf-8', 'replace') if isinstance(value, bytes) else value or ''
            return subprocess.CompletedProcess(error.cmd, 124, decoded(error.stdout), decoded(error.stderr) + '\n검사 시간 제한 600초 초과(표시용 종료 코드 124)')
    failures = []
    for t in sorted(Path(repo, "tests").glob("test_*.py")):
        r = once(t)
        if r.returncode:
            first, r = r, once(t)
            if not r.returncode:   # 불안정한 검사 — 통과로 보되 첫 실패는 남긴다
                logs = config.DATA_DIR / "merge-tests"
                logs.mkdir(parents=True, exist_ok=True)
                (logs / f"{t.stem}-flaky-{time.time_ns()}.log").write_text(f"검사: {t}\n재시도에서 통과\n\n--- stdout ---\n{first.stdout}\n--- stderr ---\n{first.stderr}", encoding="utf-8")
        if r.returncode:
            detail = (f"검사: {t}\n작업 폴더: {repo}\nPython: {sys.executable}\n"
                      f"종료 코드: {r.returncode} (0x{r.returncode & 0xffffffff:08X})\n"
                      f"\n--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}")
            logs = config.DATA_DIR / "merge-tests"
            logs.mkdir(parents=True, exist_ok=True)
            log = logs / f"{t.stem}-{time.time_ns()}.log"
            log.write_text(detail, encoding="utf-8")
            output = (r.stdout + r.stderr).strip() or "표준 출력과 오류 출력이 없어요."
            failures.append(f"{t.name}: 종료 코드 {r.returncode} (0x{r.returncode & 0xffffffff:08X})\n전체 로그: {log}\n{output[-1500:]}")
    return '\n\n'.join(failures) or None


def merge(repo: str, ref: str, after: list[str] = ()) -> tuple[str, str]:
    """시험 병합을 검사한 뒤 동일한 커밋을 반영한다. 병합·거절 롤백과 직렬화한다."""
    with _lock:
        return _merge(repo, ref, after)


def _merge(repo: str, ref: str, after: list[str], checkpoint=None, expected_task=None) -> tuple[str, str]:
    head = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if head != BASE_BRANCH:
        return "on_hold", f"저장소가 {BASE_BRANCH}가 아니라 {head}에 있어 병합 보류 — {BASE_BRANCH}로 돌려 놓은 뒤 재개해 주세요."
    if _git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        return "on_hold", "작업 폴더에 커밋 안 된 변경이 있어 병합 보류 — 정리 후 재개해 주세요."
    waiting = [p for p in after if not _merged(repo, p)]
    if waiting:
        return "on_hold", f"선행 Task({', '.join(waiting)})가 아직 병합되지 않아 병합 보류."
    branch = branch_name(ref)
    before = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    task = _git(repo, 'rev-parse', '--verify', f'{branch}^{{commit}}')
    if task.returncode:
        return 'on_hold', 'Task 브랜치를 읽을 수 없어 시험 병합을 보류했어요.'
    task_sha = task.stdout.strip()
    if expected_task and task_sha != expected_task:
        return "on_hold", "완료 등록 뒤 Task 커밋이 바뀌었어요."
    if not _git(repo, 'merge-base', '--is-ancestor', task_sha, before).returncode:
        return 'on_hold', '새로 병합할 Task 커밋이 없어요 — 이미 반영했거나 롤백한 작업을 확인해 주세요.'
    folder = config.DATA_DIR / 'merge-worktrees'
    try:
        folder.mkdir(parents=True, exist_ok=True)
        worktree = Path(tempfile.mkdtemp(prefix='merge-', dir=folder)).resolve()
    except OSError as error:
        return 'on_hold', '시험 병합 폴더를 만들지 못했어요: ' + str(error)
    added = False
    try:
        r = _git(repo, 'worktree', 'add', '--detach', str(worktree), before)
        if r.returncode:
            return 'on_hold', '시험 병합 사본을 만들지 못했어요: ' + r.stderr.strip()
        added = True
        r = _git(str(worktree), 'merge', '--no-ff', '--no-edit', '-m', f'Merge {branch} ({ref})', task_sha)
        if r.returncode:
            return 'changes_requested', f'{BASE_BRANCH}와 시험 병합이 실패했어요 — 운영 코드는 유지했어요.\n' + (r.stdout + r.stderr).strip()[-1500:]
        sha = _git(str(worktree), 'rev-parse', 'HEAD').stdout.strip()
        failed = run_tests(str(worktree))
        if failed:
            return 'changes_requested', f'시험 병합 전체 검사가 실패했어요 — 운영 코드는 유지했어요.\n\n```\n{failed}\n```'
        changed = _git(str(worktree), 'status', '--porcelain', '--untracked-files=no').stdout.strip()
        if _git(str(worktree), 'rev-parse', 'HEAD').stdout.strip() != sha or changed:
            return 'changes_requested', ('검사 중 시험 병합 사본이 변경됐어요 — 검사 결과를 반영하지 않았어요. 검사가 커밋된 파일을 다시 쓰면 .gitignore에 넣거나 임시 폴더에 쓰게 고쳐 주세요.'
                                         + (f'\n\n```\n{changed[:1500]}\n```' if changed else ''))
        if (_git(repo, 'rev-parse', '--abbrev-ref', 'HEAD').stdout.strip() != BASE_BRANCH
                or _git(repo, 'rev-parse', 'HEAD').stdout.strip() != before
                or _git(repo, 'rev-parse', branch).stdout.strip() != task_sha
                or _git(repo, 'status', '--porcelain', '--untracked-files=no').stdout.strip()):
            return 'on_hold', '검사 중 기준 브랜치·Task·작업 폴더가 바뀌었어요 — 최신 상태로 다시 시험 병합해 주세요.'
        if checkpoint:
            checkpoint(sha)
        r = _git(repo, 'merge', '--ff-only', sha)
        if r.returncode:
            return 'on_hold', '검사한 커밋 반영에 실패했어요: ' + (r.stdout + r.stderr).strip()[-800:]
        return 'merged', sha
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return 'on_hold', '시험 병합 처리에 실패했어요: ' + str(error)
    finally:
        assert worktree.is_relative_to(folder.resolve())
        try:
            if added:
                result = _git(repo, 'worktree', 'remove', '--force', str(worktree))
                if result.returncode:
                    print(f'시험 병합 사본 정리 실패: {worktree}: {result.stderr.strip()}')
            elif worktree.exists():
                worktree.rmdir()
        except OSError as error:
            print(f'시험 병합 사본 정리 실패: {worktree}: {error}')


def completion(ref):
    """최신 실행의 후처리만 반환한다."""
    with db.connect() as c:
        r = c.execute("SELECT e.* FROM execution_completion e WHERE ref=? AND run_id=(SELECT MAX(id) FROM runs WHERE issue_id=e.issue_id)", (ref,)).fetchone()
        return dict(r) if r else None


def pending():
    with db.connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM execution_completion WHERE phase IN (%s) ORDER BY run_id" % ','.join('?' for _ in ACTIVE_PHASES), ACTIVE_PHASES)]


def _owned(c, record):
    worker = c.execute('SELECT worker_id FROM execution_completion WHERE run_id=?', (record['run_id'],)).fetchone()
    if not worker or worker[0] != PROCESS_ID:
        return None
    row = c.execute('SELECT * FROM issues WHERE id=?', (record['issue_id'],)).fetchone()
    latest = c.execute('SELECT MAX(id) FROM runs WHERE issue_id=?', (record['issue_id'],)).fetchone()[0]
    event = c.execute("SELECT MAX(id) FROM events WHERE issue_id=? AND kind='status'", (record['issue_id'],)).fetchone()[0]
    return row if row and row['status'] == 'in_progress' and latest == record['run_id'] and event == record['owner_event'] and 'goal' not in json.loads(row['labels_json']) else None


def _save(record, phase, **values):
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if not _owned(c, record):
            c.execute("UPDATE execution_completion SET phase='abandoned',updated_at=? WHERE run_id=? AND worker_id=?", (db.now_iso(), record['run_id'], PROCESS_ID))
            raise issues.StoreError('후처리 소유권이 바뀌었어요 — 사람의 상태를 유지해요.', 409)
        c.execute('UPDATE execution_completion SET phase=?,updated_at=?' + ''.join(',%s=?' % k for k in values) + ' WHERE run_id=?', (phase, db.now_iso(), *values.values(), record['run_id']))
    record.update(phase=phase, **values)


def _finish(actor, record, status, note):
    """최종 상태와 완료 기록을 같은 트랜잭션에 한 번만 저장한다."""
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        current = c.execute('SELECT phase FROM execution_completion WHERE run_id=?', (record['run_id'],)).fetchone()
        if not current or current[0] not in ACTIVE_PHASES:
            return None
        row = _owned(c, record)
        if not row:
            c.execute("UPDATE execution_completion SET phase='abandoned' WHERE run_id=? AND worker_id=?", (record['run_id'], PROCESS_ID))
            return None
        phase = 'complete' if status == 'merged' else 'held' if status == 'on_hold' else 'failed'
        issues._set_status(c, actor, row, 'in_review' if status == 'merged' else status,
                           record['note'] + '\n\n🔁 ' + note if status == 'merged' else '🔀 자동 병합: ' + note)
        c.execute('UPDATE execution_completion SET phase=?,updated_at=? WHERE run_id=?', (phase, db.now_iso(), record['run_id']))
        c.execute("UPDATE task_execution_results SET state=?,commit_event_id=(SELECT MAX(id) FROM events WHERE issue_id=? AND kind='commit'),status_event_id=(SELECT MAX(id) FROM events WHERE issue_id=? AND kind='status') WHERE run_id=?", ('merged' if status == 'merged' else 'blocked',row['id'],row['id'],record['run_id']))
    if status == 'merged':
        _publish(actor, record)
    return status


def _publish(actor, record):
    with db.connect() as c:
        send = c.execute("UPDATE execution_completion SET notified=1 WHERE run_id=? AND phase='complete' AND notified=0", (record['run_id'],)).rowcount
    if send:
        import notify
        notify.send(record['ref'], '✅ 운영 반영 완료 — 확인해 주세요')
    promote_parent(actor, record['ref'])


def _verified_health(cfg, record):
    """자기 재시작은 새 프로세스와 기동 시점의 반영 SHA를 함께 확인한다."""
    if cfg.get('pm2_app') != 'dev':
        return _healthy(cfg)
    if not cfg.get('health_url'):
        return False
    deadline = time.monotonic() + cfg['health_seconds']
    while True:
        url = cfg['health_url'] + ('&' if '?' in cfg['health_url'] else '?') + 'execution_identity=true'
        code, body = _get(url)
        try:
            data = json.loads(body)
            if code == 200 and data.get('process_id') and data['process_id'] != record['process_id'] and data.get('revision') == record['merge_sha']:
                return True
        except (ValueError, TypeError):
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)


def _post_deploy(actor, repo, cfg, record):
    sha = record['merge_sha']
    files = _git(repo, 'diff', '--name-only', f'{sha}^1', sha).stdout.split()
    hits = [f for f in files if any(fnmatch.fnmatch(f, p) for p in cfg['restart_when'])]
    if record['phase'] == 'deployed':
        if not hits or not cfg.get('pm2_app'):
            return _finish(actor, record, 'merged', '재시작이 필요 없는 변경이라 재시작하지 않았어요.')
        deadline = time.monotonic() + cfg['wait_minutes'] * 60
        while _busy(cfg):
            if time.monotonic() >= deadline:
                return _finish(actor, record, 'on_hold', '작업 종료 대기 시간 제한을 넘었어요. 병합은 반영됐으며 재시작 확인이 필요해요.')
            time.sleep(cfg['poll_seconds'])
        _save(record, 'restart_requested', process_id=PROCESS_ID)
        err = _restart(repo, cfg)
        if err:
            _save(record, 'rollback_requested')
    if record['phase'] == 'restart_requested':
        if _verified_health(cfg, record):
            return _finish(actor, record, 'merged', '재시작과 반영 health 확인을 통과했어요.')
        _save(record, 'rollback_requested')
    if record['phase'] == 'rollback_requested':
        # revert 직후 종료됐으면 이미 생긴 복구 커밋을 다시 만들지 않는다.
        head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
        message = _git(repo, 'log', '-1', '--format=%B').stdout
        if head != sha and f'This reverts commit {sha}' not in message:
            return _finish(actor, record, 'on_hold', '복구 전에 운영 HEAD가 바뀌었어요 — 직접 확인해 주세요.')
        if head == sha:
            r = _git(repo, 'revert', '-m', '1', '--no-edit', sha)
            if r.returncode:
                return _finish(actor, record, 'changes_requested', '재시작/health 실패 후 revert도 실패했어요: ' + r.stderr.strip())
        back = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
        _save(record, 'rollback_applied', merge_sha=back, process_id=PROCESS_ID)
        err = _restart(repo, cfg)
        if err:
            return _finish(actor, record, 'changes_requested', '병합을 revert했지만 복구 재시작 명령이 실패했어요: ' + err)
    if record['phase'] == 'rollback_applied':
        healthy = _verified_health(cfg, record)
        return _finish(actor, record, 'changes_requested', '재시작/health 실패로 병합을 revert했어요. ' + ('복구 health를 확인했어요.' if healthy else '복구 health 확인도 실패했어요 — 직접 확인해 주세요.'))


def handle(actor: dict, ref: str) -> str | None:
    """검증된 구현 완료부터 반영 확인까지 진행하고 중단된 후처리를 복구한다."""
    with _lock:
        record = completion(ref)
        if not record or not record['auto_merge'] or record['phase'] not in ACTIVE_PHASES:
            return None
        cfg = {**DEFAULTS, **json.loads(record['cfg_json'])}
        issue = issues.get_issue(ref)
        repo = next((p['local_path'] for p in issues.list_projects() if p['key'] == issue['project_key']), '')
        try:
            with db.connect() as c:
                claimed = c.execute("UPDATE execution_completion SET worker_id=? WHERE run_id=? AND worker_id IN ('',?)", (PROCESS_ID, record['run_id'], PROCESS_ID)).rowcount
            if not claimed:
                return None
            _save(record, record['phase'])
            if record['phase'] in ('ready', 'checking'):
                _save(record, 'checking')
                def checkpoint(sha):
                    _save(record, 'applying', merge_sha=sha)
                status, note = _merge(repo, ref, [b['ref'] for b in issue['blocked_by']], checkpoint, record['task_sha'])
                if status != 'merged':
                    return _finish(actor, record, status, note)
                _save(record, 'deployed', merge_sha=note)
            elif record['phase'] == 'applying':
                head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
                if head == record['merge_sha']:
                    _save(record, 'deployed')
                else:
                    return _finish(actor, record, 'on_hold', '반영 경계에서 중단됐어요. 운영 HEAD와 검사한 병합 SHA를 확인한 뒤 재개해 주세요.')
            head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
            if head != record['merge_sha'] and record['phase'] != 'rollback_requested':
                return _finish(actor, record, 'on_hold', '후처리 중 운영 HEAD가 바뀌었어요 — 반영/롤백 상태를 확인해 주세요.')
            return _post_deploy(actor, repo, cfg, record)
        except Exception as error:
            return _finish(actor, record, 'on_hold', '후처리 오류: ' + str(error))


def take_over():
    """새 서버의 기동에서만 이전 프로세스의 후처리 소유권을 회수한다."""
    with db.connect() as c:
        c.execute("UPDATE execution_completion SET worker_id=? WHERE phase IN (%s)" % ','.join('?' for _ in ACTIVE_PHASES), (PROCESS_ID, *ACTIVE_PHASES))


def recover():
    """대기열 착수보다 먼저 영속 후처리를 이어받는다."""
    for record in pending():
        # 새 실행에 밀린 기록은 handle()의 최신 실행 조회에서 빠지므로 직접 종료한다.
        with db.connect() as c:
            stale = c.execute("UPDATE execution_completion SET phase='abandoned',updated_at=? WHERE run_id=? AND run_id<>(SELECT MAX(id) FROM runs WHERE issue_id=?)",
                              (db.now_iso(), record['run_id'], record['issue_id'])).rowcount
        if stale:
            continue
        handle({'kind': 'human', 'name': record['actor'].split(':', 1)[1]}, record['ref'])
    # 최종 전환 직후 종료돼도 부모 승격을 놓치지 않는다.
    with db.connect() as c:
        finished = [dict(r) for r in c.execute("SELECT * FROM execution_completion WHERE phase='complete' AND auto_merge=1")]
    for record in finished:
        _publish({'kind': 'human', 'name': record['actor'].split(':', 1)[1]}, record)


def promote_parent(actor: dict, ref: str) -> bool:
    """Task가 병합된 뒤(DEV-40-3) — 형제 Task가 전부 base에 들어갔으면(사람이 done/closed로 끝낸 것도) 상위 이슈를 in_review로
    올리고 무엇이 반영됐고 어디서 확인하는지 한 번에 요약한다. 올렸으면 True."""
    parent_ref = issues.get_issue(ref)["parent_ref"]
    if not parent_ref:
        return False
    parent = issues.get_issue(parent_ref)
    if parent["status"] in ("in_progress", "waiting", "on_hold", "changes_requested", "in_review", "done", "closed") or "goal" in parent["labels"]:   # goal은 사용자만 닫는다
        return False
    repo = next((p["local_path"] for p in issues.list_projects() if p["key"] == parent["project_key"]), "")
    lines, checks = [], []
    for ch in parent["children"]:
        finished = ch["status"] in ("done", "closed")
        if not finished and not (ch["status"] == "in_review" and _merged(repo, ch["ref"])):
            return False
        record = completion(ch['ref'])
        if not finished and record and (not record['auto_merge'] or record['phase'] != 'complete'):
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
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        current = issues._find(c, parent_ref)
        last_event = c.execute('SELECT MAX(id) FROM events WHERE issue_id=?', (current['id'],)).fetchone()[0]
        if current['status'] != parent['status'] or last_event != max((e['id'] for e in parent['events']), default=None):
            return False
        if 'goal' in json.loads(current['labels_json']):
            return False
        for child in parent['children']:
            row = c.execute('SELECT status FROM issues WHERE id=?', (child['id'],)).fetchone()
            if not row or row[0] != child['status']:
                return False
        issues._set_status(c, actor, current, 'in_review', note)
    return True


def merge_state(issue: dict, cfg: dict | None = None) -> str | None:
    """Task 줄의 상태 문구(DEV-40-3) — 오케스트레이터가 타임라인에 남긴 마지막 흔적으로 판단한다. 해당 없으면 None.
    cfg를 주면 settings()를 다시 읽지 않는다(Task 여러 개를 한 번에 볼 때)."""
    record = completion(issue['ref']) if issue.get('ref') else None
    if record and record['auto_merge']:
        labels = {'ready': '병합 검사 중', 'checking': '병합 검사 중', 'applying': '운영 반영 중', 'deployed': '운영 반영 중', 'restart_requested': '재시작 확인 중', 'rollback_requested': '복구 중', 'rollback_applied': '복구 확인 중', 'complete': '병합됨'}
        if record['phase'] in labels:
            return labels[record['phase']]
    for e in reversed(issue.get("events", [])):
        b = e["body"] or ""
        if e['kind'] == 'comment' and b.startswith('↩'):
            return '롤백 확인 필요' if ('닫지' in b or '재시작 확인 전' in b) else '되돌림'
        if e["kind"] == "comment" and b.startswith("🛠"):   # 다시 실행을 맡겼으면 이전 병합 흔적은 지난 일
            break
        if e["kind"] == "comment" and b.startswith("⏳"):
            return "재시작 대기"
        if e["kind"] == "comment" and b.startswith(("🔀", "🔁")):
            return "병합됨"
        if e["kind"] == "status" and b.startswith("🔀 자동 병합"):
            return "되돌림" if "revert" in b else "병합 대기" if e["data"].get("to") == "on_hold" else None
    if issue.get("status") == "in_review" and issue.get("parent_ref") and (cfg or settings(issue["project_key"])).get("auto_merge"):
        return "병합 대기"
    return None
