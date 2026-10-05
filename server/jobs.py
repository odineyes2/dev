"""
Claude 맡기기 대기열(DEV-43) — 검토·실행을 여러 개 눌러 두면 하나씩 차례로 돌린다.
동시에 하나만 도는 규칙(비용·저장소 깨끗함)은 그대로 두고, 바쁠 때 거절하는 대신 `jobs` 테이블에 줄을 세운다.

- enqueue: 사람만. 같은 이슈·같은 mode가 이미 줄에 있거나 도는 중이면 새로 넣지 않는다. 실행은 기다려도 풀리지 않는
  조건(Task 아님·계획서 미승인 등)만 거절하고, 선행 Task 미완료·저장소가 깨끗하지 않음은 줄에서 기다린다.
- pump: 도는 것이 없으면 queued를 대기열 순서(sort_key — 기본은 넣은 순, 사람이 move로 바꿈)로 훑어 지금 시작할 수 있는 첫 항목을 기존 review.start/execute.start로 돌린다.
  막힌 실행 항목은 건너뛰고(note에 이유), 영영 못 도는 항목은 skipped + 댓글.
- 펌프 시점: 넣을 때, 각 실행 스레드가 끝난 뒤(실행은 병합까지), 서버가 뜰 때, 그리고 60초 주기(선행 done 등을 놓치지 않게).
"""
import json
import sqlite3
import threading
import time

import db
import issues

PERIOD_SEC = 60
_lock = threading.Lock()
_threads = 0   # 맡긴 작업 스레드 수 — 실행은 runs가 끝난 뒤 병합까지 이 수로 "도는 중"을 본다


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


def job_for(issue_id: int) -> dict | None:
    """화면용 — 이 이슈의 대기 항목(순번 포함)."""
    return next((j for j in list_jobs() if j["issue_id"] == issue_id), None)


def busy() -> bool:
    import review
    import orchestrate
    return _threads > 0 or review.running_ref() is not None or bool(orchestrate.pending())


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
            c.execute("INSERT INTO jobs(issue_id, mode, actor, status, created_at, provider) VALUES(?, ?, ?, 'queued', ?, ?)",
                      (issue["id"], mode, issues.actor_label(actor), db.now_iso(), provider))
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


def pump() -> None:
    """도는 것이 없으면 시작할 수 있는 첫 항목을 돌린다."""
    import execute, review
    with _lock:
        import automation
        import orchestrate
        if _threads == 0:
            orchestrate.recover()
        automation.sync()
        reconcile()
        if busy():
            pending = orchestrate.pending()
            if pending:
                with db.connect() as c:
                    c.execute("UPDATE jobs SET note=? WHERE status='queued'", (pending[0]['ref'] + '의 병합·운영 반영 완료를 기다려요.',))
            return
        with db.connect() as c:
            queued = _rows(c)
        for j in queued:
            actor = _actor(j["actor"])
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
                    review.start(actor, j["ref"], j["provider"])
                else:
                    issue = issues.get_issue(j["ref"])
                    parent = issues.get_issue(issue["parent_ref"]) if issue["parent_ref"] else None
                    never = execute.blocked_reason(issue, parent, wait=False)
                    if never:
                        _set(j["id"], "skipped", never)
                        issues.add_comment(actor, j["ref"], f"⏭ 대기열의 실행을 건너뛰었어요 — {never}")
                        continue
                    why = execute.blocked_reason(issue, parent)
                    if why:   # 선행 대기 — 뒤 항목을 먼저 본다
                        _set(j["id"], "queued", why)
                        continue
                    execute.start(actor, j["ref"], j["provider"])
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
            return


def run_then_pump(fn, *args) -> None:
    """작업 스레드 — 끝나면(실행은 병합까지) 다음 항목을 꺼낸다."""
    global _threads
    with _lock:
        _threads += 1
    try:
        fn(*args)
    finally:
        with _lock:
            _threads -= 1
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
