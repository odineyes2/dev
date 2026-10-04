"""명시적인 관리자 설정 위임으로만 자동 대기를 등록한다. 자동 승인은 하지 않는다."""
import json
import os
import shutil
from pathlib import Path

import db
import issues


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
    if not job and c.execute('SELECT 1 FROM jobs WHERE issue_id=?', (issue['id'],)).fetchone():
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


def sync():
    """jobs의 잠금 아래 등록·OFF 취소를 직렬화하고 재시작에도 중복을 막는다."""
    import jobs
    import project_docs
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        for job in jobs._rows(c, "j.status='queued' AND j.source='auto'"):
            if not valid_job(c, job):
                c.execute("UPDATE jobs SET status='cancelled',note='자동 위임 조건이 바뀌었어요' WHERE id=?", (job['id'],))
                jobs._restore(c, job)
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
