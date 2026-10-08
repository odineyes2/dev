"""임시 DB로 Description 자동 작성 요청(provider 우선순위·409·재사용·사람만)과 에이전트 쓰기(claim·running·이력)를 검사한다."""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))
import db, issues, jobs, auth, auto_settings, automation, review, project_description as pd

db.init()
me = {'kind': 'human', 'name': 'admin'}
issues.create_project(me, 'DSC', '대상', '', '/target', '사람이 쓴 의도')
agent, _ = auth.create_agent('claude', 'anthropic', 'claude-opus-5-5')
agent = {**agent, 'kind': 'agent'}


def denied(fn, status):
    try:
        fn()
    except issues.StoreError as e:
        assert e.status == status, e
    else:
        raise AssertionError('차단되어야 한다')


denied(lambda: pd.request_description(agent, 'DSC'), 403)
with patch.object(automation, 'provider_available', return_value=False):
    denied(lambda: pd.request_description(me, 'DSC'), 409)

# 우선순위대로 쓸 수 있는 첫 provider를 고른다.
auto_settings.update_settings(me, 'DSC', {'description_provider_order': ['codex', 'claude']})
with patch.object(jobs, 'start_block', return_value='바쁨'), \
     patch.object(automation, 'provider_available', side_effect=lambda p: p == 'claude'):
    first = pd.request_description(me, 'dsc')
assert first['provider'] == 'claude' and not first['reused'], first
ref = first['ref']
issue = issues.get_issue(ref)
assert issue['type_ids'] == [2] and '사람이 쓴 의도' in issue['body']
assert jobs.list_jobs()[-1]['provider'] == 'claude' and jobs.list_jobs()[-1]['mode'] == 'review'
with patch.object(jobs, 'start_block', return_value='바쁨'), patch.object(automation, 'provider_available', return_value=True):
    again = pd.request_description(me, 'DSC')
assert again['ref'] == ref and again['reused'] and again['provider'] == 'claude', again

# 전용 프롬프트와 도구 허용(검토에만).
assert 'write_project_description' in review.prompt_for(ref) and '목적' in review.prompt_for(ref)
assert 'write_project_description' not in review.prompt_for(issues.create_issue(me, 'DSC', '다른 일')['ref'])
assert 'mcp__dev__write_project_description' in review.ALLOWED_TOOLS
import execute
assert not any('write_project_description' in t for t in execute.ALLOWED_TOOLS)

# 에이전트 쓰기: 사람 거절, 일반 Issue 거절, claim 없으면 거절, running 없으면 거절.
denied(lambda: pd.write_description(me, ref, '새 설명'), 403)
denied(lambda: pd.write_description(agent, 'DSC-2', '새 설명'), 409)
denied(lambda: pd.write_description(agent, ref, '새 설명'), 409)
issues.claim(agent, ref)
denied(lambda: pd.write_description(agent, ref, '새 설명'), 409)
with db.connect() as c:
    iid = issue['id']
    c.execute("INSERT INTO runs(issue_id, mode, status, actor, started_at, log_file, provider) VALUES(?, 'review', 'running', 'x', ?, '', 'claude')",
              (iid, db.now_iso()))
denied(lambda: pd.write_description(agent, ref, ''), 400)
r = pd.write_description(agent, ref, '## 목적\n새 설명')
assert r['description'] == '## 목적\n새 설명'
assert next(p for p in issues.list_projects() if p['key'] == 'DSC')['description'] == '## 목적\n새 설명'
with db.connect() as c:
    ev = c.execute('SELECT * FROM project_description_events').fetchall()
assert len(ev) == 1 and ev[0]['before'] == '사람이 쓴 의도' and ev[0]['after'] == '## 목적\n새 설명' and ev[0]['actor'] == f"agent:{agent['id']}"
assert any(e['kind'] == 'comment' and '사람이 쓴 의도' in e['body'] for e in issues.get_issue(ref)['events'])
print('OK')
