"""
프로젝트·이슈·계획서·이벤트 저장소 — REST 라우트와 MCP 도구가 같이 쓴다(권한 판단도 여기서).

actor는 app이 만든 dict: {"kind": "human"|"agent", "id", "name", "model"}.

## 권한
- 사람(admin): 전부.
- 에이전트: 이슈 만들기(하위 Task 포함)·계획서·댓글·커밋 연결·잡기/놓기·상태 바꾸기(done/closed 제외).
  이슈 제목·본문은 **자기가 만든 이슈만** 고친다 — 사람이 쓴 요구(지시)를 에이전트가 바꾸지 못하게.
  지우기·프로젝트 만들기는 못 한다.

## 잡기(claim)
한 이슈는 한 번에 한 actor만 잡는다. lease_until이 지나면 풀린 것으로 본다(죽은 에이전트가 영영 붙들지 않게).
같은 actor가 다시 잡으면 연장(heartbeat). in_review/done/closed로 가면 저절로 놓는다.
"""
import json
import hashlib
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import db
import notify


class StoreError(Exception):
    status = 400

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        if status:
            self.status = status


def _not_found(what="이슈"):
    return StoreError(f"없는 {what}예요." if what != "이슈" else "없는 이슈예요.", 404)


def _forbidden(msg):
    return StoreError(msg, 403)


RELEASE_ON = {"in_review", "done", "closed"}
HUMAN_ONLY_STATUSES = {"done", "closed"}
REF_RE = re.compile(r"^([A-Z][A-Z0-9]{0,9})-(\d+)(?:-(\d+))?$")   # NS-17 또는 Task NS-17-1
MAX_TEXT = 200_000


def actor_label(actor: dict) -> str:
    return f"human:{actor['name']}" if actor["kind"] == "human" else f"agent:{actor['id']}"


def _is_human(actor):
    return actor["kind"] == "human"


def _now():
    return datetime.now(timezone.utc)


def _text(value, what, limit=MAX_TEXT, required=False) -> str:
    v = "" if value is None else str(value)
    if required and not v.strip():
        raise StoreError(f"{what}이(가) 필요해요.")
    if len(v) > limit:
        raise StoreError(f"{what}이(가) 너무 길어요.")
    return v


def _event(c, issue_id, actor, kind, body="", data=None):
    d = dict(data or {})
    if actor["kind"] == "agent":
        # 그때의 모델명을 남긴다 — 이 이슈에 도는 실행이 있으면 Agent 행 값보다 실제로 돌린 runs.model이 맞다
        run = c.execute("SELECT model FROM runs WHERE issue_id=? AND status='running' AND model IS NOT NULL AND model<>'' ORDER BY id DESC LIMIT 1",
                        (issue_id,)).fetchone()
        d.setdefault("model", run["model"] if run else actor.get("model") or "")
    c.execute("INSERT INTO events(issue_id, actor, kind, body, data_json, created_at) VALUES(?,?,?,?,?,?)",
              (issue_id, actor_label(actor), kind, body, json.dumps(d, ensure_ascii=False), db.now_iso()))


# ---- 종류 카탈로그 ----
def _type_dict(row):
    return {**dict(row), "active": bool(row["active"])}


def list_issue_types(include_inactive=False):
    with db.connect() as c:
        return [_type_dict(r) for r in c.execute(
            "SELECT * FROM issue_types" + ("" if include_inactive else " WHERE active=1") + " ORDER BY id")]


def create_issue_type(actor, name):
    if not _is_human(actor):
        raise _forbidden("종류 관리는 사람만 할 수 있어요.")
    name = _text(name, "종류 이름", 100, required=True).strip()
    try:
        with db.connect() as c:
            tid = c.execute("INSERT INTO issue_types(name) VALUES(?)", (name,)).lastrowid
            return _type_dict(c.execute("SELECT * FROM issue_types WHERE id=?", (tid,)).fetchone())
    except sqlite3.IntegrityError:
        raise StoreError("같은 이름의 종류가 있어요.", 409)


def update_issue_type(actor, type_id, fields):
    if not _is_human(actor):
        raise _forbidden("종류 관리는 사람만 할 수 있어요.")
    sets = {}
    if "name" in fields:
        sets["name"] = _text(fields["name"], "종류 이름", 100, required=True).strip()
    if "active" in fields:
        if not isinstance(fields["active"], bool):
            raise StoreError("active는 참/거짓이어야 해요.")
        sets["active"] = int(fields["active"])
    if not sets:
        raise StoreError("바꿀 내용이 없어요.")
    try:
        with db.connect() as c:
            if not c.execute(f"UPDATE issue_types SET {', '.join(k+'=?' for k in sets)} WHERE id=?",
                             (*sets.values(), type_id)).rowcount:
                raise _not_found("종류")
            return _type_dict(c.execute("SELECT * FROM issue_types WHERE id=?", (type_id,)).fetchone())
    except sqlite3.IntegrityError:
        raise StoreError("같은 이름의 종류가 있어요.", 409)


def _type_ids(c, value, issue_id=None):
    if not isinstance(value, list) or any(type(x) is not int or x <= 0 for x in value):
        raise StoreError("type_ids는 양의 정수 ID 목록이어야 해요.")
    ids = sorted(set(value))
    existing = {r[0] for r in c.execute("SELECT type_id FROM issue_type_links WHERE issue_id=?", (issue_id,))} if issue_id else set()
    for tid in ids:
        row = c.execute("SELECT active FROM issue_types WHERE id=?", (tid,)).fetchone()
        if row is None or (not row[0] and tid not in existing):
            raise StoreError("없거나 비활성화된 종류는 새로 선택할 수 없어요.")
    return ids


def _replace_types(c, iid, ids, actor):
    c.execute("DELETE FROM issue_type_links WHERE issue_id=?", (iid,))
    c.executemany("INSERT INTO issue_type_links(issue_id,type_id,source,actor,created_at) VALUES(?,?,?,?,?)",
                  [(iid, tid, actor["kind"], actor_label(actor), db.now_iso()) for tid in ids])


def _type_revision(c, iid):
    for e in c.execute("SELECT id, data_json FROM events WHERE issue_id=? AND kind='edit' ORDER BY id DESC", (iid,)):
        if "type_ids" in json.loads(e["data_json"]).get("fields", []):
            return e["id"]
    return 0


def classify_issue(actor, ref, type_ids, expected_revision):
    """미분류 상태와 종류 수정 판을 원자적으로 확인하고 자동 분류한다."""
    if _is_human(actor):
        raise _forbidden("자동 분류는 에이전트 전용이에요.")
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row = _find(c, ref)
        _guard_goal(row, actor, "종류")
        current = _issue_dict(row)
        if current["type_ids"]:
            return {"saved": False, "reason": "already_classified", "type_ids": current["type_ids"]}
        if type(expected_revision) is not int or expected_revision != _type_revision(c, row["id"]):
            raise StoreError("종류 선택이 바뀌었어요 — 다시 조회해 주세요.", 409)
        ids = _type_ids(c, type_ids)
        if not ids:
            raise StoreError("자동 분류에는 종류를 하나 이상 선택해야 해요.")
        _replace_types(c, row["id"], ids, actor)
        c.execute("UPDATE issues SET updated_at=? WHERE id=?", (db.now_iso(), row["id"]))
        _event(c, row["id"], actor, "edit", data={"fields": ["type_ids"], "automatic": True})
        return {"saved": True, "type_ids": ids}


# ---- 프로젝트 ----
def _project_row(r) -> dict:
    d = dict(r)
    d["archived"] = bool(d["archived"])
    d.pop("next_number", None)
    return d


def list_projects() -> list[dict]:
    with db.connect() as c:
        return [_project_row(r) for r in c.execute("SELECT * FROM projects ORDER BY archived, key")]


def create_project(actor, key, name, repo_url="", local_path="", description="") -> dict:
    if not _is_human(actor):
        raise _forbidden("프로젝트는 사람만 만들 수 있어요.")
    key = str(key or "").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9]{0,9}", key):
        raise StoreError("키는 영문 대문자로 시작하는 10자 이하 영문·숫자예요(예: NS).")
    name = _text(name, "이름", 100, required=True).strip()
    try:
        with db.connect() as c:
            pid = c.execute("INSERT INTO projects(key, name, repo_url, local_path, description, created_at) VALUES(?,?,?,?,?,?)",
                            (key, name, _text(repo_url, "저장소 주소", 500), _text(local_path, "로컬 경로", 500),
                             _text(description, "프로젝트 설명"), db.now_iso())).lastrowid
            return _project_row(c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone())
    except sqlite3.IntegrityError:
        raise StoreError("같은 키의 프로젝트가 있어요.", 409)


def update_project(actor, key, fields: dict) -> dict:
    if not _is_human(actor):
        raise _forbidden("프로젝트는 사람만 고칠 수 있어요.")
    sets = {}
    for k, limit in (("name", 100), ("repo_url", 500), ("local_path", 500)):
        if k in fields:
            sets[k] = _text(fields[k], k, limit, required=(k == "name")).strip()
    if "archived" in fields:
        sets["archived"] = 1 if fields["archived"] else 0
    if "description" in fields:
        sets["description"] = _text(fields["description"], "프로젝트 설명")
    if not sets:
        raise StoreError("바꿀 내용이 없어요.")
    with db.connect() as c:
        n = c.execute(f"UPDATE projects SET {', '.join(k + '=?' for k in sets)} WHERE key=?", (*sets.values(), key)).rowcount
        if not n:
            raise _not_found("프로젝트")
        return _project_row(c.execute("SELECT * FROM projects WHERE key=?", (key,)).fetchone())


def _project_deletion(c, key):
    """같은 트랜잭션에서 삭제 범위와 확인용 지문을 구한다."""
    project = c.execute("SELECT * FROM projects WHERE key=?", (key,)).fetchone()
    if project is None:
        raise _not_found("프로젝트")
    rows = c.execute("SELECT * FROM issues WHERE project_id=? ORDER BY id", (project["id"],)).fetchall()
    target = "SELECT id FROM issues WHERE project_id=?"
    snapshot = {"project": dict(project), "issues": [dict(r) for r in rows]}
    for table in ("plans", "events", "decisions", "runs", "jobs"):
        snapshot[table] = [dict(r) for r in c.execute(
            f"SELECT * FROM {table} WHERE issue_id IN ({target}) ORDER BY id", (project["id"],))]
    deps = [dict(r) for r in c.execute(
        f"SELECT * FROM issue_deps WHERE issue_id IN ({target}) OR blocked_by_id IN ({target}) ORDER BY issue_id, blocked_by_id",
        (project["id"], project["id"]))]
    snapshot["dependencies"] = deps
    ids = {r["id"] for r in rows}
    blockers = []
    if any(_lease_active(r) for r in rows):
        blockers.append("유효한 작업 점유가 있어요.")
    if any(r["status"] == "running" for r in snapshot["runs"]):
        blockers.append("실행 중인 작업이 있어요.")
    runs = {r["id"]: r for r in snapshot["runs"]}
    if any(j["status"] == "started" and j["run_id"] not in runs for j in snapshot["jobs"]):
        blockers.append("시작된 대기열 작업의 실행 기록을 확인할 수 없어요.")
    token = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"project": _project_row(project), "issue_count": len(rows),
            "external_dependency_count": sum(d["issue_id"] not in ids for d in deps),
            "confirmation_required": bool(rows), "confirmation_token": token,
            "can_delete": not blockers, "blockers": blockers,
            "warning": "모든 Issue·Task와 계획서·댓글·결정·실행·대기열 기록 및 다른 프로젝트의 의존 연결도 함께 삭제돼요." if rows else ""}


def check_project_deletion(actor, key):
    if not _is_human(actor):
        raise _forbidden("프로젝트 삭제는 사람만 할 수 있어요.")
    with db.connect() as c:
        c.execute("BEGIN")
        return _project_deletion(c, str(key).upper())


def delete_project(actor, key, confirmation_token=None):
    if not _is_human(actor):
        raise _forbidden("프로젝트 삭제는 사람만 할 수 있어요.")
    with db.connect() as c:
        # 작업 시작과 내용 추가를 직렬화하여 확인 뒤 생긴 데이터를 지우지 않는다.
        c.execute("BEGIN IMMEDIATE")
        state = _project_deletion(c, str(key).upper())
        if not state["can_delete"]:
            raise StoreError(" ".join(state["blockers"]), 409)
        if state["confirmation_required"] and confirmation_token != state["confirmation_token"]:
            raise StoreError("프로젝트 내용이 있어요. 삭제 사전 확인을 다시 하고 동의해 주세요.", 409)
        pid = state["project"]["id"]
        c.execute("DELETE FROM issues WHERE project_id=?", (pid,))
        c.execute("DELETE FROM projects WHERE id=?", (pid,))
    _cleanup_attachments()


# ---- 이슈 ----
_LAST_DECISION = "(SELECT {} FROM decisions d WHERE d.issue_id=i.id AND d.gate='plan' ORDER BY d.id DESC LIMIT 1)"
_DEC_VERDICT, _DEC_VERSION = _LAST_DECISION.format("verdict"), _LAST_DECISION.format("plan_version")
_LATEST_PLAN = "(SELECT MAX(version) FROM plans pl WHERE pl.issue_id=i.id)"


def ref_sql(i, k):
    """ref를 SQL로 — 이슈 별칭 i, 프로젝트 별칭 k. Task는 NS-17-1, 나머지는 NS-27."""
    return f"{k}.key || '-' || CASE WHEN {i}.sub_number IS NOT NULL THEN {i}.sub_of || '-' || {i}.sub_number ELSE {i}.number END"


_PARENT_REF = f"(SELECT {ref_sql('q', 'p')} FROM issues q WHERE q.id=i.parent_id)"
_ISSUE_SELECT = (f"SELECT i.*, p.key AS project_key, {ref_sql('i', 'p')} AS ref, {_PARENT_REF} AS parent_ref, "
                 f"{_DEC_VERDICT} AS dec_verdict, {_DEC_VERSION} AS dec_version, "
                 f"{_LATEST_PLAN} AS latest_plan, "
                 "(SELECT json_group_array(json_object('id',t.id,'name',t.name,'active',t.active,'source',l.source)) "
                 "FROM issue_type_links l JOIN issue_types t ON t.id=l.type_id WHERE l.issue_id=i.id ORDER BY t.id) AS types_json "
                 "FROM issues i JOIN projects p ON p.id = i.project_id")


def _lease_active(row, now=None) -> bool:
    return bool(row["claimed_by"] and row["lease_until"] and datetime.fromisoformat(row["lease_until"]) > (now or _now()))


def title_missing(title) -> bool:
    """제목이 비었거나 글자·숫자가 하나도 없으면(예: ".") 없는 것으로 본다 — 에이전트가 본문을 보고 채운다(DEV-8)."""
    return not re.search(r"\w", title or "")


def _issue_dict(r) -> dict:
    d = dict(r)
    d["title_missing"] = title_missing(d["title"])
    d["labels"] = json.loads(d.pop("labels_json") or "[]")
    d["types"] = sorted(json.loads(d.pop("types_json") or "[]"), key=lambda t: t["id"])
    for t in d["types"]:
        t["active"] = bool(t["active"])
    d["type_ids"] = [t["id"] for t in d["types"]]
    verdict, version, latest = d.pop("dec_verdict"), d.pop("dec_version"), d.pop("latest_plan")
    # 결정은 그 계획서 판에 붙는다 — 새 판이 올라오면 stale(승인이 무효, 다시 결정해야 한다)
    d["approval"] = {"verdict": verdict, "plan_version": version, "stale": version != latest} if verdict else None
    if not _lease_active(r):
        d["claimed_by"] = None
        d["lease_until"] = None
    return d


def _find(c, ref) -> dict:
    """ref: "NS-12" 또는 내부 id(숫자)."""
    s = str(ref).strip().upper()
    m = REF_RE.match(s)
    if m and m.group(3):
        row = c.execute(_ISSUE_SELECT + " WHERE p.key=? AND i.sub_of=? AND i.sub_number=?", (m.group(1), int(m.group(2)), int(m.group(3)))).fetchone()
    elif m:
        row = c.execute(_ISSUE_SELECT + " WHERE p.key=? AND i.number=?", (m.group(1), int(m.group(2)))).fetchone()
    elif s.isdigit():
        row = c.execute(_ISSUE_SELECT + " WHERE i.id=?", (int(s),)).fetchone()
    else:
        row = None
    if row is None:
        raise _not_found()
    return row


def _labels(value) -> str:
    if value is None:
        return "[]"
    if not isinstance(value, list) or not all(isinstance(x, str) and 0 < len(x) <= 40 for x in value) or len(value) > 20:
        raise StoreError("labels는 40자 이하 글자 목록(20개까지)이에요.")
    return json.dumps(sorted(set(value)), ensure_ascii=False)


def _priority(value) -> str:
    v = str(value or "none")
    if v not in db.PRIORITIES:
        raise StoreError(f"priority는 {'/'.join(db.PRIORITIES)} 중 하나예요.")
    return v


def _list_action_context(c, rows):
    """목록과 페이지 밖 부모의 판단 근거를 묶음 조회한다. 실행 권한 검사를 대신하지 않는다."""
    ids = sorted({r["id"] for r in rows} | {r["parent_id"] for r in rows if r["parent_id"] is not None})
    if not ids:
        return {}
    marks = ','.join('?' * len(ids))
    contexts = {}
    for r in c.execute(_ISSUE_SELECT + f" WHERE i.id IN ({marks})", ids):
        issue = _issue_dict(r)
        contexts[r["id"]] = {"plan_version": r["latest_plan"], "approval": issue["approval"],
                              "has_children": False, "has_execution": False, "has_active_job": False}
    for r in c.execute(f"SELECT DISTINCT parent_id FROM issues WHERE parent_id IN ({marks})", ids):
        contexts[r["parent_id"]]["has_children"] = True
    for r in c.execute(f"SELECT DISTINCT issue_id FROM runs WHERE mode='execute' AND issue_id IN ({marks})", ids):
        contexts[r["issue_id"]]["has_execution"] = True
    for r in c.execute(f"SELECT DISTINCT j.issue_id FROM jobs j LEFT JOIN runs r ON r.id=j.run_id "
                       f"WHERE (j.status='queued' OR (j.status='started' AND (r.id IS NULL OR r.status='running'))) "
                       f"AND j.issue_id IN ({marks})", ids):
        contexts[r["issue_id"]]["has_active_job"] = True
    return {r["id"]: {**contexts[r["id"]], "parent": contexts.get(r["parent_id"])} for r in rows}


def list_issues(project=None, status=None, assignee=None, parent=None, q=None, limit=500, offset=0, approved=False) -> list[dict]:
    """status: 목록 또는 쉼표 문자열. parent: 이슈 ref(그 하위만) 또는 "none"(최상위만). offset부터 limit개(나눠 읽기).
    approved: 최신 계획서가 승인(조건부 포함)된 이슈만."""
    where, args = [], []
    if approved:
        where.append(f"{_DEC_VERDICT} IN ('approve','approve_notes') AND {_DEC_VERSION} = {_LATEST_PLAN}")
    if project:
        where.append("p.key=?"); args.append(str(project).upper())
    if status:
        sts = status.split(",") if isinstance(status, str) else list(status)
        bad = [s for s in sts if s not in db.STATUSES]
        if bad:
            raise StoreError(f"모르는 상태예요: {', '.join(bad)}")
        where.append(f"i.status IN ({','.join('?' * len(sts))})"); args += sts
    if assignee:
        where.append("i.assignee_agent_id=?"); args.append(int(assignee))
    with db.connect() as c:
        c.execute("BEGIN")   # 목록과 판단 근거를 같은 읽기 스냅샷에서 구한다.
        if parent == "none":
            where.append("i.parent_id IS NULL")
        elif parent:
            where.append("i.parent_id=?"); args.append(_find(c, parent)["id"])
        if q:
            where.append("(i.title LIKE ? OR i.body LIKE ?)"); args += [f"%{q}%"] * 2
        sql = _ISSUE_SELECT + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY i.updated_at DESC, i.id DESC LIMIT ? OFFSET ?"
        rows = c.execute(sql, (*args, max(1, min(int(limit), 2001)), max(0, int(offset)))).fetchall()
        contexts = _list_action_context(c, rows)
    out = []
    for r in rows:
        d = _issue_dict(r)
        d.pop("body")   # 목록에는 본문을 싣지 않는다(에이전트 토큰 절약) — get_issue로 읽는다
        d["action_context"] = contexts[r["id"]]
        out.append(d)
    return out


def get_issue(ref) -> dict:
    with db.connect() as c:
        row = _find(c, ref)
        d = _issue_dict(row)
        d["type_revision"] = _type_revision(c, row["id"])
        plan = c.execute("SELECT * FROM plans WHERE issue_id=? ORDER BY version DESC LIMIT 1", (row["id"],)).fetchone()
        d["plan"] = dict(plan) if plan else None
        dec = c.execute("SELECT * FROM decisions WHERE issue_id=? AND gate='plan' ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
        if dec:   # 요약(list와 같은 모양)에 메모·누가·언제를 더한다
            d["approval"] = {**d["approval"], "note": dec["note"], "actor": dec["actor"], "created_at": dec["created_at"]}
        d["events"] = [{**dict(e), "data": json.loads(e["data_json"])} for e in
                       c.execute("SELECT * FROM events WHERE issue_id=? ORDER BY id", (row["id"],))]
        for e in d["events"]:
            e.pop("data_json")
        def blockers(iid):   # 먼저 끝나야 하는 이슈들 — {"ref","status"}
            return [{"ref": r["ref"], "status": r["status"]} for r in c.execute(
                f"SELECT {ref_sql('b', 'p')} AS ref, b.status FROM issue_deps x JOIN issues b ON b.id=x.blocked_by_id "
                "JOIN projects p ON p.id=b.project_id WHERE x.issue_id=? ORDER BY b.id", (iid,))]
        d["blocked_by"] = blockers(row["id"])
        d["children"] = [{**{k: v for k, v in _issue_dict(ch).items() if k != "body"}, "blocked_by": blockers(ch["id"])} for ch in
                         c.execute(_ISSUE_SELECT + " WHERE i.parent_id=? ORDER BY i.id", (row["id"],))]
        par = c.execute(_ISSUE_SELECT + " WHERE i.id=?", (row["parent_id"],)).fetchone() if row["parent_id"] else None
        d["parent_ref"] = _issue_dict(par)["ref"] if par else None
        import attachments
        # 마이그레이션 전 데이터 보존 검사에서도 기존 이슈를 읽을 수 있게 한다.
        has_attachments = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='attachments'").fetchone()
        d["attachments"] = [attachments.metadata(dict(a)) for a in c.execute(
            'SELECT * FROM attachments WHERE issue_id=? ORDER BY created_at,id', (row['id'],))] if has_attachments else []
    return d


def events_for(ids) -> dict:
    """이슈 id들의 타임라인을 한 번에 — {id: [event…]}(id 순). 부모 상세가 Task마다 get_issue를 다시 부르지 않게(DEV-42-2)."""
    ids = list(ids)
    out = {i: [] for i in ids}
    if not ids:
        return out
    with db.connect() as c:
        for e in c.execute(f"SELECT * FROM events WHERE issue_id IN ({','.join('?' * len(ids))}) ORDER BY id", ids):
            out[e["issue_id"]].append({**{k: e[k] for k in e.keys() if k != "data_json"}, "data": json.loads(e["data_json"])})
    return out


def _next_number(c, proj, parent=None) -> tuple:
    """새 이슈의 (number, sub_of, sub_number). 최상위 이슈의 Task는 부모 번호 아래에서 센다(NS-17-1, -2…), 그 밖에는 프로젝트 번호."""
    if parent is not None and parent["parent_id"] is None and parent["sub_number"] is None:
        sub = c.execute("SELECT COALESCE(MAX(sub_number), 0) + 1 FROM issues WHERE project_id=? AND sub_of=?", (proj["id"], parent["number"])).fetchone()[0]
        return -(c.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM issues").fetchone()[0]), parent["number"], sub
    number = c.execute("SELECT next_number FROM projects WHERE id=?", (proj["id"],)).fetchone()[0]
    c.execute("UPDATE projects SET next_number=next_number+1 WHERE id=?", (proj["id"],))
    return number, None, None


def create_issue(actor, project, title, body="", priority="none", labels=None, parent=None, status="backlog", type_ids=None, attachment_ids=None) -> dict:
    # 제목은 비워도 된다 — 본문만 쓰면 이슈를 처리하는 에이전트가 제목을 지어 채운다(DEV-8). 둘 다 비면 안 된다.
    title = _text(title, "제목", 300).strip()
    body = _text(body, "본문")
    if title_missing(title) and not body.strip():
        raise StoreError("제목이나 본문 중 하나는 적어 주세요.")
    status = str(status or "backlog")
    if status not in ("backlog", "triage"):
        raise StoreError("새 이슈는 backlog나 triage로만 만들어요.")
    now = db.now_iso()
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        proj = c.execute("SELECT * FROM projects WHERE key=?", (str(project or "").upper(),)).fetchone()
        if proj is None:
            raise _not_found("프로젝트")
        parent_id, par = None, None
        if parent:
            par = _find(c, parent)
            if par["project_id"] != proj["id"]:
                raise StoreError("하위 이슈는 부모와 같은 프로젝트여야 해요.")
            parent_id = par["id"]
        ids = _type_ids(c, type_ids if type_ids is not None else [])
        number, sub_of, sub = _next_number(c, proj, par)
        iid = c.execute("INSERT INTO issues(project_id, number, sub_of, sub_number, parent_id, title, body, status, priority, labels_json, reporter, "
                        "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (proj["id"], number, sub_of, sub, parent_id, title, body, status, _priority(priority), _labels(labels),
                         actor_label(actor), now, now)).lastrowid
        _replace_types(c, iid, ids, actor)
        if attachment_ids is not None:
            import attachments
            attachments.link(c, actor, iid, attachment_ids)
        return _issue_dict(c.execute(_ISSUE_SELECT + " WHERE i.id=?", (iid,)).fetchone())


def update_issue(actor, ref, fields: dict) -> dict:
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row = _find(c, ref)
        own = row["reporter"] == actor_label(actor)
        _guard_goal(row, actor, "제목·본문·라벨·우선순위·종류")
        sets, changed = {}, []
        if "title" in fields or "body" in fields:
            # 예외: 제목이 없는 이슈(DEV-8)는 에이전트가 제목만 채울 수 있다 — 본문(지시)은 여전히 못 고친다.
            filling_title = "body" not in fields and title_missing(row["title"])
            if not _is_human(actor) and not own and not filling_title:
                raise _forbidden("사람이 쓴 이슈의 제목·본문은 에이전트가 고칠 수 없어요 — 댓글로 남겨 주세요.")
            if "title" in fields:
                sets["title"] = _text(fields["title"], "제목", 300, required=True).strip()
                if title_missing(sets["title"]):
                    raise StoreError("제목에 글자가 있어야 해요.")
            if "body" in fields:
                sets["body"] = _text(fields["body"], "본문")
        if "priority" in fields:
            sets["priority"] = _priority(fields["priority"])
        if "labels" in fields:
            sets["labels_json"] = _labels(fields["labels"])
        if "assignee_agent_id" in fields:
            aid = fields["assignee_agent_id"]
            if aid is not None and c.execute("SELECT 1 FROM agents WHERE id=?", (int(aid),)).fetchone() is None:
                raise _not_found("에이전트")
            sets["assignee_agent_id"] = None if aid is None else int(aid)
        if "parent" in fields:
            if fields["parent"]:
                par = _find(c, fields["parent"])
                if par["project_id"] != row["project_id"] or par["id"] == row["id"]:
                    raise StoreError("부모는 같은 프로젝트의 다른 이슈여야 해요.")
                sets["parent_id"] = par["id"]
            else:
                sets["parent_id"] = None
        types_changed = False
        if "type_ids" in fields:
            if not _is_human(actor) and (not own or any(t["source"] == "human" for t in _issue_dict(row)["types"])):
                raise _forbidden("에이전트의 자동 분류는 classify_issue를 사용해 주세요.")
            ids = _type_ids(c, fields["type_ids"], row["id"])
            # 같은 선택이나 빈 선택도 사람의 의사와 경합 검사를 위해 기록한다.
            types_changed = True
            if types_changed:
                _replace_types(c, row["id"], ids, actor)
        sets = {k: v for k, v in sets.items() if row[k] != v}
        if not sets and not types_changed:
            return _issue_dict(row)
        sets["updated_at"] = db.now_iso()
        c.execute(f"UPDATE issues SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), row["id"]))
        changed = [k.replace("_json", "") for k in sets if k != "updated_at"]
        if types_changed:
            changed.append("type_ids")
        if "assignee_agent_id" in changed:
            _event(c, row["id"], actor, "assign", data={"agent_id": sets["assignee_agent_id"]})
            changed.remove("assignee_agent_id")
        if changed:
            _event(c, row["id"], actor, "edit", data={"fields": changed})
        return _issue_dict(c.execute(_ISSUE_SELECT + " WHERE i.id=?", (row["id"],)).fetchone())


def delete_issue(actor, ref) -> None:
    if not _is_human(actor):
        raise _forbidden("이슈는 사람만 지울 수 있어요.")
    with db.connect() as c:
        c.execute("DELETE FROM issues WHERE id=?", (_find(c, ref)["id"],))
    _cleanup_attachments()


def _cleanup_attachments():
    # 삭제는 이미 커밋됐다. 파일/DB 실패는 영속 삭제 큐에서 재시도한다.
    import attachments
    import sqlite3
    try:
        attachments.cleanup()
    except (OSError, sqlite3.Error):
        pass


def _guard_goal(row, actor, what):
    """`goal` 라벨 이슈(사용자가 정한 최종 목표)는 사람만 바꾼다 — 에이전트가 달성했다고 올리거나 내용을 다시 쓰지 못하게."""
    if not _is_human(actor) and "goal" in json.loads(row["labels_json"] or "[]"):
        raise _forbidden(f"최종 목표(goal) 이슈의 {what}은(는) 사람만 바꿔요 — 진행 상황은 댓글로 남기고, 달성 여부는 사용자가 판단해요.")


def _set_status(c, actor, row, status, note="", data=None):
    """기존 연결에서 상태와 이벤트를 함께 기록한다(실행 시작 트랜잭션에서도 사용한다)."""
    if status not in db.STATUSES:
        raise StoreError(f"status는 {'/'.join(db.STATUSES)} 중 하나예요.")
    if status in HUMAN_ONLY_STATUSES and not _is_human(actor):
        raise _forbidden("done/closed는 사람이 확인하고 바꿔요 — 끝냈으면 in_review로 올려 주세요.")
    _guard_goal(row, actor, "상태")
    note = _text(note, "메모", 20_000)
    now = db.now_iso()
    c.execute("UPDATE issues SET status=?, closed_at=?, updated_at=?"
              + (", claimed_by=NULL, lease_until=NULL" if status in RELEASE_ON else "") + " WHERE id=?",
              (status, now if status in HUMAN_ONLY_STATUSES else None, now, row["id"]))
    _event(c, row["id"], actor, "status", note, {"from": row["status"], "to": status, **(data or {})})


def _terminal_change(actor, ref, rejecting):
    import rollback
    return rollback.terminal_change(actor, ref, rejecting)


def _sync_terminal_status(c, actor, row, status, note):
    """선택한 이슈와 같은 프로젝트의 후손을 원자적으로 종결한다."""
    ids = [r[0] for r in c.execute(
        "WITH RECURSIVE t(id) AS (SELECT ? UNION SELECT i.id FROM issues i JOIN t ON i.parent_id=t.id WHERE i.project_id=?) SELECT id FROM t",
        (row["id"], row["project_id"]))]
    marks = ",".join("?" * len(ids))
    busy = [r["ref"] for r in c.execute(
        f"SELECT DISTINCT {ref_sql('i', 'p')} AS ref FROM runs r JOIN issues i ON i.id=r.issue_id JOIN projects p ON p.id=i.project_id WHERE r.status='running' AND i.id IN ({marks})", ids)]
    if busy:
        raise StoreError(f"실행 중인 이슈가 있어요: {', '.join(busy)} — 끝난 뒤 다시 해 주세요.", 409)
    rows = c.execute(_ISSUE_SELECT + f" WHERE i.id IN ({marks}) ORDER BY i.id", ids).fetchall()
    pending = None
    if status == 'closed':
        import rollback
        pending = rollback.prepare(c, actor, row, rows, note)
        c.execute(f"UPDATE jobs SET status='cancelled' WHERE status='queued' AND issue_id IN ({marks})", ids)
    if pending and pending['restart']:
        status = 'on_hold'
        note = '롤백 코드는 반영됐어요. 운영 재시작 확인 전까지 보류해요. ' + note
    for child in rows:
        if child["status"] != status:
            _set_status(c, actor, child, status, note,
                        {**({'tree_from': row['ref']} if child['id'] != row['id'] else {}),
                         **({'rollback_id': pending['id'], 'rollback_sha': pending['head']} if pending else {})})


def set_status(actor, ref, status, note="") -> dict:
    status = str(status or "")
    if status not in db.STATUSES:
        raise StoreError(f"status는 {'/'.join(db.STATUSES)} 중 하나예요.")
    if status in HUMAN_ONLY_STATUSES and not _is_human(actor):
        raise _forbidden("done/closed는 사람이 확인하고 바꿔요 — 끝냈으면 in_review로 올려 주세요.")
    note = _text(note, "메모", 20_000)
    with _terminal_change(actor, ref, status == 'closed'), db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row = _find(c, ref)
        _guard_goal(row, actor, "상태")
        if status in HUMAN_ONLY_STATUSES:
            _sync_terminal_status(c, actor, row, status, note)
            return _issue_dict(c.execute(_ISSUE_SELECT + " WHERE i.id=?", (row["id"],)).fetchone())
        if row["status"] == status:
            return _issue_dict(row)
        _set_status(c, actor, row, status, note)
        # 에이전트가 사람을 부를 때 폰 알림 — 헤드리스 실행 중의 in_review는 실행이 끝날 때 한 번만 보낸다
        if not _is_human(actor) and status == "on_hold":
            notify.send(row["ref"], "❓ 사람의 답이 필요해요", note)
        elif not _is_human(actor) and status == "in_review" and not c.execute(
                "SELECT 1 FROM runs WHERE issue_id=? AND status='running'", (row["id"],)).fetchone():
            notify.send(row["ref"], "✅ 확인해 주세요(in_review)", note)
        return _issue_dict(c.execute(_ISSUE_SELECT + " WHERE i.id=?", (row["id"],)).fetchone())


def _complete_auto_task(c, row, provenance):
    """서버 내부 전용: 후손은 확인만 하고 선택 Task 하나만 완료한다."""
    if row['status'] != 'in_review' or not row['parent_id'] or 'goal' in json.loads(row['labels_json']):
        return False
    pending = c.execute("""WITH RECURSIVE t(id,status) AS (
        SELECT id,status FROM issues WHERE parent_id=?
        UNION SELECT i.id,i.status FROM issues i JOIN t ON i.parent_id=t.id)
        SELECT 1 FROM t WHERE status NOT IN ('done','closed') LIMIT 1""", (row['id'],)).fetchone()
    if pending:
        return False
    actor = {'kind':'human','name':'auto/delegation/' + str(provenance['delegation_id'])}
    _set_status(c, actor, row, 'done', 'Auto Task 결과를 승인하여 완료했어요.', provenance)
    return True


def complete_tree(actor, ref, note="") -> list[dict]:
    """묶음 전체 완료(DEV-44) — ref의 최상위 이슈와 그 아래 모든 이슈를 한 트랜잭션으로 done.
    이미 done/closed인 것은 그대로 둔다(거절해서 닫은 Task를 덮지 않게). Claude 실행 중인 이슈가 있으면 아무것도 바꾸지 않는다.
    사람만 — goal 라벨도 사람이 누른 것이니 닫는다. 바뀐 이슈 목록을 돌려준다."""
    if not _is_human(actor):
        raise _forbidden("done/closed는 사람이 확인하고 바꿔요 — 끝냈으면 in_review로 올려 주세요.")
    note = _text(note, "메모", 20_000)
    with db.connect() as c:
        row = _find(c, ref)
        root = row["id"]
        while (p := c.execute("SELECT parent_id FROM issues WHERE id=?", (root,)).fetchone()[0]) is not None:
            root = p
        ids = [r[0] for r in c.execute("WITH RECURSIVE t(id) AS (SELECT ? UNION SELECT i.id FROM issues i JOIN t ON i.parent_id=t.id) "
                                       "SELECT id FROM t", (root,))]
        marks = ",".join("?" * len(ids))
        busy = [r["ref"] for r in c.execute(f"SELECT DISTINCT {ref_sql('i', 'p')} AS ref FROM runs r JOIN issues i ON i.id=r.issue_id "
                                            f"JOIN projects p ON p.id=i.project_id WHERE r.status='running' AND i.id IN ({marks})", ids)]
        if busy:
            raise StoreError(f"Claude가 실행 중인 이슈가 있어요: {', '.join(busy)} — 끝난 뒤 다시 해 주세요.", 409)
        rows = c.execute(_ISSUE_SELECT + f" WHERE i.id IN ({marks}) AND i.status NOT IN ('done','closed') ORDER BY i.id", ids).fetchall()
        now = db.now_iso()
        body = note.strip() or f"전체 완료 ({row['ref']}에서 한 번에)"
        for r in rows:
            c.execute("UPDATE issues SET status='done', closed_at=?, updated_at=?, claimed_by=NULL, lease_until=NULL WHERE id=?", (now, now, r["id"]))
            _event(c, r["id"], actor, "status", body, {"from": r["status"], "to": "done", "tree_from": row["ref"]})
        return [_issue_dict(r) for r in c.execute(_ISSUE_SELECT + f" WHERE i.id IN ({','.join('?' * len(rows))}) ORDER BY i.id", [r["id"] for r in rows])]


def post_plan(actor, ref, body) -> dict:
    body = _text(body, "계획서", required=True)
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row = _find(c, ref)
        version = c.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM plans WHERE issue_id=?", (row["id"],)).fetchone()[0]
        now = db.now_iso()
        pid = c.execute("INSERT INTO plans(issue_id, version, body, author, created_at) VALUES(?,?,?,?,?)",
                        (row["id"], version, body, actor_label(actor), now)).lastrowid
        run = c.execute("SELECT id, mode, status FROM runs WHERE issue_id=? ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
        if run and run["mode"] == "review" and run["status"] == "running":
            c.execute("INSERT INTO review_plan_runs(plan_id,run_id) VALUES(?,?)", (pid, run["id"]))
        c.execute("UPDATE issues SET updated_at=? WHERE id=?", (now, row["id"]))
        _event(c, row["id"], actor, "plan", data={"version": version})
        return dict(c.execute("SELECT * FROM plans WHERE id=?", (pid,)).fetchone())


VERDICT_LABEL = {"approve": "승인", "approve_notes": "조건부 승인", "reject": "거절"}


def parse_tasks(body: str) -> list[dict]:
    """계획서의 `## Tasks` 절: `N. 제목 | 파일: … | 확인: … | 선행: 1, 2` 한 줄이 Task 하나."""
    m = re.search(r"^#{1,6}[ \t]*Tasks?[ \t]*$", body or "", re.M)
    if not m:
        return []
    rest = body[m.end():]
    nxt = re.search(r"^#{1,6}[ \t]", rest, re.M)
    out = []
    for line in (rest[:nxt.start()] if nxt else rest).splitlines():
        mm = re.match(r"\s*(\d+)[.)]\s+(.+)", line)
        if not mm:
            continue
        title, *fields = [x.strip() for x in mm.group(2).split("|")]
        info = {k.strip(): v.strip() for k, _, v in (f.partition(":") for f in fields)}
        out.append({"n": int(mm.group(1)), "title": title[:300], "files": info.get("파일", ""), "check": info.get("확인", ""),
                    "after": [int(x) for x in re.findall(r"\d+", info.get("선행", ""))]})
    return out


def _spawn_tasks(c, parent, plan_body, version, actor, note) -> int:
    """승인 시 계획서의 Task를 하위 이슈로 만들고 선후관계를 잇는다. 이미 같은 제목의 하위가 있으면 그것을 쓴다
    (같은 판 재승인·같은 Task를 다시 쓴 새 검토 판은 복제하지 않는다). 수정 요청 뒤 새 판의 새 Task는 이전 판의
    Task가 있어도 만든다(DEV-86 — v2 승인에 Task가 안 생겼다). 만든 개수를 돌려준다."""
    if parent["parent_id"]:
        return 0
    has_children = c.execute("SELECT 1 FROM issues WHERE parent_id=?", (parent["id"],)).fetchone()
    if (version == 1 or not parse_tasks(plan_body)) and has_children:
        return 0   # 첫 판인데 하위가 이미 있음(사람이 만든 Task), 또는 Tasks 절 없는 새 판(기존 Task를 이어 감) — 그대로 둔다
    # 계획서에 Tasks 절이 없으면(작은 일) 이슈 자체를 Task 하나로 — 실행은 Task에만 붙는다
    tasks = parse_tasks(plan_body) or [{"n": 1, "title": parent["title"], "files": "", "check": "", "after": []}]
    existing = {r["title"]: r["id"] for r in c.execute("SELECT id, title FROM issues WHERE parent_id=?", (parent["id"],))}
    proj = c.execute("SELECT * FROM projects WHERE id=?", (parent["project_id"],)).fetchone()
    now, ids, made, ref = db.now_iso(), {}, set(), _issue_dict(parent)["ref"]
    for t in tasks:
        if t["title"] in existing:
            ids[t["n"]] = existing[t["title"]]
            continue
        body = f"{ref} 계획서 v{version}의 Task {t['n']}."
        if t["files"]:
            body += f"\n\n**바꿀 파일**: {t['files']}"
        if t["check"]:
            body += f"\n\n**확인**: {t['check']}"
        if note.strip():
            body += f"\n\n**{ref}의 조건부 승인 메모(계획서보다 우선)**:\n{note.strip()}"
        number, sub_of, sub = _next_number(c, proj, parent)
        ids[t["n"]] = c.execute("INSERT INTO issues(project_id, number, sub_of, sub_number, parent_id, title, body, status, priority, labels_json, reporter, "
                                "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (proj["id"], number, sub_of, sub, parent["id"], t["title"], body, "backlog", "none", "[]", actor_label(actor), now, now)).lastrowid
        made.add(t["n"])
    for t in tasks:
        if t["n"] not in made:
            continue   # 기존 Task의 선후관계는 건드리지 않는다
        for a in t["after"]:
            if a in ids and a != t["n"]:
                c.execute("INSERT OR IGNORE INTO issue_deps(issue_id, blocked_by_id) VALUES(?,?)", (ids[t["n"]], ids[a]))
    return len(made)


def _record_plan_decision(c, row, latest, verdict, note, actor, provenance=None):
    """직렬화된 호출자 트랜잭션에서 결정과 Task 생성을 함께 기록한다."""
    c.execute("INSERT INTO decisions(issue_id, gate, plan_version, verdict, note, actor, created_at) VALUES(?,?,?,?,?,?,?)",
              (row["id"], "plan", latest, verdict, note, actor_label(actor), db.now_iso()))
    if verdict != "reject":   # 거절은 아래 상태 변경 이벤트가 사유를 담는다
        plan_body = c.execute("SELECT body FROM plans WHERE issue_id=? AND version=?", (row["id"], latest)).fetchone()[0]
        made = _spawn_tasks(c, row, plan_body, latest, actor, "" if provenance else note)
        label = "Auto 계획 승인" if provenance else VERDICT_LABEL[verdict]
        _event(c, row["id"], actor, "comment", f"**{label}** (계획서 v{latest})" + (f"\n\n{note}" if note.strip() else "")
               + (f"\n\n하위 Task {made}개를 만들었어요." if made else ""),
               {"decision": verdict, "plan_version": latest, **(provenance or {})})
        c.execute("UPDATE issues SET updated_at=? WHERE id=?", (db.now_iso(), row["id"]))


def decide(actor, ref, verdict, note="", plan_version=None) -> dict:
    """계획서에 대한 사람의 결정(게이트 1). 보고 있던 판(plan_version)이 최신이 아니면 409 — 안 본 계획서를 승인하지 않게.
    거절은 이슈를 closed로 닫는다(사유 = 메모). 승인은 기록만 한다 — 착수는 에이전트가 approval을 읽고 한다."""
    if not _is_human(actor):
        raise _forbidden("계획서 승인·거절은 사람만 할 수 있어요.")
    if verdict not in VERDICT_LABEL:
        raise StoreError(f"verdict는 {'/'.join(VERDICT_LABEL)} 중 하나예요.")
    note = _text(note, "메모", 20_000)
    if verdict != "approve" and not note.strip():
        raise StoreError(f"{VERDICT_LABEL[verdict]}은(는) 메모가 필요해요 — " + ("답하거나 고칠 내용을 적어 주세요." if verdict == "approve_notes" else "거절 이유를 적어 주세요."))
    with _terminal_change(actor, ref, verdict == 'reject'), db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row = _find(c, ref)
        latest = c.execute("SELECT MAX(version) FROM plans WHERE issue_id=?", (row["id"],)).fetchone()[0]
        if latest is None:
            raise StoreError("계획서가 없어서 결정할 수 없어요.", 409)
        if plan_version != latest:
            raise StoreError(f"보던 계획서(v{plan_version})가 최신(v{latest})이 아니에요 — 새로 고쳐서 다시 결정해 주세요.", 409)
        if row["status"] in HUMAN_ONLY_STATUSES:
            raise StoreError("끝난 이슈예요.", 409)
        _record_plan_decision(c, row, latest, verdict, note, actor)
        if verdict == "reject":
            _sync_terminal_status(c, actor, row, "closed", f"거절 (계획서 v{latest}): {note.strip()}")
    return get_issue(ref)


def list_plans(ref) -> list[dict]:
    with db.connect() as c:
        row = _find(c, ref)
        return [dict(p) for p in c.execute("SELECT * FROM plans WHERE issue_id=? ORDER BY version DESC", (row["id"],))]


def add_comment(actor, ref, body) -> dict:
    body = _text(body, "댓글", 50_000, required=True)
    with db.connect() as c:
        row = _find(c, ref)
        _event(c, row["id"], actor, "comment", body)
        c.execute("UPDATE issues SET updated_at=? WHERE id=?", (db.now_iso(), row["id"]))
        return _last_event(c, row["id"])


def link_commit(actor, ref, sha, repo="", message="") -> dict:
    sha = str(sha or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        raise StoreError("sha는 7~40자리 16진수예요.")
    with db.connect() as c:
        row = _find(c, ref)
        _event(c, row["id"], actor, "commit", _text(message, "커밋 메시지", 5_000),
               {"sha": sha, "repo": _text(repo, "저장소", 500)})
        c.execute("UPDATE issues SET updated_at=? WHERE id=?", (db.now_iso(), row["id"]))
        return _last_event(c, row["id"])


def _last_event(c, issue_id) -> dict:
    e = dict(c.execute("SELECT * FROM events WHERE issue_id=? ORDER BY id DESC LIMIT 1", (issue_id,)).fetchone())
    e["data"] = json.loads(e.pop("data_json"))
    return e


def claim(actor, ref, minutes=30) -> dict:
    minutes = max(1, min(int(minutes or 30), 240))
    who = actor_label(actor)
    now = _now()
    with db.connect() as c:
        row = _find(c, ref)
        if _lease_active(row, now) and row["claimed_by"] != who:
            raise StoreError(f"{row['claimed_by']}가 잡고 있어요(점유 만료 {row['lease_until']}).", 409)
        if row["status"] in ("done", "closed"):
            raise StoreError("끝난 이슈는 잡을 수 없어요.", 409)
        renew = _lease_active(row, now) and row["claimed_by"] == who
        until = (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")
        c.execute("UPDATE issues SET claimed_by=?, lease_until=? WHERE id=?", (who, until, row["id"]))
        if not renew:
            _event(c, row["id"], actor, "claim", data={"action": "claim", "minutes": minutes})
        return _issue_dict(c.execute(_ISSUE_SELECT + " WHERE i.id=?", (row["id"],)).fetchone())


def release(actor, ref) -> dict:
    who = actor_label(actor)
    with db.connect() as c:
        row = _find(c, ref)
        if not _lease_active(row):
            return _issue_dict(row)
        if row["claimed_by"] != who and not _is_human(actor):
            raise _forbidden("다른 actor가 잡은 이슈는 놓을 수 없어요.")
        c.execute("UPDATE issues SET claimed_by=NULL, lease_until=NULL WHERE id=?", (row["id"],))
        _event(c, row["id"], actor, "claim", data={"action": "release", "holder": row["claimed_by"]})
        return _issue_dict(c.execute(_ISSUE_SELECT + " WHERE i.id=?", (row["id"],)).fetchone())
