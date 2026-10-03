"""
Claude 맡기기 대기열(DEV-43) — 검토·실행을 여러 개 눌러 두면 하나씩 차례로 돌린다.
동시에 하나만 도는 규칙(비용·저장소 깨끗함)은 그대로 두고, 바쁠 때 거절하는 대신 `jobs` 테이블에 줄을 세운다.

- enqueue: 사람만. 같은 이슈·같은 mode가 이미 줄에 있거나 도는 중이면 새로 넣지 않는다. 실행은 기다려도 풀리지 않는
  조건(Task 아님·계획서 미승인 등)만 거절하고, 선행 Task 미완료·저장소가 깨끗하지 않음은 줄에서 기다린다.
- pump: 도는 것이 없으면 queued를 오래된 순으로 훑어 지금 시작할 수 있는 첫 항목을 기존 review.start/execute.start로 돌린다.
  막힌 실행 항목은 건너뛰고(note에 이유), 영영 못 도는 항목은 skipped + 댓글.
- 펌프 시점: 넣을 때, 각 실행 스레드가 끝난 뒤(실행은 병합까지), 서버가 뜰 때, 그리고 60초 주기(선행 done 등을 놓치지 않게).
"""
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
        f"WHERE {where} ORDER BY j.id", args)]


def list_jobs() -> list[dict]:
    """대기 중인 항목, 오래된 것이 먼저(position은 1부터)."""
    with db.connect() as c:
        return [{**j, "position": n} for n, j in enumerate(_rows(c), 1)]


def job_for(issue_id: int) -> dict | None:
    """화면용 — 이 이슈의 대기 항목(순번 포함)."""
    return next((j for j in list_jobs() if j["issue_id"] == issue_id), None)


def busy() -> bool:
    import review
    return _threads > 0 or review.running_ref() is not None


def enqueue(actor: dict, ref: str, mode: str, provider: str = "claude") -> dict:
    """줄에 넣고 펌프를 돌린다. 바로 시작하면 {started}, 아니면 {queued, position}."""
    import execute
    if actor["kind"] != "human":
        raise issues.StoreError("검토·실행은 사람만 맡길 수 있어요.", 403)
    if provider not in ("claude", "codex"):
        raise issues.StoreError("지원하지 않는 검토 도구예요.", 400)
    issue = issues.get_issue(ref)   # 없으면 404
    ref = issue["ref"]
    if mode == "execute":
        why = execute.blocked_reason(issue, issues.get_issue(issue["parent_ref"]) if issue["parent_ref"] else None, wait=False)
        if why:
            raise issues.StoreError(why, 409)
    with db.connect() as c:
        existing = c.execute("SELECT provider FROM runs WHERE issue_id=? AND mode=? AND status='running' UNION ALL SELECT provider FROM jobs WHERE issue_id=? AND mode=? AND status='queued' LIMIT 1",
                             (issue["id"], mode, issue["id"], mode)).fetchone()
        if existing and existing["provider"] != provider:
            raise issues.StoreError("이 이슈는 다른 검토 도구로 이미 맡겼어요 — 대기 항목을 취소하거나 끝난 뒤 다시 맡겨 주세요.", 409)
        if c.execute("SELECT 1 FROM runs WHERE issue_id=? AND mode=? AND status='running'", (issue["id"], mode)).fetchone():
            return {"started": True, "ref": ref}   # 이미 도는 중 — 중복 클릭
        if not c.execute("SELECT 1 FROM jobs WHERE issue_id=? AND mode=? AND status='queued'", (issue["id"], mode)).fetchone():
            c.execute("INSERT INTO jobs(issue_id, mode, actor, status, created_at, provider) VALUES(?, ?, ?, 'queued', ?, ?)",
                      (issue["id"], mode, issues.actor_label(actor), db.now_iso(), provider))
    pump()
    job = next((j for j in list_jobs() if j["issue_id"] == issue["id"] and j["mode"] == mode), None)
    if job is None:
        return {"started": True, "ref": ref}
    return {"queued": True, "ref": ref, "job_id": job["id"], "position": job["position"], "note": job["note"]}


def cancel(actor: dict, job_id: int) -> dict:
    """queued인 것만 취소한다(사람만)."""
    if actor["kind"] != "human":
        raise issues.StoreError("대기열은 사람만 고칠 수 있어요.", 403)
    with db.connect() as c:
        if not c.execute("UPDATE jobs SET status='cancelled' WHERE id=? AND status='queued'", (job_id,)).rowcount:
            raise issues.StoreError("대기 중인 항목이 아니에요.", 404)
    return {"cancelled": job_id}


def _set(job_id: int, status: str, note: str = "") -> None:
    with db.connect() as c:
        run = c.execute("SELECT id FROM runs WHERE status='running' ORDER BY id DESC LIMIT 1").fetchone() if status == "started" else None
        c.execute("UPDATE jobs SET status=?, note=?, started_at=?, run_id=? WHERE id=?",
                  (status, note, db.now_iso() if status == "started" else None, run["id"] if run else None, job_id))


def pump() -> None:
    """도는 것이 없으면 시작할 수 있는 첫 항목을 돌린다."""
    import execute, review
    with _lock:
        if busy():
            return
        with db.connect() as c:
            queued = _rows(c)
        for j in queued:
            actor = _actor(j["actor"])
            try:
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
            except issues.StoreError as e:
                if e.status == 404:
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
