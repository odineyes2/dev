"""프로젝트별 자동화 설정과 관리자 위임 이력을 저장한다."""
import json

import db
import model_catalog
from issues import StoreError, actor_label

PROVIDERS = ("claude", "codex")
ORDER_FIELDS = ("provider_order", "orchestrator_provider_order", "troubleshooter_provider_order", "description_provider_order")
BOOL_FIELDS = ("auto_review", "auto_execute", "auto_approve", "auto_plan_approve", "token_exhaustion_fallback")
# agent_orders: {역할: [Agent id, …]} — 저장하면 그 역할의 provider 순서도 처음 나오는 provider 순으로 맞춘다.
FIELDS = {"auto_review", "auto_execute", "auto_approve", "auto_plan_approve", "provider_order", "orchestrator_provider_order", "troubleshooter_provider_order", "description_provider_order", "token_exhaustion_fallback", "agent_orders"}


def _project(c, key):
    row = c.execute("SELECT id, key FROM projects WHERE key=?", (key.upper(),)).fetchone()
    if row is None:
        raise StoreError("없는 프로젝트예요.", 404)
    return row


def _candidates(c):
    """우선순위 후보 — 켜져 있고 vendor·model이 카탈로그 안인 Agent(id 순)."""
    allowed = {v["vendor"]: (v["provider"], {m["id"] for m in v["models"]}) for v in model_catalog.VENDORS}
    out = []
    for a in c.execute("SELECT id, name, vendor, model FROM agents WHERE enabled=1 ORDER BY id"):
        provider, models = allowed.get(a["vendor"], (None, set()))
        if a["model"] in models:
            out.append({"id": a["id"], "name": a["name"], "vendor": a["vendor"], "model": a["model"], "provider": provider})
    return out


def _agent_order(c, project, role, provider_order, candidates):
    """저장한 순서에서 빠진 Agent를 지우고, 새 Agent는 provider 순서 → id 순으로 끝에 붙인다."""
    row = c.execute("SELECT agent_ids_json FROM project_agent_orders WHERE project_id=? AND role=?",
                    (project["id"], role)).fetchone()
    saved = json.loads(row["agent_ids_json"]) if row else []
    by_id = {a["id"]: a for a in candidates}
    rest = sorted((a for a in candidates if a["id"] not in saved), key=lambda a: provider_order.index(a["provider"]))
    return [by_id[i] for i in saved if i in by_id] + rest


def _settings(c, project):
    row = c.execute("SELECT * FROM project_auto_settings WHERE project_id=?", (project["id"],)).fetchone()
    roles = c.execute("SELECT * FROM project_auto_role_settings WHERE project_id=?", (project["id"],)).fetchone()
    result = _base_settings(row, roles, project)
    candidates = _candidates(c)
    result["agent_orders"] = {f: _agent_order(c, project, f, result[f], candidates) for f in ORDER_FIELDS}
    return result


def model_for(c, issue_id, provider):
    """이슈의 역할(Description 요청이면 description, 아니면 검토·실행) 순서에서 그 provider의 첫 Agent 모델. 없으면 None."""
    project = c.execute("SELECT p.id, p.key FROM projects p JOIN issues i ON i.project_id=p.id WHERE i.id=?", (issue_id,)).fetchone()
    described = c.execute("SELECT 1 FROM project_description_requests WHERE issue_id=?", (issue_id,)).fetchone()
    role = "description_provider_order" if described else "provider_order"
    agent = next((a for a in _settings(c, project)["agent_orders"][role] if a["provider"] == provider), None)
    return agent["model"] if agent else None


def _base_settings(row, roles, project):
    return {
        "project_key": project["key"],
        "auto_review": bool(row["auto_review"]) if row else False,
        "auto_execute": bool(row["auto_execute"]) if row else False,
        "auto_approve": bool(row["auto_approve"]) if row else False,
        "provider_order": json.loads(row["provider_order_json"]) if row else list(PROVIDERS),
        "orchestrator_provider_order": json.loads(roles["orchestrator_provider_order_json"]) if roles else list(PROVIDERS),
        "troubleshooter_provider_order": json.loads(roles["troubleshooter_provider_order_json"]) if roles else list(PROVIDERS),
        "description_provider_order": json.loads(roles["description_provider_order_json"]) if roles else list(PROVIDERS),
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
        changes = dict(changes)
        agent_orders = changes.pop("agent_orders", {})
        if not isinstance(agent_orders, dict) or set(agent_orders) - set(ORDER_FIELDS):
            raise StoreError("agent_orders 역할이 맞지 않아요.")
        for role, ids in agent_orders.items():
            current = before["agent_orders"][role]
            if (not isinstance(ids, list) or any(type(i) is not int for i in ids)
                    or sorted(ids) != sorted(a["id"] for a in current)):
                raise StoreError(f"{role} Agent 순서는 지금 후보 Agent를 한 번씩 포함해야 해요. 새로고침 후 다시 시도해 주세요.")
            by_id = {a["id"]: a for a in current}
            providers = list(dict.fromkeys(by_id[i]["provider"] for i in ids))
            changes[role] = providers + [p for p in before[role] if p not in providers]
        for role in ORDER_FIELDS:
            if role in agent_orders:
                c.execute("""INSERT INTO project_agent_orders(project_id,role,agent_ids_json) VALUES(?,?,?)
                    ON CONFLICT(project_id,role) DO UPDATE SET agent_ids_json=excluded.agent_ids_json""",
                    (project["id"], role, json.dumps(agent_orders[role])))
            elif role in changes and changes[role] != before[role]:
                # 예전 provider 순서만 바꾸면 Agent 순서는 그 provider 순서로 다시 만든다.
                c.execute("DELETE FROM project_agent_orders WHERE project_id=? AND role=?", (project["id"], role))
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
            (project_id,orchestrator_provider_order_json,troubleshooter_provider_order_json,token_exhaustion_fallback,
             description_provider_order_json)
            VALUES(?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET
            orchestrator_provider_order_json=excluded.orchestrator_provider_order_json,
            troubleshooter_provider_order_json=excluded.troubleshooter_provider_order_json,
            token_exhaustion_fallback=excluded.token_exhaustion_fallback,
            description_provider_order_json=excluded.description_provider_order_json""",
            (project["id"],
             json.dumps(updated["orchestrator_provider_order"], separators=(",", ":")),
             json.dumps(updated["troubleshooter_provider_order"], separators=(",", ":")),
             updated["token_exhaustion_fallback"],
             json.dumps(updated["description_provider_order"], separators=(",", ":"))))
        after = _settings(c, project)
        c.execute("""INSERT INTO project_auto_settings_events
            (project_id,actor,before_json,after_json,created_at,plan_id_floor)
            VALUES(?,?,?,?,?,(SELECT COALESCE(MAX(id),0) FROM plans))""",
            (project["id"], who, json.dumps(before, ensure_ascii=False),
             json.dumps(after, ensure_ascii=False), now))
        return after
