"""점유와 독립적인 참여 이력·하위 Task 협업·종결 경과 시간을 검사한다."""
import os
import sys
import tempfile
from pathlib import Path

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
os.environ['NTFY_TOPIC'] = ''
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import auth, db, issues

db.init()
human = {'kind': 'human', 'name': 'admin'}
issues.create_project(human, 'HIST', 'history')
parent = issues.create_issue(human, 'HIST', 'parent')
child = issues.create_issue(human, 'HIST', 'child', parent=parent['ref'])
other = issues.create_issue(human, 'HIST', 'other')
start, end = '2026-10-10T00:00:00+00:00', '2026-10-10T01:30:00+00:00'
with db.connect() as c:
    for issue, provider in ((parent, 'claude'), (child, 'codex')):
        c.execute('INSERT INTO runs(issue_id,mode,status,actor,started_at,ended_at,provider) VALUES(?,?,?,?,?,?,?)',
                  (issue['id'], 'execute', 'ok', 'human:admin', start, end, provider))
    c.execute("UPDATE issues SET status='done',first_started_at=?,last_done_at=? WHERE id=?", (start, end, parent['id']))
    c.execute("UPDATE issues SET status='in_progress',first_started_at=? WHERE id=?", (start, child['id']))
done = issues.get_issue(parent['ref'])
assert done['processing_agents'] == ['claude', 'codex']
assert done['claimed_by'] is None and done['completion_seconds'] == 5400
assert issues.get_issue(child['ref'])['processing_agents'] == ['codex']
assert issues.get_issue(child['ref'])['completion_seconds'] is None
assert issues.get_issue(other['ref'])['processing_agents'] == []
assert next(i for i in issues.list_issues('HIST') if i['id'] == parent['id'])['processing_agents'] == ['claude', 'codex']

# 직접 MCP로 작업한 에이전트도 이력을 보존하고 점유만 한 경우는 제외한다.
agent, _ = auth.create_agent('manual', 'anthropic', '')
actor = {'kind': 'agent', 'name': agent['name'], 'id': agent['id']}
issues.claim(actor, other['ref'])
assert issues.get_issue(other['ref'])['processing_agents'] == []
issues.add_comment(actor, other['ref'], 'Implementation checked')
issues.release(actor, other['ref'])
assert issues.get_issue(other['ref'])['processing_agents'] == ['claude']
with db.connect() as c:
    c.execute("UPDATE issues SET status='closed',closed_at=? WHERE id=?", (end, child['id']))
    c.execute("UPDATE issues SET status='closed',closed_at=? WHERE id=?", (end, other['id']))
assert issues.get_issue(child['ref'])['completion_seconds'] == 5400
assert issues.get_issue(other['ref'])['completion_seconds'] is None
with db.connect() as c:
    c.execute("UPDATE issues SET status='in_progress' WHERE id=?", (parent['id'],))
assert issues.get_issue(parent['ref'])['completion_seconds'] is None
print('OK issue history')
