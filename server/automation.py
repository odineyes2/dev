"""명시적인 관리자 설정 위임으로만 자동 승인과 대기를 등록한다."""
import json
import os
import shutil
import re
from pathlib import Path, PureWindowsPath

import db
import issues


def plan_structure_error(body, root):
    """의미적 오류 검출을 보장하지 않고 기계적 승인에 필요한 구조만 검사한다."""
    pending = re.search(r'^#{1,6}\s*정해야 할 것[^\n]*\n(.*?)(?=^#{1,6}\s|\Z)', body, re.M | re.S)
    if pending and pending[1].strip().strip('- *').rstrip('.') not in ('', '없음', '없어요'):
        return '미해결 정해야 할 것이 있어요.'
    section = re.search(r'^#{1,6}\s*Tasks?\s*\n(.*?)(?=^#{1,6}\s|\Z)', body, re.M | re.S)
    try:
        tasks = issues.parse_tasks(body)
    except (ValueError, OverflowError):
        return 'Task 번호가 유효하지 않아요.'
    if not section or not tasks:
        return '명시적인 Tasks가 필요해요.'
    lines = [line.strip() for line in section[1].splitlines() if line.strip()]
    if len(lines) != len(tasks) or any(not re.match(r'^\d+[.)]\s+', line) for line in lines):
        return 'Tasks는 번호가 있는 한 줄 형식이어야 해요.'
    numbers = [t['n'] for t in tasks]
    if numbers != list(range(1, len(tasks) + 1)):
        return 'Task 번호는 1부터 중복 없이 순서대로 적어 주세요.'
    for task, line in zip(tasks, lines):
        if not task['title'] or not task['files'] or not task['check']:
            return 'Task 제목·파일·확인이 필요해요.'
        fields = [part.strip() for part in line.split('|')[1:]]
        if len(fields) != 3 or {f.partition(':')[0].strip() for f in fields} != {'파일', '확인', '선행'}:
            return 'Task에 파일·확인·선행을 명시해 주세요.'
        after = next(f.partition(':')[2].strip() for f in fields if f.startswith('선행'))
        if not re.fullmatch(r'(없음|\d+(?:\s*,\s*\d+)*)', after):
            return '선행은 없음 또는 Task 번호 목록이어야 해요.'
        if len(set(task['after'])) != len(task['after']) or any(n not in numbers or n == task['n'] for n in task['after']):
            return '선행 Task 번호가 유효하지 않아요.'
        for value in re.split(r'[,、]', task['files']):
            value = value.strip().strip('`')
            win = PureWindowsPath(value)
            # 다른 OS의 절대 경로와 역슬래시도 동일하게 판정한다.
            path = Path(value.replace('\\', '/'))
            if not value or win.drive or win.root or path.is_absolute() or '..' in path.parts or value.startswith('~'):
                return '파일 경로는 프로젝트 내부 상대 경로여야 해요.'
            if not root:
                return '프로젝트 경로가 필요해요.'
            try:
                (Path(root) / path).resolve().relative_to(Path(root).resolve())
            except (ValueError, OSError, RuntimeError):
                return '파일 경로가 프로젝트 밖을 가리켜요.'
    graph = {t['n']: t['after'] for t in tasks}
    remaining = set(graph)
    while remaining:
        ready = {n for n in remaining if not (set(graph[n]) & remaining)}
        if not ready:
            return '선행 Task에 순환이 있어요.'
        remaining -= ready
    return None


def _approve_tasks(c, setting):
    """쓰기 잠금 안에서 관리자 위임과 단일 Task 결과를 재검증한다."""
    import orchestrate
    if not setting['auto_approve']:
        return
    delegation = c.execute("""SELECT * FROM project_auto_settings_events WHERE project_id=?
        AND json_extract(before_json,'$.auto_approve')=0
        AND json_extract(after_json,'$.auto_approve')=1 ORDER BY id DESC LIMIT 1""",
        (setting['project_id'],)).fetchone()
    if not delegation or not delegation['actor'].startswith('human:') or delegation['actor'].startswith('human:auto/'):
        return
    rows = c.execute("SELECT id FROM issues WHERE project_id=? AND parent_id IS NOT NULL AND status='in_review'", (setting['project_id'],)).fetchall()
    for item in rows:
        row = issues._find(c, item['id'])
        if 'goal' in json.loads(row['labels_json']) or row['claimed_by']:
            continue
        parent = issues._find(c, row['parent_id'])
        if parent['project_id'] != row['project_id']:
            continue
        plan = c.execute('SELECT * FROM plans WHERE issue_id=? ORDER BY version DESC LIMIT 1', (parent['id'],)).fetchone()
        decision = c.execute("SELECT * FROM decisions WHERE issue_id=? AND gate='plan' ORDER BY id DESC LIMIT 1", (parent['id'],)).fetchone()
        if (not plan or not decision or decision['plan_version'] != plan['version']
            or decision['verdict'] not in ('approve','approve_notes')
            or not decision['actor'].startswith('human:') or decision['actor'].startswith('human:auto/')):
            continue
        run = c.execute('SELECT * FROM runs WHERE issue_id=? ORDER BY id DESC LIMIT 1', (row['id'],)).fetchone()
        if not run or run['mode'] != 'execute' or run['status'] != 'ok' or not run['ended_at']:
            continue
        result = c.execute('SELECT * FROM task_execution_results WHERE run_id=?', (run['id'],)).fetchone()
        status = c.execute("SELECT * FROM events WHERE issue_id=? AND kind='status' ORDER BY id DESC LIMIT 1", (row['id'],)).fetchone()
        commit = c.execute("SELECT id FROM events WHERE issue_id=? AND kind='commit' ORDER BY id DESC LIMIT 1", (row['id'],)).fetchone()
        start = c.execute("SELECT * FROM events WHERE issue_id=? AND kind='status' AND json_extract(data_json,'$.run_id')=? ORDER BY id DESC LIMIT 1", (row['id'], run['id'])).fetchone()
        if not start or not status or status['id'] <= start['id'] or json.loads(status['data_json']).get('to') != 'in_review':
            continue
        if c.execute("SELECT 1 FROM events WHERE issue_id=? AND kind='status' AND id>? AND id<?", (row['id'], start['id'], status['id'])).fetchone():
            continue
        if c.execute("SELECT 1 FROM events WHERE issue_id=? AND id>? AND (kind='plan' OR json_extract(data_json,'$.decision') IS NOT NULL)", (parent['id'], start['id'])).fetchone():
            continue
        if result:
            if (result['state'] not in ('merged','unmerged') or result['plan_version'] != plan['version']
                or not status or result['status_event_id'] != status['id']
                or (commit['id'] if commit else None) != result['commit_event_id']):
                continue
        else:
            # 이전 실행도 같은 실행 구간의 상태·승인·배포 성공 기록이 필요하다.
            if decision['created_at'] > run['started_at']:
                continue
            if orchestrate.settings(row['project_key']).get('auto_merge'):
                deployed = c.execute("SELECT * FROM events WHERE issue_id=? AND kind='comment' AND id>? ORDER BY id DESC LIMIT 1", (row['id'], status['id'])).fetchone()
                if (not commit or commit['id'] <= start['id'] or not deployed
                    or deployed['actor'] != run['actor'] or not deployed['body'].startswith('🔁')
                    or deployed['created_at'] < run['ended_at']):
                    continue
        if orchestrate.settings(row['project_key']).get('auto_merge') and result and (result['state'] != 'merged' or not commit):
            continue
        provenance = {'source':'auto','delegation_id':delegation['id'],'delegated_by':delegation['actor'],
                      'run_id':run['id'],'plan_version':plan['version'], 'commit_event_id':commit['id'] if commit else None}
        issues._complete_auto_task(c, row, provenance)


def provider_available(provider):
    import review
    if provider == 'codex':
        binary = os.environ.get('DEV_CODEX_BIN') or 'codex'
        return bool(os.environ.get('DEV_CODEX_AGENT_KEY') and (shutil.which(binary) or Path(binary).is_file()))
    return bool((shutil.which(review.claude_bin()) or Path(review.claude_bin()).is_file()) and review.MCP_CONFIG.is_file())


def eligible(c, issue, mode, job=None):
    import execute
    if 'goal' in issue['labels'] or issue.get('claimed_by'):
        return False
    expected = 'waiting' if job else 'backlog'
    if issue['status'] != expected or (job and job['previous_status'] != 'backlog'):
        return False
    if c.execute('SELECT 1 FROM runs WHERE issue_id=?', (issue['id'],)).fetchone():
        return False
    if not job and c.execute("""SELECT 1 FROM jobs WHERE issue_id=? AND NOT (
        source='auto' AND mode='execute' AND status='cancelled'
        AND COALESCE(cancellation_reason,'')='auto_execute_off'
        AND cancellation_event_id IS NOT NULL AND started_at IS NULL AND run_id IS NULL)
        """, (issue['id'],)).fetchone():
        return False
    if mode == 'review':
        return (not issue['parent_ref'] and issue['reporter'].startswith('human:') and not issue['plan'])
    parent = issues.get_issue(issue['parent_ref']) if issue['parent_ref'] else None
    if execute.blocked_reason(issue, parent):
        return False
    approval = parent['approval']
    return (approval['actor'].startswith('human:') and
            (not job or approval['plan_version'] == job['approval_version']))


def valid_job(c, job):
    issue = issues.get_issue(job['ref'])
    settings = c.execute('SELECT s.*,p.archived FROM project_auto_settings s JOIN projects p ON p.id=s.project_id WHERE s.project_id=?', (issue['project_id'],)).fetchone()
    if not settings or settings['archived'] or not settings['auto_' + job['mode']]:
        return False
    return eligible(c, issue, job['mode'], job)


def cancel_invalid(c, job, start_error=False):
    """동일 잠금에서 OFF와 다른 무효 조건을 구별하고 대기 소유권을 복구한다."""
    import jobs
    current = c.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone()
    if not current or current['status'] != 'queued':
        return
    issue = issues.get_issue(job['ref'])
    setting = c.execute('SELECT s.*,p.archived FROM project_auto_settings s JOIN projects p ON p.id=s.project_id WHERE s.project_id=?', (issue['project_id'],)).fetchone()
    reason, event_id = ('start_error' if start_error else 'conditions_changed'), None
    if (not start_error and setting and not setting['archived'] and not setting['auto_execute']
            and job['source'] == 'auto' and job['mode'] == 'execute'
            and current['started_at'] is None and current['run_id'] is None
            and jobs._latest(c, issue['id']).get('job_id') == job['id']
            and eligible(c, issue, 'execute', job)):
        event = c.execute("""SELECT * FROM project_auto_settings_events WHERE project_id=?
            AND id>? AND json_extract(before_json,'$.auto_execute')=1
            AND json_extract(after_json,'$.auto_execute')=0 ORDER BY id DESC LIMIT 1""",
            (issue['project_id'], job['delegation_id'] or 0)).fetchone()
        if event:
            reason, event_id = 'auto_execute_off', event['id']
    c.execute("UPDATE jobs SET status='cancelled',note='자동 위임 조건이 바뀌었어요',cancellation_reason=?,cancellation_event_id=? WHERE id=?", (reason, event_id, job['id']))
    jobs._restore(c, job)


def sync():
    """jobs의 잠금 아래 등록·OFF 취소를 직렬화하고 재시작에도 중복을 막는다."""
    import jobs
    import project_docs
    # 결과 승인을 커밋한 뒤 기존 대기열 경로가 선행 Task 완료를 읽게 한다.
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        settings = c.execute('SELECT s.* FROM project_auto_settings s JOIN projects p ON p.id=s.project_id WHERE p.archived=0 ORDER BY p.id').fetchall()
        for setting in settings:
            _approve_tasks(c, setting)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        for job in jobs._rows(c, "j.status='queued' AND j.source='auto'"):
            if not valid_job(c, job):
                cancel_invalid(c, job)
        settings = c.execute('SELECT s.*,p.key FROM project_auto_settings s JOIN projects p ON p.id=s.project_id WHERE p.archived=0 ORDER BY p.id').fetchall()
        for setting in settings:
            delegation = c.execute('SELECT id FROM project_auto_settings_events WHERE project_id=? ORDER BY id DESC LIMIT 1', (setting['project_id'],)).fetchone()
            if not delegation:
                continue
            rows = c.execute(f"SELECT {issues.ref_sql('i','p')} AS ref FROM issues i JOIN projects p ON p.id=i.project_id WHERE i.project_id=? AND i.status='backlog' ORDER BY i.id", (setting['project_id'],)).fetchall()
            for row in rows:
                issue = issues.get_issue(row['ref'])
                mode = 'execute' if issue['parent_ref'] else 'review'
                if not setting['auto_' + mode] or not eligible(c, issue, mode):
                    continue
                fixed = project_docs.provider_for(issue['id'])
                order = [fixed] if fixed else json.loads(setting['provider_order_json'])
                provider = next((p for p in order if provider_available(p)), None)
                if not provider:
                    continue
                # 기존 실행기의 사람 위임 경로를 쓰되 수동 요청자로 가장하지 않는다.
                actor = 'human:auto/delegation/' + str(delegation['id'])
                version = issues.get_issue(issue['parent_ref'])['approval']['plan_version'] if mode == 'execute' else None
                jid = c.execute("INSERT INTO jobs(issue_id,mode,actor,status,created_at,provider,source,delegation_id,approval_version) VALUES(?,?,?,'queued',?,?,'auto',?,?)", (issue['id'],mode,actor,db.now_iso(),provider,delegation['id'],version)).lastrowid
                issues._event(c, issue['id'], jobs._actor(actor), 'comment', 'Auto 설정 위임으로 작업을 등록했어요.', {'source':'auto','delegation_id':delegation['id'],'delegated_by':setting['updated_by'],'job_id':jid})
                jobs._waiting(c, c.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone())
