"""프로젝트별 자동화 설정과 관리자 위임 이력을 저장한다."""
import json

import db
from issues import StoreError, actor_label

PROVIDERS = ("claude", "codex")
ORDER_FIELDS = ("provider_order", "orchestrator_provider_order", "troubleshooter_provider_order")
BOOL_FIELDS = ("auto_review", "auto_execute", "auto_approve", "auto_plan_approve", "token_exhaustion_fallback")
FIELDS = {"auto_review", "auto_execute", "auto_approve", "auto_plan_approve", "provider_order", "orchestrator_provider_order", "troubleshooter_provider_order", "token_exhaustion_fallback"}


def _project(c, key):
    row = c.execute("SELECT id, key FROM projects WHERE key=?", (key.upper(),)).fetchone()
    if row is None:
        raise StoreError("없는 프로젝트예요.", 404)
    return row


def _settings(c, project):
    row = c.execute("SELECT * FROM project_auto_settings WHERE project_id=?", (project["id"],)).fetchone()
    roles = c.execute("SELECT * FROM project_auto_role_settings WHERE project_id=?", (project["id"],)).fetchone()
    return {
        "project_key": project["key"],
        "auto_review": bool(row["auto_review"]) if row else False,
        "auto_execute": bool(row["auto_execute"]) if row else False,
        "auto_approve": bool(row["auto_approve"]) if row else False,
        "provider_order": json.loads(row["provider_order_json"]) if row else list(PROVIDERS),
        "orchestrator_provider_order": json.loads(roles["orchestrator_provider_order_json"]) if roles else list(PROVIDERS),
        "troubleshooter_provider_order": json.loads(roles["troubleshooter_provider_order_json"]) if roles else list(PROVIDERS),
        "token_exhaustion_fallback": bool(roles["token_exhaustion_fallback"]) if roles else False,
        "auto_plan_approve": bool(row["auto_plan_approve"]) if row else False,
        "auto_plan_approve_available": True,
        "auto_approve_available": True,
        "auto_approve_disabled_reason": "",
        "updated_by": row["updated_by"] if row else None,
        "updated_at": row["updated_at"] if row else None,
    }


def get_settings(key):
    with db.connect() as c:
        return _settings(c, _project(c, key))


def update_settings(actor, key, changes):
    if not actor or actor.get("kind") != "human":
        raise StoreError("설정 변경은 사람(관리자)만 할 수 있어요.", 403)
    if not isinstance(changes, dict) or set(changes) - FIELDS:
        raise StoreError("자동화 설정 항목이 맞지 않아요.")
    for field in BOOL_FIELDS:
        if field in changes and type(changes[field]) is not bool:
            raise StoreError(f"{field}는 boolean이어야 해요.")
    for field in ORDER_FIELDS:
        if field in changes:
            order = changes[field]
            if not isinstance(order, list) or order not in [list(PROVIDERS), list(reversed(PROVIDERS))]:
                raise StoreError(f"{field} 순서는 claude와 codex를 한 번씩 포함해야 해요.")
    with db.connect() as c:
        # 읽기와 쓰기를 직렬화해 동시 부분 수정과 감사 기록의 일관성을 유지한다.
        c.execute("BEGIN IMMEDIATE")
        project = _project(c, key)
        before = _settings(c, project)
        if not changes:
            return before
        updated = {**before, **changes}
        who, now = actor_label(actor), db.now_iso()
        c.execute("""INSERT INTO project_auto_settings
            (project_id,auto_review,auto_execute,auto_approve,provider_order_json,updated_by,updated_at,auto_plan_approve)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET
            auto_review=excluded.auto_review, auto_execute=excluded.auto_execute,
            auto_approve=excluded.auto_approve, auto_plan_approve=excluded.auto_plan_approve,
            provider_order_json=excluded.provider_order_json,
            updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
            (project["id"], updated["auto_review"], updated["auto_execute"], updated["auto_approve"],
             json.dumps(updated["provider_order"], separators=(",", ":")), who, now, updated["auto_plan_approve"]))
        c.execute("""INSERT INTO project_auto_role_settings
            (project_id,orchestrator_provider_order_json,troubleshooter_provider_order_json,token_exhaustion_fallback)
            VALUES(?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET
            orchestrator_provider_order_json=excluded.orchestrator_provider_order_json,
            troubleshooter_provider_order_json=excluded.troubleshooter_provider_order_json,
            token_exhaustion_fallback=excluded.token_exhaustion_fallback""",
            (project["id"],
             json.dumps(updated["orchestrator_provider_order"], separators=(",", ":")),
             json.dumps(updated["troubleshooter_provider_order"], separators=(",", ":")),
             updated["token_exhaustion_fallback"]))
        after = _settings(c, project)
        c.execute("""INSERT INTO project_auto_settings_events
            (project_id,actor,before_json,after_json,created_at,plan_id_floor)
            VALUES(?,?,?,?,?,(SELECT COALESCE(MAX(id),0) FROM plans))""",
            (project["id"], who, json.dumps(before, ensure_ascii=False),
             json.dumps(after, ensure_ascii=False), now))
        return after
