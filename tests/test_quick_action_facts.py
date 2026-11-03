"""빠른 액션 사실(DEV-243-1) — action_context가 목록·상세·child에서 같고 쿼리 수가 고정되며, 일반 검토 수락 조건과 재검토 경로를 임시 DB로 검사한다."""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix='dev-quick-action-'))
os.environ['DEV_DATA_DIR'] = str(TMP / 'data')
os.environ['DEV_DB_PATH'] = str(TMP / 'data' / 'dev.db')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))

import db  # noqa: E402
import issues  # noqa: E402
import jobs  # noqa: E402

db.init()
jobs.kick = lambda *a, **k: None   # 대기열에 넣기만 보고 실제 검토(claude)는 돌리지 않는다
HUMAN = {'kind': 'human', 'name': 'admin'}
FAILS = []


def check(cond, label):
    print(('ok   ' if cond else 'FAIL ') + label)
    if not cond:
        FAILS.append(label)


def sql(query, args=()):
    with db.connect() as c:
        return c.execute(query, args).lastrowid


def status(it, value):
    sql("UPDATE issues SET status=? WHERE id=?", (value, it['id']))


def plan(iid, version, body="# 계획\n\n## Tasks\n1. 하나 | 파일: a.py\n"):
    sql("INSERT INTO plans(issue_id, version, body, author, created_at) VALUES(?,?,?,?,?)", (iid, version, body, 'agent:1', db.now_iso()))


def decide(iid, version, verdict):
    sql("INSERT INTO decisions(issue_id, gate, plan_version, verdict, note, actor, created_at) VALUES(?,?,?,?,?,?,?)",
        (iid, 'plan', version, verdict, '', 'human:admin', db.now_iso()))


def claim(it, minutes=30):
    until = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec='seconds')
    sql("UPDATE issues SET claimed_by='agent:7', lease_until=? WHERE id=?", (until, it['id']))


def event(iid, body, data):
    sql("INSERT INTO events(issue_id, actor, kind, body, data_json, created_at) VALUES(?,?,?,?,?,?)",
        (iid, 'human:admin', 'comment', body, json.dumps(data, ensure_ascii=False), db.now_iso()))


def enqueue(it, **kw):
    try:
        return jobs.enqueue(HUMAN, it['ref'], 'review', 'claude', **kw), None
    except issues.StoreError as e:
        return None, (e.status, str(e))


issues.create_project(HUMAN, 'QA', '빠른 액션')
parent = issues.create_issue(HUMAN, 'QA', '부모 — 승인됨')
plan(parent['id'], 1)
decide(parent['id'], 1, 'approve')
status(parent, 'in_progress')
t1 = issues.create_issue(HUMAN, 'QA', 'Task 수정 요청', parent=parent['ref'])
t2 = issues.create_issue(HUMAN, 'QA', 'Task 결과 대기', parent=parent['ref'])
t3 = issues.create_issue(HUMAN, 'QA', 'Task 대체됨', parent=parent['ref'])
status(t1, 'changes_requested')
status(t2, 'in_review')
event(t3['id'], '보관', {'superseded_by_plan': 2})
sql("INSERT INTO jobs(issue_id, mode, actor, status, created_at, provider) VALUES(?,?,?,?,?,?)",
    (t1['id'], 'execute', 'human:admin', 'queued', db.now_iso(), 'claude'))

ns116 = issues.create_issue(HUMAN, 'QA', 'NS-116 — 계획서 있음, 승인 없음')
plan(ns116['id'], 1)
claimed = issues.create_issue(HUMAN, 'QA', '대화형 세션이 잡고 있음')
claim(claimed)
status(claimed, 'in_progress')
expired = issues.create_issue(HUMAN, 'QA', '점유 만료')
claim(expired, -5)
revised = issues.create_issue(HUMAN, 'QA', '재검토 요청')
plan(revised['id'], 1)
event(revised['id'], '다시', {'plan_revision_request': True, 'plan_version': 1})
manual = issues.create_issue(HUMAN, 'QA', '계획서 없이 Task만')
mt = issues.create_issue(HUMAN, 'QA', '대화형 세션이 만든 Task', parent=manual['ref'])
closed = issues.create_issue(HUMAN, 'QA', '닫힘')
status(closed, 'closed')
fresh = issues.create_issue(HUMAN, 'QA', '새 이슈')

# (a) 목록·상세·child가 같은 action_context
listed = {it['ref']: it['action_context'] for it in issues.list_issues(project='QA')}
detail = issues.get_issue(parent['ref'])
for it in (parent, ns116, claimed, expired, revised, manual, closed, fresh, t1, t2, mt):
    check(listed[it['ref']] == issues.get_issue(it['ref'])['action_context'], f"{it['ref']} 목록·상세 action_context가 같다")
for ch in detail['children']:
    check(ch['action_context'] == listed[ch['ref']], f"{ch['ref']} child·목록 action_context가 같다")
check(t3['ref'] not in [ch['ref'] for ch in detail['children']] and issues.get_issue(t3['ref'])['action_context']['superseded'] is True,
      '대체된 Task는 child에서 빠지고 superseded')

keys = {'plan_version', 'approval', 'revision_requested', 'has_children', 'has_execution', 'has_active_job',
        'claim_active', 'claimed_by', 'lease_until', 'superseded', 'parent'}
check(set(listed[fresh['ref']]) == keys, f"action_context 키 ({sorted(listed[fresh['ref']])})")
p = listed[parent['ref']]
check(p['has_children'] and p['approval']['verdict'] == 'approve' and p['parent'] is None, '부모 사실 — Task 있음·승인')
check(listed[t1['ref']]['has_active_job'] and listed[t1['ref']]['parent']['plan_version'] == 1, 'Task의 대기 job과 부모 사실')
check(listed[t2['ref']]['superseded'] is False and listed[t2['ref']]['parent']['approval']['stale'] is False, 'Task의 부모 승인 유효')
c_ = listed[claimed['ref']]
check(c_['claim_active'] and c_['claimed_by'] == 'agent:7' and c_['lease_until'], '활성 점유 사실')
e_ = listed[expired['ref']]
check(not e_['claim_active'] and e_['claimed_by'] is None and e_['lease_until'] is None, '만료된 점유는 사실에 없다')
check(listed[revised['ref']]['revision_requested'] is True and listed[ns116['ref']]['revision_requested'] is False,
      '재검토 요청은 결정이 없어도 최상위 키로')
check(listed[ns116['ref']]['plan_version'] == 1 and listed[ns116['ref']]['approval'] is None, 'NS-116 — 계획서 v1·승인 없음')
check(listed[manual['ref']]['has_children'] and listed[manual['ref']]['plan_version'] is None, '계획서 없이 Task 있음')
with db.connect() as c:
    only_sup = issues._action_facts(c, [issues._find(c, t3['ref'])])
check(only_sup[t3['id']]['parent']['has_children'], '부모 has_children은 대체되지 않은 Task로 센다')
lone = issues.create_issue(HUMAN, 'QA', '대체된 Task만 있는 부모')
lt = issues.create_issue(HUMAN, 'QA', '대체됨', parent=lone['ref'])
event(lt['id'], '보관', {'superseded_by_plan': 2})
check(issues.get_issue(lone['ref'])['action_context']['has_children'] is False, '대체된 Task만 있으면 has_children은 거짓')

# (b) 쿼리 수 고정 — _action_facts와 목록이 행 수와 무관하다
for n in range(15):
    issues.create_issue(HUMAN, 'QA', f'채움 {n}', parent=parent['ref'] if n % 2 else None)


def count_selects(fn):
    seen = []
    with db.connect() as c:
        rows = c.execute(issues._ISSUE_SELECT + " WHERE p.key='QA' ORDER BY i.id").fetchall()
        c.set_trace_callback(lambda s: seen.append(s) if s.lstrip().upper().startswith(('SELECT', 'WITH')) else None)
        fn(c, rows)
    return len(seen)


small = count_selects(lambda c, rows: issues._action_facts(c, rows[:2]))
large = count_selects(lambda c, rows: issues._action_facts(c, rows[:25]))
check(small == large, f'_action_facts 쿼리 수가 행 수와 무관하다 ({small}, {large})')

# (c) 일반 검토 수락 조건 — 계획서·점유·Task·종결이면 409, 계획서 없음+Task 있음은 접수
for it, why in ((ns116, '계획서가 있어요'), (claimed, '잡고 있어요'), (t2, 'Task'), (closed, None)):
    ok, err = enqueue(it)
    check(ok is None and err and err[0] in (409, 403) and (why is None or why in err[1]), f"{it['ref']} 일반 검토 거절 ({err})")
ok, err = enqueue(manual)
check(ok and (ok.get('queued') or ok.get('started')), f'계획서 없음+Task 있음은 접수 ({ok or err})')
ok, err = enqueue(expired)
check(ok and (ok.get('queued') or ok.get('started')), f'점유가 만료되면 접수 ({ok or err})')
ok, err = enqueue(fresh)
check(ok and (ok.get('queued') or ok.get('started')), f'새 이슈는 접수 ({ok or err})')
again, err = enqueue(fresh)
check(again and again.get('job_id') == ok.get('job_id'), f'이미 줄에 선 검토는 다시 눌러도 같은 자리 ({again or err})')

# (d) 메모 붙여 재검토는 계획서가 있어도 접수
ok, err = enqueue(ns116, note='범위를 줄여 주세요', plan_version=1)
check(ok and (ok.get('queued') or ok.get('started')), f'메모 붙여 재검토는 계획서가 있어도 접수 ({ok or err})')
check(issues.get_issue(ns116['ref'])['action_context']['revision_requested'] is True, '재검토 뒤 revision_requested')

print(f"\n{'실패 ' + str(len(FAILS)) + '개' if FAILS else '모두 통과'}")
sys.exit(1 if FAILS else 0)
