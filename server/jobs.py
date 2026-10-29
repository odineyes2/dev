"""
Claude 맡기기 대기열(DEV-43) — 검토·실행을 여러 개 눌러 두면 차례로 돌린다.
줄은 (프로젝트, 검토|실행)마다 하나이고 각 줄은 한 번에 하나만 돈다. 실행 줄은 병합·운영 반영이 끝날 때까지 바쁘다
(프로젝트 안의 실행 순서는 예전 한 줄 대기열 그대로). 전체 규칙 두 가지:
동시에 바쁜 줄 수는 전체 상한(app_settings.queue_concurrency, 기본 2)과 실행·검토 칸별 상한(queue_concurrency_execute·_review, 없으면 무제한)을 함께 지켜서, dev 자신을 재시작할 병합이 진행 중이면 어느 줄도 새로 시작하지 않는다.

- enqueue: 사람만. 같은 이슈·같은 mode가 이미 줄에 있거나 도는 중이면 새로 넣지 않는다. 실행은 기다려도 풀리지 않는
  조건(Task 아님·계획서 미승인 등)만 거절하고, 선행 Task 미완료·저장소가 깨끗하지 않음은 줄에서 기다린다.
- pump: queued를 대기열 순서(sort_key — 기본은 넣은 순, 사람이 move로 바꿈)로 훑어 줄이 비고 상한이 남은 항목을 기존 review.start/execute.start로 돌린다.
  막힌 항목은 건너뛰고(note에 그 줄의 이유), 영영 못 도는 항목은 skipped + 댓글.
- 펌프 시점: 넣을 때, 각 실행 스레드가 끝난 뒤(실행은 병합까지), 서버가 뜰 때, 그리고 60초 주기(선행 done 등을 놓치지 않게).
"""
import json
import os
import re
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


_WAIT_REF = re.compile(r"[A-Z][A-Z0-9]*-\d+(?:-\d+)?")


def wait_short(note: str, mode: str) -> str:
    """저장된 대기 문장을 Status 칸에 쓸 짧은 이름으로 바꾼다."""
    if not note:
        return ""
    ref = _WAIT_REF.search(note)
    if '사람을 기다려요' in note or '작업이 끝날 때까지 기다려요' in note:
        return f"대기: {ref.group()}" if ref else ('실행 대기' if mode == 'execute' else '검토 대기')
    if '병합' in note and ('기다려요' in note or '끝나면' in note):
        if '재시작' in note:
            return '재시작 대기'
        return f"병합 대기: {ref.group()}" if ref else '병합 대기'
    if note.startswith('선행 ') and ref:
        refs = list(dict.fromkeys(_WAIT_REF.findall(note.split('—', 1)[0])))
        return '선행: ' + ', '.join(refs)
    if '롤백·운영 반영' in note:
        return '롤백 대기'
    if '재시작' in note:
        return '재시작 대기'
    if '상한(' in note or '동시 실행 상한' in note:
        return '상한 대기'
    if '서비스 연결' in note:
        return '연결 대기'
    if '초기 생성' in note or '초기 프로젝트 생성' in note:
        return '초기 생성 대기'
    if '목표 대비 점검' in note:
        return '점검 대기'
    if '실패 검사 재현' in note:
        return '재현 대기'
    return '실행 대기' if mode == 'execute' else '검토 대기'


def list_jobs() -> list[dict]:
    """대기 중인 항목, 사람이 정한 순서(기본은 넣은 순서). position은 1부터, prev_id/next_id는 이동 버튼용 이웃."""
    with db.connect() as c:
        rows = _rows(c)
    ids = [None] + [j["id"] for j in rows] + [None]
    return [{**j, "note_short": wait_short(j["note"], j["mode"]),
             "position": n, "prev_id": ids[n - 1], "next_id": ids[n + 1]} for n, j in enumerate(rows, 1)]


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


_UNSET = object()
_LIMIT_KEYS = {"execute": "queue_concurrency_execute", "review": "queue_concurrency_review"}


def concurrency_limits(c=None) -> dict:
    """전체·실행·검토 상한. 실행·검토는 키가 없으면 None(무제한)."""
    if c is None:
        with db.connect() as c:
            return concurrency_limits(c)
    out = {"total": concurrency(c)}
    for kind, key in _LIMIT_KEYS.items():
        row = c.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        out[kind] = int(row["value"]) if row and row["value"] not in (None, "") else None
    return out


def queue_settings() -> dict:
    with db.connect() as c:
        limits = concurrency_limits(c)
        counts = lane_counts(c)
    return {"queue_concurrency": limits["total"], "execute": limits["execute"], "review": limits["review"],
            "max": MAX_CONCURRENCY, "busy": {**counts, "total": counts["execute"] + counts["review"]}}


def set_concurrency(actor: dict, value=_UNSET, *, total=_UNSET, execute=_UNSET, review=_UNSET) -> dict:
    """안 준 항목은 그대로 둔다. 실행·검토에 None을 주면 무제한."""
    if actor["kind"] != "human":
        raise issues.StoreError("대기열 설정은 사람만 바꿀 수 있어요.", 403)
    if value is not _UNSET:
        total = value
    msg = f"동시 실행 상한은 1~{MAX_CONCURRENCY} 사이의 정수예요."
    for name, v in (("total", total), ("execute", execute), ("review", review)):
        if v is _UNSET or (v is None and name != "total"):
            continue
        if type(v) is not int or not 1 <= v <= MAX_CONCURRENCY:
            raise issues.StoreError(msg, 400)
    who, now = issues.actor_label(actor), db.now_iso()
    with db.connect() as c:
        for key, v in (("queue_concurrency", total), (_LIMIT_KEYS["execute"], execute), (_LIMIT_KEYS["review"], review)):
            if v is _UNSET:
                continue
            if v is None:
                c.execute("DELETE FROM app_settings WHERE key=?", (key,))
                continue
            c.execute("INSERT INTO app_settings(key,value,updated_at,updated_by) VALUES(?,?,?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                      (key, str(v), now, who))
    pump()   # 늘렸으면 기다리던 줄을 바로 채운다
    return queue_settings()


def lane_kind(mode: str) -> str:
    """줄의 모드가 세어지는 칸 — 실행(execute·initialization) 아니면 검토(review·audit·qa)."""
    return "execute" if mode in ("execute", "initialization") else "review"


def lane_counts(c, lanes=None) -> dict:
    """지금 바쁜 줄 수를 칸별로."""
    counts = {"execute": 0, "review": 0}
    for lane in (_active_lanes(c) if lanes is None else lanes):
        counts[lane_kind(lane[1])] += 1
    return counts


def lane_of_run(run_id: int) -> tuple | None:
    with db.connect() as c:
        r = c.execute("SELECT p.key, r.mode FROM runs r JOIN issues i ON i.id=r.issue_id JOIN projects p ON p.id=i.project_id WHERE r.id=?", (run_id,)).fetchone()
    return (r["key"], r["mode"]) if r else None


def _pending_merges(c) -> list[dict]:
    import orchestrate
    return [dict(r) for r in c.execute("SELECT * FROM execution_completion WHERE phase IN (%s) ORDER BY run_id" % ','.join('?' * len(orchestrate.ACTIVE_PHASES)), orchestrate.ACTIVE_PHASES)]


def self_restart_state(c) -> dict | None:
    """dev 자신을 재시작할 병합과 기다리는 작업을 읽는다. 고아 정리·외부 요청은 하지 않는다."""
    from datetime import datetime, timezone
    import orchestrate
    import reproduce
    record = next((m for m in _pending_merges(c)
                   if json.loads(m['cfg_json']).get('pm2_app') == SELF_PM2_APP), None)
    if record is None:
        return None
    now = datetime.now(timezone.utc)

    def minutes(since):
        try:
            stamp = datetime.fromisoformat(since)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return max(0, int((now - stamp).total_seconds() // 60))
        except (TypeError, ValueError):
            return 0

    busy = [dict(r) for r in c.execute(
        f"SELECT {issues.ref_sql('i', 'p')} AS ref, r.mode, r.provider, r.started_at "
        "FROM runs r JOIN issues i ON i.id=r.issue_id JOIN projects p ON p.id=i.project_id "
        "WHERE r.status='running' AND r.id<>? ORDER BY r.id", (record['run_id'],))]
    for table, mode, active, stamp in (
            ('project_audits', 'audit', "'preparing','running','cancelling'", 'COALESCE(a.started_at,a.created_at)'),
            ('project_initializations', 'initialization', "'running','cancelling'", 'a.created_at')):
        busy.extend(dict(r) for r in c.execute(
            f"SELECT p.key AS ref, '{mode}' AS mode, a.provider, {stamp} AS started_at "
            f"FROM {table} a JOIN projects p ON p.id=a.project_id WHERE a.status IN ({active}) ORDER BY a.project_id"))
    for row in c.execute("SELECT key,value FROM app_settings WHERE key LIKE ? "
                         "AND json_extract(value,'$.status')='running' AND json_extract(value,'$.process')=? ORDER BY key",
                         (reproduce.PREFIX + '%', reproduce.PROCESS)):
        state = json.loads(row['value'])
        busy.append({'ref': row['key'][len(reproduce.PREFIX):], 'mode': 'reproduce', 'provider': '', 'started_at': state.get('started_at')})
    busy = [{**{k: r[k] for k in ('ref', 'mode', 'provider')}, 'minutes': minutes(r['started_at'])} for r in busy]
    phase = record['phase']
    phase = ('waiting' if busy else 'restarting') if phase == 'deployed' else (
        'restarting' if phase in ('restart_requested', 'rollback_requested', 'rollback_applied') else 'merging')
    overdue = max(0, minutes(record['updated_at']) - int(json.loads(record['cfg_json']).get('wait_minutes', 60))) if phase == 'waiting' else 0
    return {'phase': phase, 'ref': record['ref'], 'since': record['updated_at'], 'busy': busy,
            'overdue_minutes': overdue,
            'waiting': [j['ref'] for j in _rows(c)],
            'process_id': orchestrate.PROCESS_ID, 'revision': orchestrate.BOOT_SHA}


def _active_lanes(c) -> set[tuple]:
    """지금 바쁜 줄 — 도는 runs, 병합·반영 중인 실행, 아직 끝나지 않은 작업 스레드."""
    lanes = {(r["key"], r["mode"]) for r in c.execute(
        "SELECT p.key, r.mode FROM runs r JOIN issues i ON i.id=r.issue_id JOIN projects p ON p.id=i.project_id WHERE r.status='running'")}
    lanes |= {(m["ref"].split("-")[0], "execute") for m in _pending_merges(c)}
    import project_activity
    return lanes | project_activity.lanes(c) | {lane for lane, n in _threads.items() if n and lane}


def busy() -> bool:
    """바쁜 줄이 하나라도 있는지."""
    with db.connect() as c:
        return bool(_active_lanes(c))


def start_block(c, project_key: str, mode: str) -> str | None:
    """이 줄에서 지금 새로 시작하면 안 되는 이유. 없으면 None."""
    import project_restart
    import reproduce
    if reproduce.running(c):
        return '실패 검사 재현이 끝난 뒤 시작해요.'
    block = project_restart.block(c, project_key)
    if block:
        return block
    if c.execute("SELECT 1 FROM project_services WHERE status='running'").fetchone():
        return '공용 터널의 서비스 연결이 끝나면 시작해요.'
    import orchestrate
    import project_activity
    import rollback
    if any(r['project_key'] == project_key or
           orchestrate.settings(r['project_key']).get('pm2_app') == SELF_PM2_APP
           for r in rollback._records() if r['phase'] not in ('complete', 'failed')):
        return '롤백·운영 반영이 끝나면 시작해요.'
    if project_activity.audit_active(c, project_key):
        return '프로젝트 목표 대비 점검이 진행 중이에요. 끝난 뒤 시작해 주세요.'
    for m in _pending_merges(c):
        if orchestrate.settings(m["ref"].split("-")[0]).get("pm2_app") == SELF_PM2_APP:
            return f"{m['ref']} 병합 뒤 dev 재시작을 기다려요 — 재시작하면 도는 작업이 모두 끊겨서 그동안은 새로 시작하지 않아요."
    lanes = _active_lanes(c)
    if (project_key, 'initialization') in lanes and mode != 'initialization':
        return '프로젝트 초기 생성이 끝나면 시작해요.'
    if mode in ('audit', 'initialization') and any(lane[0] == project_key for lane in lanes):
        return '프로젝트 작업이 끝난 뒤 시작해 주세요.'
    if (project_key, mode) in lanes:
        merging = next((m["ref"] for m in _pending_merges(c) if mode == "execute" and m["ref"].split("-")[0] == project_key), None)
        if merging:
            return f"{merging}의 병합·운영 반영이 끝나면 시작해요."
        return f"{project_key} {'실행' if mode == 'execute' else '검토'} 줄의 앞 작업이 끝나면 시작해요."
    limits = concurrency_limits(c)
    counts = lane_counts(c, lanes)
    kind = lane_kind(mode)
    if limits[kind] is not None and counts[kind] >= limits[kind]:
        return f"{'실행' if kind == 'execute' else '검토'} 상한({limits[kind]}개)이 차서 기다려요 — 도는 작업이 끝나면 시작해요."
    if len(lanes) >= limits["total"]:
        return (f"전체 동시 실행 상한({limits['total']}개)이 차서 기다려요 — "
                f"지금 실행 {counts['execute']}·검토 {counts['review']}")
    return None


def _latest(c, issue_id):
    event = c.execute("SELECT data_json FROM events WHERE issue_id=? AND kind='status' ORDER BY id DESC LIMIT 1", (issue_id,)).fetchone()
    return json.loads(event[0]) if event else {}


def _waiting(c, j):
    row = c.execute("SELECT * FROM issues WHERE id=?", (j["issue_id"],)).fetchone()
    if c.execute("SELECT 1 FROM execution_completion WHERE issue_id=? AND phase IN ('ready','checking','applying','deployed','restart_requested','rollback_requested','rollback_applied')", (j['issue_id'],)).fetchone():
        return
    if j["previous_status"] is not None or row["status"] in ("done", "closed"):
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


def enqueue(actor: dict, ref: str, mode: str, provider: str | None = None, *, note=None, plan_version=None) -> dict:
    """줄에 넣고 펌프를 돌린다. 바로 시작하면 {started}, 아니면 {queued, position}."""
    import auto_settings
    import execute
    import project_docs
    if actor["kind"] != "human":
        raise issues.StoreError("검토·실행은 사람만 맡길 수 있어요.", 403)
    if note is not None:
        if not isinstance(note, str):
            raise issues.StoreError('재검토 메모는 글로 적어 주세요.', 400)
        note = issues._text(note, '재검토 메모', 20_000, required=True).strip()
        if mode != 'review' or type(plan_version) is not int:
            raise issues.StoreError('재검토할 계획서 판을 지정해 주세요.', 400)
    issue = issues.get_issue(ref)
    provider = project_docs.resolve_provider(issue['id'], provider)
    if provider not in ("claude", "codex"):
        raise issues.StoreError("지원하지 않는 검토 도구예요.", 400)
    if mode not in ("review", "execute"):
        raise issues.StoreError("지원하지 않는 작업이에요.", 400)
    issue = issues.get_issue(ref)   # 없으면 404
    ref = issue["ref"]
    if mode == 'review' and note is None:
        import project_init
        from pathlib import Path
        with db.connect() as c:
            initialization = c.execute("SELECT r.status,p.local_path FROM project_initializations r JOIN projects p ON p.id=r.project_id WHERE p.key=?", (issue['project_key'],)).fetchone() if c.execute("SELECT 1 FROM sqlite_master WHERE name='project_initializations'").fetchone() else None
        if initialization and not Path(initialization['local_path']).exists() and initialization['status'] == 'failed':
            project_init.retry(actor, issue['project_key'])
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        issue = issues.get_issue(ref)
        issues._guard_terminal_parent(c, issues._find(c, ref))
        if note is not None:
            if issue['parent_ref'] or issue['status'] in issues.HUMAN_ONLY_STATUSES:
                raise issues.StoreError('열려 있는 최상위 Issue의 계획서만 재검토할 수 있어요.', 409)
            if not issue['plan'] or issue['plan']['version'] != plan_version:
                raise issues.StoreError('보던 계획서가 최신이 아니에요. 새로고침한 뒤 재검토해 주세요.', 409)
            active = c.execute("""WITH RECURSIVE family(id) AS (
                SELECT ? UNION SELECT i.id FROM issues i JOIN family f ON i.parent_id=f.id)
                SELECT 1 FROM runs WHERE issue_id IN (SELECT id FROM family) AND status='running'
                UNION ALL SELECT 1 FROM execution_completion WHERE issue_id IN (SELECT id FROM family)
                AND phase IN ('ready','checking','applying','deployed','restart_requested','rollback_requested','rollback_applied') LIMIT 1""",
                (issue['id'],)).fetchone()
            if active:
                raise issues.StoreError('하위 Task가 실행·병합 중이에요. 작업이 끝난 뒤 재검토해 주세요.', 409)
            if c.execute("SELECT 1 FROM runs WHERE issue_id=? AND status='running' UNION ALL SELECT 1 FROM jobs WHERE issue_id=? AND status='queued' LIMIT 1", (issue['id'], issue['id'])).fetchone():
                raise issues.StoreError('이미 맡긴 작업이 있어요. 끝나거나 대기를 취소한 뒤 재검토해 주세요.', 409)
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
        if note is not None:
            issues._event(c, issue['id'], actor, 'comment', note,
                          {'plan_revision_request': True, 'plan_version': plan_version, 'job_id': j['id'], 'provider': provider})
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
    if direction not in ("up", "down", "top", "bottom"):
        raise issues.StoreError("방향은 up, down, top, bottom 중 하나예요.", 400)
    if neighbor_id is not None and type(neighbor_id) is not int:
        raise issues.StoreError("이웃 항목 번호가 올바르지 않아요.", 400)
    with _lock, db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        rows = c.execute("SELECT id, sort_key FROM jobs WHERE status='queued' ORDER BY sort_key, id").fetchall()
        at = next((n for n, r in enumerate(rows) if r["id"] == job_id), None)
        if at is None:
            raise issues.StoreError("대기 중인 항목이 아니에요.", 404)
        if direction in ("top", "bottom"):
            # 맨 위/아래: neighbor_id는 화면에서 본 첫/마지막 항목. 이미 그 자리면 순서를 바꾸지 않는다.
            edge = rows[0] if direction == "top" else rows[-1]
            if edge["id"] != neighbor_id:
                raise issues.StoreError("대기열이 그 사이 바뀌었어요 — 새로 고친 순서를 보고 다시 옮겨 주세요.", 409)
            other = edge if edge["id"] != job_id else None
            if other:
                key = rows[0]["sort_key"] - 1 if direction == "top" else rows[-1]["sort_key"] + 1
                c.execute("UPDATE jobs SET sort_key=? WHERE id=?", (key, job_id))
            return_after = True
        else:
            return_after = False
            n = at - 1 if direction == "up" else at + 1
            other = rows[n] if 0 <= n < len(rows) else None
            if (other["id"] if other else None) != neighbor_id:
                raise issues.StoreError("대기열이 그 사이 바뀌었어요 — 새로 고친 순서를 보고 다시 옮겨 주세요.", 409)
        if other and not return_after:
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
    사람을 기다리는 부모도 남은 Task 파일을 예약한다. keep_stalled는 검토 프롬프트와의 호환 인자다."""
    import execute
    held = {}
    for p in c.execute(f"""SELECT DISTINCT q.id, {issues.ref_sql('q', 'k')} AS ref FROM runs r JOIN issues i ON i.id=r.issue_id
            JOIN issues q ON q.id=i.parent_id JOIN projects k ON k.id=q.project_id WHERE r.mode='execute'""").fetchall():
        kids = c.execute(f"SELECT i.status, i.body FROM issues i WHERE i.parent_id=? AND i.status NOT IN ('done','closed') AND {issues._SUPERSEDED} IS NULL", (p["id"],)).fetchall()
        if not kids:
            continue
        todo = ("backlog", "waiting", "in_progress") + STALLED
        files = set().union(*(execute.task_files({"body": k["body"]}) for k in kids if k["status"] in todo))
        if files or any(k["status"] in STALLED for k in kids):
            held[p["id"]] = (p["ref"], files)
    return held


def _overlap(a: set[str], b: set[str]) -> list[str]:
    """같은 파일이거나 한쪽이 다른 쪽의 폴더."""
    return sorted({x for x in a for y in b if x == y or x.startswith(y.rstrip("/") + "/") or y.startswith(x.rstrip("/") + "/")})


def schedule(c, queued: list[dict], stalled_waiters: dict[int, set[str]] | None = None,
             stalled_jobs: set[int] | None = None) -> tuple[list[dict], dict[int, str]]:
    """꺼낼 순서와 파일 겹침으로 기다릴 항목의 메모 {job id: 메모}.
    순서: 진행 중인 부모의 실행 → 그 밖의 실행 → 검토(같은 묶음 안은 대기열 순서). 시작한 부모를 먼저 끝내
    다른 이슈의 Task가 사이에 끼어 같은 파일을 바꾸지 않게 한다(DEV-68·70, DEV-81·82).
    진행 중인 부모끼리는 멈춘 부모와 겹치는 경우에만 막는다."""
    import execute
    held = _reservations(c)
    info = {r["id"]: r for r in c.execute(
        f"SELECT i.id, i.project_id, i.parent_id, i.body, {issues.ref_sql('i', 'p')} AS ref "
        f"FROM issues i JOIN projects p ON p.id=i.project_id WHERE i.id IN ({','.join('?' * len(queued))})",
        [j["issue_id"] for j in queued])} if queued else {}
    held_projects = {r["id"]: r["project_id"] for r in c.execute(
        f"SELECT id, project_id FROM issues WHERE id IN ({','.join('?' * len(held))})", list(held))} if held else {}
    stalled = {r["parent_id"] for r in c.execute(
        f"SELECT DISTINCT i.parent_id FROM issues i WHERE i.parent_id IN ({','.join('?' * len(held))}) "
        f"AND i.status IN ('on_hold','changes_requested') AND {issues._SUPERSEDED} IS NULL", list(held))} if held else set()

    def stalled_predecessors(parent_id):
        if parent_id is None or not stalled:
            return set()
        return {r[0] for r in c.execute("""WITH RECURSIVE chain(id) AS (
            SELECT blocked_by_id FROM issue_deps WHERE issue_id=?
            UNION SELECT d.blocked_by_id FROM issue_deps d JOIN chain ON d.issue_id=chain.id)
            SELECT id FROM chain""", (parent_id,))} & stalled

    def group(j):
        if j["mode"] != "execute":
            return 2
        parent_id = info[j["issue_id"]]["parent_id"]
        return 0 if parent_id in held and parent_id not in stalled else 1
    ordered = sorted(queued, key=group)
    notes = {}
    for j in ordered:
        if j["mode"] != "execute":
            continue
        parent_id = info[j["issue_id"]]["parent_id"]
        ancestors = stalled_predecessors(parent_id)
        mine = execute.task_files({"body": info[j["issue_id"]]["body"]})
        for parent_id, (ref, files) in held.items():
            if parent_id == info[j["issue_id"]]["parent_id"]:
                continue
            if held_projects[parent_id] != info[j["issue_id"]]["project_id"]:
                continue
            if parent_id in ancestors:
                notes[j["id"]] = f"{ref}이 사람을 기다려요 — 선행이라 함께 기다려요"
            elif parent_id in stalled:
                hit = _overlap(mine, files)
                if hit:
                    notes[j["id"]] = f"{ref}이 사람을 기다려요 — 겹치는 파일: " + ", ".join(hit[:5]) + (" 외" if len(hit) > 5 else "")
            elif group(j) == 1:
                hit = _overlap(mine, files)
                if hit:
                    notes[j["id"]] = f"{ref} 작업이 끝날 때까지 기다려요 — 겹치는 파일: " + ", ".join(hit[:5]) + (" 외" if len(hit) > 5 else "")
            if j["id"] in notes:
                if parent_id in stalled:
                    if stalled_waiters is not None:
                        stalled_waiters.setdefault(parent_id, set()).add(info[j["issue_id"]]["ref"])
                    if stalled_jobs is not None:
                        stalled_jobs.add(j["id"])
                break
    return ordered, notes


def _notify_stalled(c, waiters: dict[int, set[str]]) -> None:
    """멈춘 Task의 이번 보류 기간에 대기 알림을 한 번만 남긴다."""
    for parent_id, refs in waiters.items():
        last_stall = c.execute("""SELECT MAX(e.id) FROM events e JOIN issues i ON i.id=e.issue_id
            WHERE i.parent_id=? AND e.kind='status'
            AND json_extract(e.data_json,'$.to') IN ('on_hold','changes_requested')""", (parent_id,)).fetchone()[0] or 0
        last_notice = c.execute("""SELECT MAX(id) FROM events WHERE issue_id=? AND kind='comment'
            AND json_extract(data_json,'$.source')='stalled_job_wait'""", (parent_id,)).fetchone()[0] or 0
        if last_notice > last_stall:
            continue
        issues._event(c, parent_id, {"kind": "agent", "id": "dev/queue"}, "comment",
                      "이 이슈 때문에 " + ", ".join(sorted(refs)) + "이 기다리고 있어요.",
                      {"source": "stalled_job_wait"})
        c.execute("UPDATE issues SET updated_at=? WHERE id=?", (db.now_iso(), parent_id))


def _note(job_id: int, note: str) -> None:
    with db.connect() as c:
        c.execute("UPDATE jobs SET note=? WHERE id=? AND status='queued'", (note, job_id))


def pump() -> None:
    """비어 있는 줄마다 시작할 수 있는 첫 항목을 돌린다(전체 상한까지)."""
    import execute, review
    with _lock:
        import automation
        import orchestrate
        # 병합을 이어받는 스레드가 없을 때만 — 도는 병합과 겹치지 않게. 병합 잠금은 기다리지 않는다:
        # 이 _lock을 쥔 채 병합 검사(수십 분)를 기다리면 넣기·취소·화면 요청까지 모두 멈춘다(DEV-160-1 병합 재시도).
        if not any(n for lane, n in _threads.items() if lane and lane[1] == "execute") and orchestrate._lock.acquire(blocking=False):
            try:
                orchestrate.recover()
            finally:
                orchestrate._lock.release()
        automation.sync()
        reconcile()
        stalled_waiters, stalled_jobs = {}, set()
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            queued, waits = schedule(c, _rows(c), stalled_waiters, stalled_jobs)
            _notify_stalled(c, stalled_waiters)
        for j in queued:
            actor = _actor(j["actor"])
            if j['mode'] == 'review':
                with db.connect() as c:
                    initialization = c.execute("SELECT r.status,r.error FROM project_initializations r JOIN issues i ON i.project_id=r.project_id WHERE i.id=?", (j['issue_id'],)).fetchone() if c.execute("SELECT 1 FROM sqlite_master WHERE name='project_initializations'").fetchone() else None
                if initialization and initialization['status'] != 'complete':
                    _note(j['id'], '초기 프로젝트 생성 완료를 기다려요.' if initialization['status'] in ('queued', 'running') else '초기 생성을 완료한 뒤 검토할 수 있어요. ' + initialization['error'])
                    continue
            with db.connect() as c:
                block = start_block(c, j["ref"].split("-")[0], j["mode"])
            if block:   # 그 줄의 진짜 이유만 적는다 — 다른 줄 항목은 계속 본다. 실행은 자기 사정(선행·겹치는 파일)이 있으면 그것이 더 쓸모 있다.
                own = None
                if j["mode"] == "execute":
                    try:
                        issue = issues.get_issue(j["ref"])
                        own = (waits.get(j["id"]) if j["id"] in stalled_jobs else None) or (
                            execute.blocked_reason(issue, issues.get_issue(issue["parent_ref"]) if issue["parent_ref"] else None)
                            or execute.predecessor_merge_reason(issue) or waits.get(j["id"]))
                    except issues.StoreError:
                        pass
                _note(j["id"], own or block)
                continue
            try:
                import troubleshoot
                with db.connect() as c:
                    recovery = troubleshoot.for_job(c, j['id'])
                    if recovery and not troubleshoot.valid_job(c, j):
                        troubleshoot.cancel_invalid(c, j)
                        continue
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
                    waiting_refs = ([b["ref"] for b in c.execute(
                        f"SELECT {issues.ref_sql('b', 'p')} AS ref FROM issue_deps d "
                        "JOIN issues b ON b.id=d.blocked_by_id JOIN projects p ON p.id=b.project_id "
                        "WHERE d.issue_id=? AND b.status!='done' ORDER BY b.id", (j["issue_id"],))]
                        if row and j["mode"] == "review" and row["parent_id"] is None else [])
                if not row or row["status"] != "waiting" or not owned:
                    _set(j["id"], "skipped", "상태가 바뀌었거나 보호된 이슈여서 대기를 종료했어요")
                    continue
                if waiting_refs:
                    _set(j["id"], "queued", f"선행 Issue({', '.join(waiting_refs)})가 done이 되어야 해요.")
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
                    why = (waits.get(j["id"]) if j["id"] in stalled_jobs else None) or (
                        execute.blocked_reason(issue, parent) or execute.predecessor_merge_reason(issue) or waits.get(j["id"]))
                    if why:   # 선행·겹치는 파일 대기 — 뒤 항목을 먼저 본다
                        _set(j["id"], "queued", why)
                        continue
                    if recovery:
                        execute.start(actor, j['ref'], j['provider'], j['model'], troubleshoot_id=recovery['id'])
                    else:
                        execute.start(actor, j["ref"], j["provider"], j["model"])
            except sqlite3.IntegrityError as e:
                if j['source'] != 'auto' and not recovery:
                    raise
                with db.connect() as c:
                    c.execute('BEGIN IMMEDIATE')
                    automation.cancel_invalid(c, j, start_error=str(e) != '자동 위임 조건이 바뀌었어요')
                continue
            except issues.StoreError as e:
                if recovery:
                    with db.connect() as c:
                        troubleshoot.cancel_invalid(c, j, str(e))
                elif j['source'] == 'auto' or e.status == 404:
                    _set(j["id"], "skipped", str(e))
                else:   # 저장소가 깨끗하지 않음 등 — 줄에서 기다린다
                    _set(j["id"], "queued", str(e))
                continue
            _set(j["id"], "started")

        import qa_runner
        qa_runner.pump()


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
            try:
                import execute
                execute.prune_worktrees()
            except Exception as e:
                print("worktree 정리 오류:", e)
            time.sleep(PERIOD_SEC)
    threading.Thread(target=loop, daemon=True).start()
