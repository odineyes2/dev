"""
Claude 맡기기 대기열(DEV-43) — 검토·실행을 여러 개 눌러 두면 차례로 돌린다.
줄은 (프로젝트, 검토|실행)마다 하나이고 각 줄은 한 번에 하나만 돈다. 실행 줄은 병합·운영 반영이 끝날 때까지 바쁘다
(프로젝트 안의 실행 순서는 예전 한 줄 대기열 그대로). 전체 규칙 두 가지:
동시에 바쁜 줄 수는 설정 상한(app_settings.queue_concurrency, 기본 2)까지, dev 자신을 재시작할 병합이 진행 중이면 어느 줄도 새로 시작하지 않는다.

- enqueue: 사람만. 같은 이슈·같은 mode가 이미 줄에 있거나 도는 중이면 새로 넣지 않는다. 실행은 기다려도 풀리지 않는
  조건(Task 아님·계획서 미승인 등)만 거절하고, 선행 Task 미완료·저장소가 깨끗하지 않음은 줄에서 기다린다.
- pump: queued를 대기열 순서(sort_key — 기본은 넣은 순, 사람이 move로 바꿈)로 훑어 줄이 비고 상한이 남은 항목을 기존 review.start/execute.start로 돌린다.
  막힌 항목은 건너뛰고(note에 그 줄의 이유), 영영 못 도는 항목은 skipped + 댓글.
- 펌프 시점: 넣을 때, 각 실행 스레드가 끝난 뒤(실행은 병합까지), 서버가 뜰 때, 그리고 60초 주기(선행 done 등을 놓치지 않게).
"""
import json
import os
import sqlite3
import threading
import time

import db
import issues

PERIOD_SEC = 60
DEFAULT_CONCURRENCY, MAX_CONCURRENCY = 2, 6
SELF_PM2_APP = os.environ.get("DEV_PM2_APP") or "dev"   # 이 앱을 재시작하는 병합은 모든 줄의 작업을 끊는다
_lock = threading.Lock()
_threads: dict[tuple, int] = {}   # 줄별 맡긴 작업 스레드 수 — 실행 줄은 runs가 끝난 뒤 병합까지 이 수로 "도는 중"을 본다


def _actor(label: str) -> dict:
    return {"kind": "human", "name": label.split(":", 1)[1]}


def _rows(c, where: str = "j.status='queued'", args=()) -> list[dict]:
    return [dict(r) for r in c.execute(
        f"SELECT j.*, {issues.ref_sql('i', 'p')} AS ref FROM jobs j JOIN issues i ON i.id=j.issue_id JOIN projects p ON p.id=i.project_id "
        f"WHERE {where} ORDER BY j.sort_key, j.id", args)]


def list_jobs() -> list[dict]:
    """대기 중인 항목, 사람이 정한 순서(기본은 넣은 순서). position은 1부터, prev_id/next_id는 이동 버튼용 이웃."""
    with db.connect() as c:
        rows = _rows(c)
    ids = [None] + [j["id"] for j in rows] + [None]
    return [{**j, "position": n, "prev_id": ids[n - 1], "next_id": ids[n + 1]} for n, j in enumerate(rows, 1)]


def list_in_progress() -> list[dict]:
    """게시판 대기열 상자 위쪽(DEV-91) — in_progress 이슈 전부(프로젝트 무관), 도는 실행이 있으면 mode·provider·started_at."""
    with db.connect() as c:
        return [dict(r) for r in c.execute(
            f"SELECT {issues.ref_sql('i', 'p')} AS ref, i.title, i.claimed_by, i.updated_at, r.mode, r.provider, r.started_at "
            "FROM issues i JOIN projects p ON p.id=i.project_id "
            "LEFT JOIN runs r ON r.id=(SELECT MAX(id) FROM runs WHERE issue_id=i.id AND status='running') "
            "WHERE i.status='in_progress' ORDER BY i.updated_at, i.id")]


def job_for(issue_id: int) -> dict | None:
    """화면용 — 이 이슈의 대기 항목(순번 포함)."""
    return next((j for j in list_jobs() if j["issue_id"] == issue_id), None)


def concurrency(c=None) -> int:
    """동시에 바쁠 수 있는 줄 수(Settings에서 바꾼다)."""
    if c is None:
        with db.connect() as c:
            return concurrency(c)
    row = c.execute("SELECT value FROM app_settings WHERE key='queue_concurrency'").fetchone()
    return int(row["value"]) if row else DEFAULT_CONCURRENCY


def set_concurrency(actor: dict, value) -> dict:
    if actor["kind"] != "human":
        raise issues.StoreError("대기열 설정은 사람만 바꿀 수 있어요.", 403)
    if type(value) is not int or not 1 <= value <= MAX_CONCURRENCY:
        raise issues.StoreError(f"동시 실행 상한은 1~{MAX_CONCURRENCY} 사이의 정수예요.", 400)
    with db.connect() as c:
        c.execute("INSERT INTO app_settings(key,value,updated_at,updated_by) VALUES('queue_concurrency',?,?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                  (str(value), db.now_iso(), issues.actor_label(actor)))
    pump()   # 늘렸으면 기다리던 줄을 바로 채운다
    return {"queue_concurrency": value, "max": MAX_CONCURRENCY}


def lane_of_run(run_id: int) -> tuple | None:
    with db.connect() as c:
        r = c.execute("SELECT p.key, r.mode FROM runs r JOIN issues i ON i.id=r.issue_id JOIN projects p ON p.id=i.project_id WHERE r.id=?", (run_id,)).fetchone()
    return (r["key"], r["mode"]) if r else None


def _pending_merges(c) -> list[dict]:
    import orchestrate
    return [dict(r) for r in c.execute("SELECT * FROM execution_completion WHERE phase IN (%s) ORDER BY run_id" % ','.join('?' * len(orchestrate.ACTIVE_PHASES)), orchestrate.ACTIVE_PHASES)]


def _active_lanes(c) -> set[tuple]:
    """지금 바쁜 줄 — 도는 runs, 병합·반영 중인 실행, 아직 끝나지 않은 작업 스레드."""
    lanes = {(r["key"], r["mode"]) for r in c.execute(
        "SELECT p.key, r.mode FROM runs r JOIN issues i ON i.id=r.issue_id JOIN projects p ON p.id=i.project_id WHERE r.status='running'")}
    lanes |= {(m["ref"].split("-")[0], "execute") for m in _pending_merges(c)}
    return lanes | {lane for lane, n in _threads.items() if n and lane}


def busy() -> bool:
    """바쁜 줄이 하나라도 있는지."""
    with db.connect() as c:
        return bool(_active_lanes(c))


def start_block(c, project_key: str, mode: str) -> str | None:
    """이 줄에서 지금 새로 시작하면 안 되는 이유. 없으면 None."""
    import orchestrate
    for m in _pending_merges(c):
        if orchestrate.settings(m["ref"].split("-")[0]).get("pm2_app") == SELF_PM2_APP:
            return f"{m['ref']} 병합 뒤 dev 재시작을 기다려요 — 재시작하면 도는 작업이 모두 끊겨서 그동안은 새로 시작하지 않아요."
    lanes = _active_lanes(c)
    if (project_key, mode) in lanes:
        merging = next((m["ref"] for m in _pending_merges(c) if mode == "execute" and m["ref"].split("-")[0] == project_key), None)
        if merging:
            return f"{merging}의 병합·운영 반영이 끝나면 시작해요."
        return f"{project_key} {'실행' if mode == 'execute' else '검토'} 줄의 앞 작업이 끝나면 시작해요."
    limit = concurrency(c)
    if len(lanes) >= limit:
        return f"동시 실행 상한({limit}개)이 차서 기다려요 — 도는 작업이 끝나면 시작해요."
    return None


def _latest(c, issue_id):
    event = c.execute("SELECT data_json FROM events WHERE issue_id=? AND kind='status' ORDER BY id DESC LIMIT 1", (issue_id,)).fetchone()
    return json.loads(event[0]) if event else {}


def _waiting(c, j):
    row = c.execute("SELECT * FROM issues WHERE id=?", (j["issue_id"],)).fetchone()
    if c.execute("SELECT 1 FROM execution_completion WHERE issue_id=? AND phase IN ('ready','checking','applying','deployed','restart_requested','rollback_requested','rollback_applied')", (j['issue_id'],)).fetchone():
        return
    if j["previous_status"] is not None or row["status"] in ("done", "closed") or "goal" in json.loads(row["labels_json"]):
        return
    prior = row["status"]
    if prior == "waiting":
        other = c.execute("SELECT previous_status FROM jobs WHERE issue_id=? AND status='queued' AND previous_status IS NOT NULL ORDER BY id LIMIT 1", (row["id"],)).fetchone()
        prior = other[0] if other else "backlog"
    c.execute("UPDATE jobs SET previous_status=? WHERE id=?", (prior, j["id"]))
    issues._set_status(c, _actor(j["actor"]), row, "waiting", "작업 대기열에 넣었어요", {"job_id": j["id"]})


def _restore(c, j):
    row = c.execute("SELECT * FROM issues WHERE id=?", (j["issue_id"],)).fetchone()
    if not row or row["status"] != "waiting" or _latest(c, row["id"]).get("job_id") != j["id"]:
        return
    other = c.execute("SELECT * FROM jobs WHERE issue_id=? AND status='queued' AND id<>? ORDER BY id LIMIT 1", (row["id"], j["id"])).fetchone()
    if other:
        c.execute("UPDATE jobs SET previous_status=? WHERE id=?", (j["previous_status"], other["id"]))
        issues._set_status(c, _actor(other["actor"]), row, "waiting", "다른 작업이 대기 중이에요", {"job_id": other["id"]})
    else:
        issues._set_status(c, _actor(j["actor"]), row, j["previous_status"] or "backlog", "대기 전 상태로 복구했어요")


def reconcile():
    """이전 대기 항목과 재시작으로 끊긴 착수 상태를 정합화한다."""
    import review
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        for r in c.execute("SELECT r.* FROM runs r JOIN issues i ON i.id=r.issue_id WHERE r.status='orphaned' AND i.status='in_progress'").fetchall():
            if c.execute('SELECT 1 FROM execution_completion WHERE run_id=?', (r['id'],)).fetchone():
                continue
            row = c.execute("SELECT * FROM issues WHERE id=?", (r["issue_id"],)).fetchone()
            start = review.owned_start(c, r["issue_id"], r["id"])
            if row and start:
                issues._set_status(c, _actor(r["actor"]), row, start.get("restore_status", start["from"]), "재시작으로 끊긴 작업을 복구했어요")
                # 복구한 상태를 남은 대기의 원래 상태로 보존하고 소유권을 넘긴다.
                c.execute("UPDATE jobs SET previous_status=NULL WHERE issue_id=? AND status='queued'", (r["issue_id"],))
        for j in _rows(c):
            if c.execute("SELECT 1 FROM execution_completion WHERE issue_id=? AND phase IN ('ready','checking','applying','deployed','restart_requested','rollback_requested','rollback_applied')", (j['issue_id'],)).fetchone():
                continue
            _waiting(c, j)


def enqueue(actor: dict, ref: str, mode: str, provider: str | None = None) -> dict:
    """줄에 넣고 펌프를 돌린다. 바로 시작하면 {started}, 아니면 {queued, position}."""
    import auto_settings
    import execute
    import project_docs
    if actor["kind"] != "human":
        raise issues.StoreError("검토·실행은 사람만 맡길 수 있어요.", 403)
    issue = issues.get_issue(ref)
    provider = project_docs.resolve_provider(issue['id'], provider)
    if provider not in ("claude", "codex"):
        raise issues.StoreError("지원하지 않는 검토 도구예요.", 400)
    if mode not in ("review", "execute"):
        raise issues.StoreError("지원하지 않는 작업이에요.", 400)
    issue = issues.get_issue(ref)   # 없으면 404
    ref = issue["ref"]
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        issue = issues.get_issue(ref)
        existing = c.execute("SELECT provider FROM runs WHERE issue_id=? AND mode=? AND status='running' UNION ALL SELECT provider FROM jobs WHERE issue_id=? AND mode=? AND status='queued' LIMIT 1",
                             (issue["id"], mode, issue["id"], mode)).fetchone()
        if existing and existing["provider"] != provider:
            raise issues.StoreError("이 이슈는 다른 검토 도구로 이미 맡겼어요 — 대기 항목을 취소하거나 끝난 뒤 다시 맡겨 주세요.", 409)
        if c.execute("SELECT 1 FROM runs WHERE issue_id=? AND mode=? AND status='running'", (issue["id"], mode)).fetchone():
            return {"started": True, "ref": ref}   # 이미 도는 중 — 중복 클릭
        if not c.execute("SELECT 1 FROM jobs WHERE issue_id=? AND mode=? AND status='queued'", (issue["id"], mode)).fetchone():
            if mode == "execute":
                why = execute.blocked_reason(issue, issues.get_issue(issue["parent_ref"]) if issue["parent_ref"] else None, wait=False)
                if why:
                    raise issues.StoreError(why, 409)
            c.execute("INSERT INTO jobs(issue_id, mode, actor, status, created_at, provider, model) VALUES(?, ?, ?, 'queued', ?, ?, ?)",
                      (issue["id"], mode, issues.actor_label(actor), db.now_iso(), provider, auto_settings.model_for(c, issue["id"], provider)))
        j = c.execute("SELECT * FROM jobs WHERE issue_id=? AND mode=? AND status='queued'", (issue["id"], mode)).fetchone()
        _waiting(c, j)
    pump()
    job = next((j for j in list_jobs() if j["issue_id"] == issue["id"] and j["mode"] == mode), None)
    if job is None:
        with db.connect() as c:
            ended = c.execute("SELECT status,note FROM jobs WHERE id=?", (j["id"],)).fetchone()
        if ended and ended["status"] == "skipped":
            raise issues.StoreError(ended["note"], 409)
        return {"started": True, "ref": ref}
    return {"queued": True, "ref": ref, "job_id": job["id"], "position": job["position"], "note": job["note"]}


def cancel(actor: dict, job_id: int) -> dict:
    """queued인 것만 취소한다(사람만)."""
    if actor["kind"] != "human":
        raise issues.StoreError("대기열은 사람만 고칠 수 있어요.", 403)
    with _lock, db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        j = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not c.execute("UPDATE jobs SET status='cancelled',cancellation_reason='human_cancel' WHERE id=? AND status='queued'", (job_id,)).rowcount:
            raise issues.StoreError("대기 중인 항목이 아니에요.", 404)
        _restore(c, j)
    return {"cancelled": job_id}


def move(actor: dict, job_id: int, direction: str, neighbor_id: int | None) -> dict:
    """queued 항목을 이웃과 한 칸 바꾼다(사람만). neighbor_id는 화면에서 본 이웃 — 그 사이 바뀌었으면 409.
    끝에서 더 미는 요청(이웃 없음)은 순서를 바꾸지 않는다."""
    if actor["kind"] != "human":
        raise issues.StoreError("대기열은 사람만 고칠 수 있어요.", 403)
    if direction not in ("up", "down"):
        raise issues.StoreError("방향은 up 또는 down이에요.", 400)
    if neighbor_id is not None and type(neighbor_id) is not int:
        raise issues.StoreError("이웃 항목 번호가 올바르지 않아요.", 400)
    with _lock, db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        rows = c.execute("SELECT id, sort_key FROM jobs WHERE status='queued' ORDER BY sort_key, id").fetchall()
        at = next((n for n, r in enumerate(rows) if r["id"] == job_id), None)
        if at is None:
            raise issues.StoreError("대기 중인 항목이 아니에요.", 404)
        n = at - 1 if direction == "up" else at + 1
        other = rows[n] if 0 <= n < len(rows) else None
        if (other["id"] if other else None) != neighbor_id:
            raise issues.StoreError("대기열이 그 사이 바뀌었어요 — 새로 고친 순서를 보고 다시 옮겨 주세요.", 409)
        if other:
            c.execute("UPDATE jobs SET sort_key=? WHERE id=?", (other["sort_key"], job_id))
            c.execute("UPDATE jobs SET sort_key=? WHERE id=?", (rows[at]["sort_key"], other["id"]))
    return {"moved": bool(other), "jobs": list_jobs()}


def _set(job_id: int, status: str, note: str = "") -> None:
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        j = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not j:  # 시작 준비 중 이슈가 지워지면 대기 항목도 함께 사라진다.
            return
        run = c.execute("SELECT id FROM runs WHERE issue_id=? AND mode=? ORDER BY id DESC LIMIT 1", (j["issue_id"], j["mode"])).fetchone() if status == "started" else None
        c.execute("UPDATE jobs SET status=?, note=?, started_at=?, run_id=? WHERE id=?",
                  (status, note, db.now_iso() if status == "started" else None, run["id"] if run else None, job_id))

        if status == "queued" and j["status"] == "started":
            row = c.execute("SELECT * FROM issues WHERE id=?", (j["issue_id"],)).fetchone()
            if row["status"] == j["previous_status"] and _latest(c, row["id"]).get("job_id") == job_id:
                c.execute("UPDATE jobs SET previous_status=NULL WHERE id=?", (job_id,))
                _waiting(c, c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        if status in ("cancelled", "skipped"):
            _restore(c, j)


STALLED = ("on_hold", "changes_requested")


def _reservations(c, keep_stalled: bool = False) -> dict[int, tuple[str, set[str]]]:
    """진행 중인 부모(Task가 한 번이라도 실행됐고 남은 Task가 있는 부모)가 아직 쓸 파일 — {부모 id: (ref, 파일)}.
    남은 Task 중 사람을 기다리는 것(on_hold·changes_requested)이 있으면 예약을 푼다 — 사람이 없을 때 전체가 멈추지 않게.
    그 부모가 다시 돌 때는 execute.refresh_worktree가 최신 base를 병합한다.
    keep_stalled면 사람을 기다리는 부모도 남은 Task 파일 전부로 넣는다(검토 프롬프트용 — 언젠가 다시 돈다)."""
    import execute
    held = {}
    for p in c.execute(f"""SELECT DISTINCT q.id, {issues.ref_sql('q', 'k')} AS ref FROM runs r JOIN issues i ON i.id=r.issue_id
            JOIN issues q ON q.id=i.parent_id JOIN projects k ON k.id=q.project_id WHERE r.mode='execute'""").fetchall():
        kids = c.execute("SELECT status, body FROM issues WHERE parent_id=? AND status NOT IN ('done','closed')", (p["id"],)).fetchall()
        if not kids or (not keep_stalled and any(k["status"] in STALLED for k in kids)):
            continue
        todo = ("backlog", "waiting", "in_progress") + (STALLED if keep_stalled else ())
        files = set().union(*(execute.task_files({"body": k["body"]}) for k in kids if k["status"] in todo))
        if files:
            held[p["id"]] = (p["ref"], files)
    return held


def _overlap(a: set[str], b: set[str]) -> list[str]:
    """같은 파일이거나 한쪽이 다른 쪽의 폴더."""
    return sorted({x for x in a for y in b if x == y or x.startswith(y.rstrip("/") + "/") or y.startswith(x.rstrip("/") + "/")})


def schedule(c, queued: list[dict]) -> tuple[list[dict], dict[int, str]]:
    """꺼낼 순서와 파일 겹침으로 기다릴 항목의 메모 {job id: 메모}.
    순서: 진행 중인 부모의 실행 → 그 밖의 실행 → 검토(같은 묶음 안은 대기열 순서). 시작한 부모를 먼저 끝내
    다른 이슈의 Task가 사이에 끼어 같은 파일을 바꾸지 않게 한다(DEV-68·70, DEV-81·82). 진행 중인 부모끼리는 막지 않는다."""
    import execute
    held = _reservations(c)
    info = {r["id"]: r for r in c.execute(
        f"SELECT id, parent_id, body FROM issues WHERE id IN ({','.join('?' * len(queued))})", [j["issue_id"] for j in queued])} if queued else {}

    def group(j):
        if j["mode"] != "execute":
            return 2
        return 0 if info[j["issue_id"]]["parent_id"] in held else 1
    ordered = sorted(queued, key=group)
    notes = {}
    for j in ordered:
        if group(j) != 1:
            continue
        mine = execute.task_files({"body": info[j["issue_id"]]["body"]})
        for ref, files in held.values():
            hit = _overlap(mine, files)
            if hit:
                notes[j["id"]] = f"{ref} 작업이 끝날 때까지 기다려요 — 겹치는 파일: " + ", ".join(hit[:5]) + (" 외" if len(hit) > 5 else "")
                break
    return ordered, notes


def _note(job_id: int, note: str) -> None:
    with db.connect() as c:
        c.execute("UPDATE jobs SET note=? WHERE id=? AND status='queued'", (note, job_id))


def pump() -> None:
    """비어 있는 줄마다 시작할 수 있는 첫 항목을 돌린다(전체 상한까지)."""
    import execute, review
    with _lock:
        import automation
        import orchestrate
        if not any(n for lane, n in _threads.items() if lane and lane[1] == "execute"):
            orchestrate.recover()   # 병합을 이어받는 스레드가 없을 때만 — 도는 병합과 겹치지 않게
        automation.sync()
        reconcile()
        with db.connect() as c:
            queued, waits = schedule(c, _rows(c))
        for j in queued:
            actor = _actor(j["actor"])
            with db.connect() as c:
                block = start_block(c, j["ref"].split("-")[0], j["mode"])
            if block:   # 그 줄의 진짜 이유만 적는다 — 다른 줄 항목은 계속 본다. 실행은 자기 사정(선행·겹치는 파일)이 있으면 그것이 더 쓸모 있다.
                own = None
                if j["mode"] == "execute":
                    try:
                        issue = issues.get_issue(j["ref"])
                        own = waits.get(j["id"]) or execute.blocked_reason(issue, issues.get_issue(issue["parent_ref"]) if issue["parent_ref"] else None)
                    except issues.StoreError:
                        pass
                _note(j["id"], own or block)
                continue
            try:
                if j['source'] == 'auto':
                    with db.connect() as c:
                        c.execute('BEGIN IMMEDIATE')
                        valid = automation.valid_job(c, j)
                        if not valid:
                            automation.cancel_invalid(c, j)
                    if not valid:
                        continue
                with db.connect() as c:
                    row = c.execute("SELECT * FROM issues WHERE id=?", (j["issue_id"],)).fetchone()
                    owner = _latest(c, j["issue_id"]).get("job_id")
                    owned = c.execute("SELECT 1 FROM jobs WHERE id=? AND issue_id=? AND status='queued'", (owner, j["issue_id"])).fetchone()
                if not row or row["status"] != "waiting" or not owned:
                    _set(j["id"], "skipped", "상태가 바뀌었거나 보호된 이슈여서 대기를 종료했어요")
                    continue
                if j["mode"] == "review":
                    review.start(actor, j["ref"], j["provider"], j["model"])
                else:
                    issue = issues.get_issue(j["ref"])
                    parent = issues.get_issue(issue["parent_ref"]) if issue["parent_ref"] else None
                    never = execute.blocked_reason(issue, parent, wait=False)
                    if never:
                        _set(j["id"], "skipped", never)
                        issues.add_comment(actor, j["ref"], f"⏭ 대기열의 실행을 건너뛰었어요 — {never}")
                        continue
                    why = waits.get(j["id"]) or execute.blocked_reason(issue, parent)
                    if why:   # 선행·겹치는 파일 대기 — 뒤 항목을 먼저 본다
                        _set(j["id"], "queued", why)
                        continue
                    execute.start(actor, j["ref"], j["provider"], j["model"])
            except sqlite3.IntegrityError as e:
                if j['source'] != 'auto':
                    raise
                with db.connect() as c:
                    c.execute('BEGIN IMMEDIATE')
                    automation.cancel_invalid(c, j, start_error=str(e) != '자동 위임 조건이 바뀌었어요')
                continue
            except issues.StoreError as e:
                if j['source'] == 'auto' or e.status == 404:
                    _set(j["id"], "skipped", str(e))
                else:   # 저장소가 깨끗하지 않음 등 — 줄에서 기다린다
                    _set(j["id"], "queued", str(e))
                continue
            _set(j["id"], "started")


def run_then_pump(fn, *args, lane: tuple | None = None) -> None:
    """작업 스레드 — 끝나면(실행은 병합까지) 다음 항목을 꺼낸다. lane은 (프로젝트 키, mode)."""
    with _lock:
        _threads[lane] = _threads.get(lane, 0) + 1
    try:
        fn(*args)
    finally:
        with _lock:
            _threads[lane] -= 1
        pump()


def start_timer() -> None:
    """서버가 뜰 때 한 번, 그 뒤 60초마다 펌프."""
    def loop():
        while True:
            try:
                pump()
            except Exception as e:   # 주기 펌프는 죽지 않게
                print("jobs.pump 오류:", e)
            time.sleep(PERIOD_SEC)
    threading.Thread(target=loop, daemon=True).start()
