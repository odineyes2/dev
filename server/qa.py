"""??? Issue? QA ??? ????? ?? ??? ????."""
import json
import hashlib
import subprocess

import db
import issues


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def snapshot(c, issue_id):
    """판정 대상을 고정한다. QA 자신의 댓글·사용량은 키에 포함하지 않는다."""
    row = c.execute(f"SELECT i.*,p.key,p.local_path,{issues.ref_sql('i','p')} AS ref FROM issues i JOIN projects p ON p.id=i.project_id WHERE i.id=?", (issue_id,)).fetchone()
    if not row or row['parent_id'] is not None or row['status'] != 'in_review':
        raise ValueError('최상위 In Review Issue만 QA할 수 있어요.')
    import os
    base = subprocess.run(['git', 'rev-parse', os.environ.get('DEV_EXEC_BASE') or 'main'],
                          cwd=row['local_path'], capture_output=True, text=True, check=True, timeout=30).stdout.strip()
    family = [dict(r) for r in c.execute(f"""WITH RECURSIVE family(id) AS (
        SELECT ? UNION SELECT i.id FROM issues i JOIN family f ON i.parent_id=f.id)
        SELECT i.id,i.parent_id,i.title,i.body,i.status,i.labels_json,{issues._SUPERSEDED} AS superseded_by_plan,
        {issues.ref_sql('i','p')} AS ref FROM issues i JOIN projects p ON p.id=i.project_id
        WHERE i.id IN (SELECT id FROM family) AND superseded_by_plan IS NULL ORDER BY i.id""", (issue_id,))]
    ids = [r['id'] for r in family]
    marks = ','.join('?' * len(ids))
    event = c.execute("SELECT MAX(id) FROM events WHERE issue_id=? AND kind='status'", (issue_id,)).fetchone()[0]
    plan = c.execute('SELECT * FROM plans WHERE issue_id=? ORDER BY version DESC LIMIT 1', (issue_id,)).fetchone()
    decisions = [dict(r) for r in c.execute("SELECT * FROM decisions WHERE issue_id=? AND gate='plan' ORDER BY id", (issue_id,))]
    runs = [dict(r) for r in c.execute(f"SELECT id,issue_id,provider,model,status FROM runs WHERE mode='execute' AND id IN (SELECT MAX(id) FROM runs WHERE mode='execute' AND issue_id IN ({marks}) GROUP BY issue_id) ORDER BY id", ids)]
    results = [dict(r) for r in c.execute(f"SELECT e.* FROM execution_completion e WHERE issue_id IN ({marks}) ORDER BY run_id", ids)]
    commits = [dict(r) for r in c.execute(f"SELECT id,issue_id,body,data_json FROM events WHERE issue_id IN ({marks}) AND kind='commit' ORDER BY id", ids)]
    import auto_settings
    settings = auto_settings._settings(c, {'key': row['key']})
    value = {'issue_ref': row['ref'], 'issue_id': issue_id, 'project_key': row['key'],
             'local_path': row['local_path'], 'base_sha': base, 'status_event': event,
             'family': family, 'plan': dict(plan) if plan else None, 'decisions': decisions,
             'implementations': runs, 'completions': results, 'commits': commits,
             'auto_qa': settings['auto_qa'], 'settings_updated_at': settings['updated_at'],
             'check_settings': {r['key']:r['value'] for r in c.execute("SELECT key,value FROM app_settings WHERE key LIKE 'regression:%'")}}
    value['requirements'] = requirements(value)
    return value


def requirements(value):
    """본문의 각 비어 있지 않은 줄과 Plan 확인 항목을 빠짐없이 고정한다."""
    out = []
    for n, line in enumerate(value['family'][0]['body'].splitlines(), 1):
        if line.strip() and not line.lstrip().startswith('#'):
            out.append({'id': f'body:{n}', 'text': line.strip()})
    checking = False
    for n, line in enumerate((value['plan'] or {}).get('body', '').splitlines(), 1):
        if line.startswith('#'):
            checking = '확인' in line
        elif line.strip() and (checking or '| 확인:' in line):
            out.append({'id': f'plan:{n}', 'text': line.strip()})
    return out


def key(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def sync():
    """수동·서버 상태 전환을 같은 영속 대기 항목으로 수집한다."""
    with db.connect() as c:
        # 재작업 묶음이 끝나기 전에는 중간 상태를 다시 검사하지 않는다.
        refs = [r[0] for r in c.execute("""SELECT id FROM issues i WHERE parent_id IS NULL AND status='in_review'
            AND NOT EXISTS(SELECT 1 FROM qa_rework_batches b WHERE b.issue_id=i.id AND b.status='registered')""")]
    for issue_id in refs:
        try:
            with db.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                value = snapshot(c, issue_id)
                c.execute("INSERT OR IGNORE INTO qa_rounds(issue_id,round_number,snapshot_key,snapshot_json,created_at) VALUES(?,(SELECT COALESCE(MAX(round_number),0)+1 FROM qa_rounds WHERE issue_id=?),?,?,?)",
                          (issue_id, issue_id, key(value), _json(value), db.now_iso()))
        except (ValueError, OSError, subprocess.SubprocessError) as e:
            # 잘못된 저장소는 유료 호출 없이 판정 불가로 기록한다.
            with db.connect() as c:
                row = c.execute('SELECT status FROM issues WHERE id=?', (issue_id,)).fetchone()
                event = c.execute("SELECT MAX(id) FROM events WHERE issue_id=? AND kind='status'", (issue_id,)).fetchone()[0]
                if row and row['status'] == 'in_review':
                    c.execute("INSERT OR IGNORE INTO qa_rounds(issue_id,round_number,snapshot_key,snapshot_json,status,note,created_at,ended_at) VALUES(?,(SELECT COALESCE(MAX(round_number),0)+1 FROM qa_rounds WHERE issue_id=?),?,'{}','inconclusive',?,?,?)",
                              (issue_id, issue_id, f'unavailable:{event}', '검사 저장소를 준비하지 못했어요. Local path와 기준 브랜치를 확인해 주세요.', db.now_iso(), db.now_iso()))


def valid(c, row):
    try:
        return key(snapshot(c, row['issue_id'])) == row['snapshot_key']
    except (ValueError, OSError, subprocess.SubprocessError):
        return False


def select_agent(c, value):
    import auto_settings
    providers = {r['provider'] for r in value['implementations']}
    task_ids = {r['id'] for r in value['family'][1:]}
    known = {r['issue_id'] for r in value['implementations']}
    if not providers or task_ids - known:
        raise ValueError('구현 provider 이력이 없어요. 구현 실행 이력을 확인한 뒤 새 QA 회차를 요청해 주세요.')
    choices = auto_settings._settings(c, {'key': value['project_key']})['agent_orders']['qa_provider_order']
    agent = next((a for a in choices if a['provider'] not in providers), None)
    if not agent:
        raise ValueError('전체 구현자와 독립적인 QA Agent가 없어요. 다른 provider의 활성 Agent를 준비해 주세요.')
    return agent


def validate_report(report, value, files, evidence):
    if not isinstance(report, dict) or set(report) != {'items'} or not isinstance(report['items'], list):
        raise ValueError('QA 결과 형식이 잘못됐어요.')
    expected = {r['id'] for r in value['requirements']}
    tasks = {r['ref'] for r in value['family'][1:]}
    seen = set()
    for item in report['items']:
        if not isinstance(item, dict) or set(item) != {'requirement_id','verdict','evidence','task_refs','suggestion'}:
            raise ValueError('QA 항목 형식이 잘못됐어요.')
        rid = item['requirement_id']
        if not isinstance(rid, str) or rid not in expected or rid in seen or item['verdict'] not in ('pass','fail'):
            raise ValueError('요구 항목이 누락·중복되거나 판정이 잘못됐어요.')
        seen.add(rid)
        if not isinstance(item['task_refs'], list) or any(not isinstance(t,str) or t not in tasks for t in item['task_refs']) or not isinstance(item['suggestion'],str):
            raise ValueError('관련 Task·제안이 잘못됐어요.')
        refs = item['evidence']
        if not isinstance(refs, list) or not refs:
            raise ValueError('직접 확인 근거가 없어요.')
        has_test = False
        for ref in refs:
            if not isinstance(ref,dict):
                raise ValueError('근거 형식이 잘못됐어요.')
            if set(ref) == {'file','line'}:
                if ref['file'] not in files or type(ref['line']) is not int or not 1 <= ref['line'] <= len(files[ref['file']].splitlines()):
                    raise ValueError('파일·줄 참조가 잘못됐어요.')
            elif set(ref) == {'check_id'} and ref['check_id'] in evidence:
                has_test = True
                if evidence[ref['check_id']]['returncode'] is None:
                    raise ValueError('미실시 검사는 제품 실패·통과로 판정할 수 없어요.')
                if item['verdict'] == 'pass' and evidence[ref['check_id']]['returncode'] != 0:
                    raise ValueError('실패한 직접 검사를 통과 근거로 사용할 수 없어요.')
            else:
                raise ValueError('실행하지 않은 근거를 사용할 수 없어요.')
        if item['verdict'] == 'pass' and not has_test:
            raise ValueError('통과에는 실제 검사 근거가 필요해요.')
    if not expected or seen != expected:
        raise ValueError('요구·확인 항목이 누락됐어요.')
    return report['items']


def finish(qid, status, note='', items=None, usage=None, run_status=None):
    """최신성 재검증·결과 저장을 한 트랜잭션으로 처리한다. Issue 상태는 바꾸지 않는다."""
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM qa_rounds WHERE id=?', (qid,)).fetchone()
        if not row or row['status'] not in ('queued','running'):
            return
        if not valid(c, row):
            status, note, items = 'stale', '검사 중 목표·계획·Task·기준 커밋 또는 사람 설정이 바뀌었어요.', None
        now = db.now_iso()
        if items:
            for item in items:
                c.execute('INSERT INTO qa_items VALUES(?,?,?,?,?,?)', (qid,item['requirement_id'],item['verdict'],_json(item['evidence']),_json(item['task_refs']),item['suggestion']))
        c.execute('UPDATE qa_rounds SET status=?,note=?,ended_at=? WHERE id=?', (status,note,now,qid))
        if row['run_id']:
            usage = usage or {}
            c.execute('UPDATE runs SET status=?,ended_at=?,note=?,input_tokens=?,output_tokens=?,cost_usd=? WHERE id=?',
                      (run_status or ('ok' if status in ('passed','failed') else 'failed'),now,note,usage.get('input_tokens'),usage.get('output_tokens'),usage.get('cost_usd'),row['run_id']))
        issues._event(c,row['issue_id'],{'kind':'human','name':'system/qa'},'comment',
                      f"QA {row['round_number']}회: {status}. {note}", {'qa_id':qid,'qa_status':status})
    apply(qid)


REWORK_LIMIT = 5


def _delegation(c, key):
    """Auto QA를 켠 사람의 설정 위임. 꺼졌거나 사람이 켠 기록이 없으면 None."""
    import auto_settings
    if not auto_settings._settings(c, {'key': key})['auto_qa']:
        return None
    row = c.execute("""SELECT * FROM project_auto_settings_events WHERE scope='global'
        AND json_extract(before_json,'$.auto_qa')=0 AND json_extract(after_json,'$.auto_qa')=1
        ORDER BY id DESC LIMIT 1""").fetchone()
    if not row or not row['actor'].startswith('human:') or row['actor'].startswith('human:auto/'):
        return None
    return row


def _family(c, issue_id):
    return [issues._find(c, r[0]) for r in c.execute("""WITH RECURSIVE t(id) AS (SELECT ? UNION
        SELECT i.id FROM issues i JOIN t ON i.parent_id=t.id) SELECT id FROM t ORDER BY id""", (issue_id,))]


def apply(qid):
    """Auto QA ON에서만 판정을 상태에 반영한다. OFF·stale·goal·사람 검사·중복은 아무것도 바꾸지 않는다."""
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM qa_rounds WHERE id=?', (qid,)).fetchone()
        if not row or row['status'] not in ('passed', 'failed') or not valid(c, row):
            return
        value = json.loads(row['snapshot_json'])
        delegation = _delegation(c, value['project_key'])
        if not value.get('auto_qa') or not delegation:
            return
        actor = {'kind': 'human', 'name': 'auto/delegation/' + str(delegation['id'])}
        data = {'source': 'auto', 'qa_id': qid, 'delegation_id': delegation['id'], 'delegated_by': delegation['actor']}
        root = issues._find(c, row['issue_id'])
        family = [r for r in _family(c, root['id']) if not r['superseded_by_plan']]
        if c.execute("SELECT 1 FROM events WHERE issue_id=? AND json_extract(data_json,'$.qa_applied')=?", (root['id'], qid)).fetchone():
            return
        if row['status'] == 'passed':
            plans = [c.execute('SELECT body FROM plans WHERE issue_id=? ORDER BY version DESC LIMIT 1', (r['id'],)).fetchone() for r in family]
            if any('goal' in json.loads(r['labels_json']) for r in family):
                why = 'goal Issue는 사용자만 닫아요.'
            elif any(p and issues.human_checks(p['body']) for p in plans):
                why = "계획서의 '사람이 할 일'이 남아 있어요."
            else:
                why = ''
            if why:
                issues._event(c, root['id'], actor, 'comment', f'QA는 통과했지만 자동 Done 하지 않았어요 — {why} 확인 후 직접 Done 해 주세요.', {**data, 'qa_applied': qid})
                return
            for r in family:
                if r['status'] not in ('done', 'closed'):
                    issues._set_status(c, actor, r, 'done', 'Auto QA 통과로 완료했어요.', {**data, 'qa_applied': qid})
            return
        count = c.execute("SELECT COUNT(*) FROM qa_rework_batches WHERE issue_id=? AND status IN ('registered','complete')", (root['id'],)).fetchone()[0]
        refs = sorted({t for item in c.execute("SELECT task_refs_json FROM qa_items WHERE qa_id=? AND verdict='fail'", (qid,))
                       for t in json.loads(item[0])})
        tasks = [r for r in family[1:] if r['ref'] in refs and r['status'] not in ('closed',)]
        if 'goal' in json.loads(root['labels_json']) or any('goal' in json.loads(t['labels_json']) for t in tasks):
            issues._event(c, root['id'], actor, 'comment', 'QA 실패예요. goal Issue는 자동 재작업하지 않아요 — 사람이 결정해 주세요.', {**data, 'qa_applied': qid})
            return
        if count >= REWORK_LIMIT or not tasks:
            why = (f'재작업 {REWORK_LIMIT}회 후에도 QA 기준에 못 미쳤어요' if tasks or count >= REWORK_LIMIT
                   else '기존 Task와 연결되지 않은 실패예요. 새 Task가 필요하면 계획서를 고쳐 승인해 주세요(승인 전에는 실행하지 않아요)')
            issues._set_status(c, actor, root, 'on_hold', f'{why} — 사람에게 넘겨요.', {**data, 'qa_applied': qid})
            return
        c.execute("INSERT INTO qa_rework_batches(issue_id,qa_id,attempt,status,task_refs_json,created_at) VALUES(?,?,?,'registered',?,?)",
                  (root['id'], qid, count + 1, _json([t['ref'] for t in tasks]), db.now_iso()))
        memo = '\n'.join(f"- {i['requirement_id']}: {i['suggestion']}" for i in c.execute(
            "SELECT * FROM qa_items WHERE qa_id=? AND verdict='fail' ORDER BY requirement_id", (qid,)))
        for t in tasks:
            if t['status'] != 'changes_requested':
                issues._set_status(c, actor, t, 'changes_requested', f'QA 재작업 {count + 1}/{REWORK_LIMIT}:\n{memo}', data)
        issues._event(c, root['id'], actor, 'comment',
                      f"QA 재작업 {count + 1}/{REWORK_LIMIT}회를 등록했어요 — {', '.join(t['ref'] for t in tasks)}\n{memo}", {**data, 'qa_applied': qid})


def rework_job_ok(c, issue, job=None):
    """등록된 QA 재작업 묶음의 Task를 Auto 실행해도 되는지(선행·승인·설정을 다시 확인)."""
    import execute
    batch = c.execute("""SELECT b.* FROM qa_rework_batches b, json_each(b.task_refs_json) t
        WHERE b.status='registered' AND t.value=? ORDER BY b.id DESC LIMIT 1""", (issue['ref'],)).fetchone()
    if not batch or issue.get('claimed_by') or 'goal' in issue['labels']:
        return False
    expected = 'waiting' if job else 'changes_requested'
    if issue['status'] != expected or (job and job['previous_status'] != 'changes_requested'):
        return False
    if not _delegation(c, issue['project_key']):
        return False
    parent = issues.get_issue(issue['parent_ref'])
    if execute.blocked_reason(issue, parent) or not parent['approval']['actor'].startswith('human:'):
        return False
    return not job or parent['approval']['plan_version'] == job['approval_version']


def sync_rework(c):
    """재작업 Task를 선행 순서대로 Auto 대기열에 넣고, 병합된 Task를 위임으로 Done 처리해 다음 Task·재검사를 연다."""
    import auto_settings
    import automation
    import jobs
    for batch in c.execute("SELECT * FROM qa_rework_batches WHERE status='registered' ORDER BY id").fetchall():
        root = issues._find(c, batch['issue_id'])
        delegation = _delegation(c, root['project_key'])
        if not delegation or root['status'] != 'in_review':
            continue
        actor = {'kind': 'human', 'name': 'auto/delegation/' + str(delegation['id'])}
        refs = json.loads(batch['task_refs_json'])
        for ref in refs:
            row = issues._find(c, ref)
            run = c.execute("SELECT * FROM runs WHERE issue_id=? AND mode='execute' ORDER BY id DESC LIMIT 1", (row['id'],)).fetchone()
            if row['status'] == 'in_review' and run and run['status'] == 'ok' and run['started_at'] >= batch['created_at']:
                issues._set_status(c, actor, row, 'done', 'QA 재작업 병합을 확인해 완료했어요.', {'source': 'auto', 'qa_rework': batch['id']})
        if all(issues._find(c, ref)['status'] in ('done', 'closed') for ref in refs):
            c.execute("UPDATE qa_rework_batches SET status='complete' WHERE id=?", (batch['id'],))
            continue
        if not auto_settings._settings(c, {'key': root['project_key']})['auto_execute']:
            continue
        for ref in refs:
            issue = issues.get_issue(ref)
            if not rework_job_ok(c, issue) or c.execute("SELECT 1 FROM jobs WHERE issue_id=? AND status='queued'", (issue['id'],)).fetchone():
                continue
            provider = next((p for p in json.loads(c.execute("SELECT provider_order_json FROM global_project_auto_settings WHERE project_id=?", (issue['project_id'],)).fetchone()[0]) if automation.provider_available(p)), None)
            if not provider:
                continue
            jid = c.execute("INSERT INTO jobs(issue_id,mode,actor,status,created_at,provider,source,delegation_id,approval_version,model) VALUES(?,'execute',?,'queued',?,?,'auto',?,?,?)",
                            (issue['id'], 'human:' + actor['name'], db.now_iso(), provider, delegation['id'],
                             issues.get_issue(issue['parent_ref'])['approval']['plan_version'], auto_settings.model_for(c, issue['id'], provider))).lastrowid
            jobs._waiting(c, c.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone())


def recover():
    with db.connect() as c:
        c.execute("UPDATE runs SET status='orphaned',ended_at=?,note='서버 재시작으로 QA가 중단됐어요.' WHERE id IN (SELECT run_id FROM qa_rounds WHERE status='running') AND status='running'", (db.now_iso(),))
        c.execute("UPDATE qa_rounds SET status='orphaned',ended_at=?,note='서버 재시작으로 중단됐어요. 증거를 확인하고 새 QA 회차를 요청해 주세요.' WHERE status='running'", (db.now_iso(),))


def panel(ref):
    issue = issues.get_issue(ref)
    if issue['parent_id'] is not None:
        raise issues.StoreError('QA? ??? Issue?? ??? ???.', 400)
    with db.connect() as c:
        rounds = []
        for row in c.execute("SELECT q.*,r.input_tokens,r.output_tokens,r.cost_usd,r.started_at FROM qa_rounds q LEFT JOIN runs r ON r.id=q.run_id WHERE q.issue_id=? ORDER BY q.round_number DESC", (issue['id'],)):
            entry = dict(row)
            entry['snapshot'] = json.loads(entry.pop('snapshot_json'))
            from pathlib import Path
            evidence_path = db.config.DATA_DIR / 'qa-evidence' / str(row['id']) / 'checks.json'
            try:
                entry['checks'] = json.loads(evidence_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                entry['checks'] = {}
            entry['items'] = []
            for item in c.execute('SELECT * FROM qa_items WHERE qa_id=? ORDER BY requirement_id', (row['id'],)):
                value = dict(item)
                value['evidence'] = json.loads(value.pop('evidence_json'))
                value['task_refs'] = json.loads(value.pop('task_refs_json'))
                entry['items'].append(value)
            batches = []
            for batch in c.execute('SELECT * FROM qa_rework_batches WHERE qa_id=? ORDER BY id', (row['id'],)):
                value = dict(batch)
                value['task_refs'] = json.loads(value.pop('task_refs_json'))
                batches.append(value)
            entry['rework_batches'] = batches
            rounds.append(entry)
        count = c.execute("SELECT COUNT(*) FROM qa_rework_batches WHERE issue_id=? AND status IN ('registered','complete')", (issue['id'],)).fetchone()[0]
    return {'issue_ref': issue['ref'], 'rounds': rounds, 'rework_count': count, 'rework_limit': 5}
