"""이슈 주기 요약(DEV-240-1) — lifecycle_facts가 목록·상세에서 같고, 실패 검사 label·실행 경과·쿼리 수 고정을 임시 DB로 검사한다."""
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix='dev-lifecycle-'))
os.environ['DEV_DATA_DIR'] = str(TMP / 'data')
os.environ['DEV_DB_PATH'] = str(TMP / 'data' / 'dev.db')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))

import db  # noqa: E402
import issues  # noqa: E402
import run_progress  # noqa: E402
import task_checks  # noqa: E402

db.init()
HUMAN = {'kind': 'human', 'name': 'admin'}
FAILS = []


def check(cond, label):
    print(('ok   ' if cond else 'FAIL ') + label)
    if not cond:
        FAILS.append(label)


def sql(query, args=()):
    with db.connect() as c:
        return c.execute(query, args).lastrowid


def status(ref, value):
    sql("UPDATE issues SET status=? WHERE id=?", (value, issues.get_issue(ref)['id']))


def run(iid, mode, state, provider='claude'):
    sql("INSERT INTO runs(issue_id, mode, status, actor, started_at, provider) VALUES(?,?,?,?,?,?)",
        (iid, mode, state, 'human:admin', db.now_iso(), provider))


def event(iid, kind, body='', data=None):
    sql("INSERT INTO events(issue_id, actor, kind, body, data_json, created_at) VALUES(?,?,?,?,?,?)",
        (iid, 'human:admin', kind, body, json.dumps(data or {}, ensure_ascii=False), db.now_iso()))


def plan(iid, version, body):
    sql("INSERT INTO plans(issue_id, version, body, author, created_at) VALUES(?,?,?,?,?)", (iid, version, body, 'agent:1', db.now_iso()))


def decide(iid, version, verdict, note=''):
    sql("INSERT INTO decisions(issue_id, gate, plan_version, verdict, note, actor, created_at) VALUES(?,?,?,?,?,?,?)",
        (iid, 'plan', version, verdict, note, 'human:admin', db.now_iso()))


TASKS_BODY = "# 계획\n\n## Tasks\n1. 하나 | 파일: a.py\n2. 둘 | 파일: b.py\n"

issues.create_project(HUMAN, 'LC', '주기')
parent = issues.create_issue(HUMAN, 'LC', '부모')
plan(parent['id'], 1, TASKS_BODY)
plan(parent['id'], 2, TASKS_BODY)
decide(parent['id'], 2, 'approve')
old = issues.create_issue(HUMAN, 'LC', '대체된 Task', parent=parent['ref'])
done = issues.create_issue(HUMAN, 'LC', 'Task done', parent=parent['ref'])
busy = issues.create_issue(HUMAN, 'LC', 'Task 실행 중', parent=parent['ref'])
idle = issues.create_issue(HUMAN, 'LC', 'Task 실패', parent=parent['ref'])
event(old['id'], 'comment', '보관', {'superseded_by_plan': 2})
run(old['id'], 'execute', 'failed')
status(done['ref'], 'done')
status(busy['ref'], 'in_progress')
for state in ('ok', 'ok', 'failed'):
    run(idle['id'], 'execute', state)
status(parent['ref'], 'changes_requested')
event(parent['id'], 'status', '모바일에서 라벨이 잘려요', {'from': 'in_review', 'to': 'changes_requested'})
run(busy['id'], 'execute', 'running', 'codex')
run_progress.set_phase(busy['ref'], 'implement', provider='Codex')
run_progress._runs[busy['ref']]['since'] -= 100
run_progress._runs[busy['ref']]['started'] -= 100
run_progress.set_phase(busy['ref'], 'rework', provider='Codex', failed=['python -m pytest tests/test_qa.py'])

reviewing = issues.create_issue(HUMAN, 'LC', '검토 중')
run(reviewing['id'], 'review', 'running')
planned = issues.create_issue(HUMAN, 'LC', '계획서만')
plan(planned['id'], 1, TASKS_BODY)
rejected = issues.create_issue(HUMAN, 'LC', '거절')
plan(rejected['id'], 1, TASKS_BODY)
decide(rejected['id'], 1, 'reject', 'DEV-238 방식으로 대체')
status(rejected['ref'], 'closed')


def strip(facts):
    return {**facts, 'running': {k: v for k, v in facts['running'].items() if k != 'seconds'} if facts['running'] else None}


listed = {it['ref']: it['lifecycle_facts'] for it in issues.list_issues(project='LC')}
check(old['ref'] not in listed, '대체된 Task는 목록에서 빠진다')
for it in (parent, done, busy, idle, reviewing, planned, rejected):
    check(strip(listed[it['ref']]) == strip(issues.get_issue(it['ref'])['lifecycle_facts']), f"{it['ref']} 목록·상세 lifecycle_facts가 같다")

f = listed[parent['ref']]
check([t['ref'] for t in f['tasks']] == [done['ref'], busy['ref'], idle['ref']], 'tasks는 id 순이고 대체된 Task가 빠진다')
check([t['sub_number'] for t in f['tasks']] == [2, 3, 4] and [t['status'] for t in f['tasks']] == ['done', 'in_progress', 'backlog'], 'tasks의 sub_number·status')
check(f['failed_tasks'] == 1, f"failed_tasks == 1 ({f['failed_tasks']})")
check(f['loops'] == 4, f"loops == 2 + 1 + 1 == 4 ({f['loops']})")
r = f['running'] or {}
check({k: r.get(k) for k in ('mode', 'provider', 'ref', 'sub_number', 'phase', 'failed')} ==
      {'mode': 'execute', 'provider': 'codex', 'ref': busy['ref'], 'sub_number': 3, 'phase': 'rework', 'failed': ['python -m pytest tests/test_qa.py']},
      f'부모 running은 도는 Task의 재작업 ({r})')
check(100 <= r.get('seconds', 0) < 160, f"running.seconds는 첫 set_phase부터 잰다 ({r.get('seconds')})")
check(f['note'] == '모바일에서 라벨이 잘려요', '수정 요청 메모가 note에 실린다')
check(f['parent_plan_version'] is None and f['planned_tasks'] is None, '부모 이슈는 parent_plan_version·planned_tasks가 없다')

check(listed[busy['ref']]['parent_plan_version'] == 2 and listed[busy['ref']]['tasks'] == [], 'Task 이슈는 부모 최신 판을 싣고 tasks는 비었다')
check(listed[busy['ref']]['running']['phase'] == 'rework' and listed[busy['ref']]['loops'] == 1, 'Task 이슈 자신의 running·loops')
check(listed[idle['ref']]['failed_tasks'] == 1 and listed[idle['ref']]['loops'] == 2, 'Task 이슈는 자기 실행만 본다')
check(listed[done['ref']]['failed_tasks'] == 0 and listed[done['ref']]['running'] is None, 'done Task는 실패·실행이 없다')
rv = listed[reviewing['ref']]['running'] or {}
check(rv.get('mode') == 'review' and rv.get('ref') == reviewing['ref'] and rv.get('phase') == 'implement', f'검토 중 이슈의 running.mode == review ({rv})')
check(listed[planned['ref']]['planned_tasks'] == 2, '계획서만 있는 이슈의 planned_tasks == 2')
check(listed[rejected['ref']]['note'] == 'DEV-238 방식으로 대체', '거절 닫힘 이슈의 note는 거절 사유')
check(listed[rejected['ref']]['planned_tasks'] == 2 and listed[reviewing['ref']]['note'] == '', '다른 값은 비어 있다')

report, ok = task_checks._report([
    {'cmd': '문법 검사', 'ok': True, 'seconds': 0.1, 'always': True},
    {'cmd': '비밀값 검사', 'ok': False, 'seconds': 0.1, 'always': True, 'detail': 'a.py:3', 'output': 'x'},
    {'cmd': 'python -m pytest tests/test_qa.py', 'ok': False, 'seconds': 2.0, 'output': 'boom · E'},
    {'cmd': 'node --check static/app.js', 'ok': True, 'seconds': 0.2},
])
check(not ok and run_progress.failed_checks(report) == ['비밀값 검사', 'python -m pytest tests/test_qa.py'],
      f'failed_checks가 실제 보고에서 실패 label만 뽑는다 ({run_progress.failed_checks(report)})')
check(run_progress.failed_checks('') == [], '빈 보고는 빈 목록')
view = run_progress.view(busy['ref'])
check(view['failed'] == ['python -m pytest tests/test_qa.py'] and view['seconds'] >= 100, 'view가 failed·seconds를 돌려준다')
run_progress.clear(busy['ref'])
run_progress.set_phase(busy['ref'], 'implement')
check(run_progress.view(busy['ref'])['seconds'] < 5, 'clear 뒤 새 실행은 started를 새로 잡는다')
run_progress.clear(busy['ref'])

# 쿼리 수 고정 — 행 2개와 20개에서 list_issues의 SELECT 수가 같다
for n in range(15):
    issues.create_issue(HUMAN, 'LC', f'채움 {n}')
original = db.connect


@contextmanager
def counting():
    with original() as c:
        c.set_trace_callback(lambda s: COUNT.append(s) if s.lstrip().upper().startswith(('SELECT', 'WITH')) else None)
        yield c


db.connect = counting
counts = {}
for limit in (2, 20):
    COUNT = []
    rows = issues.list_issues(project='LC', limit=limit, sort='id_asc')
    counts[limit] = (len(rows), len(COUNT))
db.connect = original
check(counts[2][0] == 2 and counts[20][0] == 20 and counts[2][1] == counts[20][1], f'목록 쿼리 수가 행 수와 무관하다 ({counts})')

print(f"\n{'실패 ' + str(len(FAILS)) + '개' if FAILS else '모두 통과'}")
sys.exit(1 if FAILS else 0)
