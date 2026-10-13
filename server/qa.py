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
        refs = [r[0] for r in c.execute("SELECT id FROM issues WHERE parent_id IS NULL AND status='in_review'")]
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
