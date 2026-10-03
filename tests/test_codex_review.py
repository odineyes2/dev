"""Codex 검토 — 실제 계정 호출 없이 API·대기열·JSONL·실패 처리를 검증한다."""
import json, os, sys, tempfile, time, tomllib
from pathlib import Path

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
os.environ['DEV_CODEX_AGENT_KEY'] = 'test-secret'
os.environ['NTFY_TOPIC'] = ''
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))
import review, jobs, db, auth, app as A
import httpx
from fastapi.testclient import TestClient

cmd = review.codex_command_for('CX-1')
assert '--ignore-user-config' in cmd and '--ignore-rules' in cmd
assert cmd[cmd.index('--sandbox') + 1] == 'read-only'
assert 'approval_policy="never"' in cmd and '--json' in cmd
assert 'test-secret' not in ' '.join(cmd)
mcp = next(v for v in cmd if v.startswith('mcp_servers='))
assert 'required=true' in mcp and 'post_plan' in mcp and 'link_commit' not in mcp
policy = tomllib.loads(mcp)['mcp_servers']['dev']
assert policy['default_tools_approval_mode'] == 'prompt'
assert set(policy['tools']) == set(policy['enabled_tools'])
assert all(t['approval_mode'] == 'approve' for t in policy['tools'].values())
events = '\n'.join(json.dumps(e) for e in [
    {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'plan ready'}},
    {'type': 'turn.completed', 'usage': {'input_tokens': 100, 'cached_input_tokens': 50, 'output_tokens': 12}}])
text, stats = review._parse(events, 'codex')
assert text == 'plan ready' and stats == {'input_tokens': 100, 'output_tokens': 12}
assert review._parse('not json', 'codex') == ('not json', {})
fake = Path(os.environ['DEV_DATA_DIR']) / 'fake.py'
fake.write_text('import time\ntime.sleep(.3)\nprint(' + repr(events) + ')\n', 'utf-8')
success_script = '''import os,sys,time
sys.path.insert(0, sys.argv[2])
import db,issues
time.sleep(.3)
actor = {'kind': 'agent', 'id': 1, 'name': 'codex-test', 'model': 'test'}
issues.post_plan(actor, sys.argv[1], 'Review plan')
issue = issues.get_issue(sys.argv[1])
if issue['title_missing']:
    issues.update_issue(actor, sys.argv[1], {'title': 'Reviewed title'})
'''
fake.write_text(success_script + 'print(' + repr(events) + ')\n', 'utf-8')
review.codex_command_for = lambda ref: [sys.executable, str(fake), ref, str(Path(review.__file__).parent)]
review.command_for = lambda ref: [sys.executable, str(fake)]
auth._client = httpx.AsyncClient(transport=httpx.MockTransport(
    lambda req: httpx.Response(200, json={'user': {'id': 1, 'username': 'admin', 'role': 'admin'}})))
H = {'X-Requested-With': 'dev'}

def wait_idle():
    for _ in range(100):
        if not jobs.busy() and not jobs.list_jobs():
            return
        time.sleep(.1)
    raise AssertionError('queue stuck')

with TestClient(A.app) as c:
    c.cookies.set('ns_session', 'adm')
    c.post('/api/projects', json={'key': 'CX', 'name': 'codex'}, headers=H)
    c.post('/api/agents', json={'name': 'fake-reviewer'}, headers=H)
    for n in range(3):
        c.post('/api/issues', json={'project': 'CX', 'title': str(n)}, headers=H)
    c.post('/api/issues', json={'project': 'CX', 'title': '', 'body': 'Fill title and post a plan'}, headers=H)
    assert c.post('/api/issues/CX-1/review', json={'provider': 'other'}, headers=H).status_code == 400
    assert c.post('/api/issues/CX-1/review', json={'provider': 'codex'}, headers=H).json()['started']
    queued = c.post('/api/issues/CX-2/review', json={'provider': 'codex'}, headers=H).json()
    assert queued['queued'] and jobs.list_jobs()[0]['provider'] == 'codex'
    assert c.post('/api/issues/CX-2/review', json={'provider': 'claude'}, headers=H).status_code == 409
    assert c.post('/api/issues/CX-2/review', json={'provider': 'codex'}, headers=H).json()['job_id'] == queued['job_id']
    wait_idle()
    for ref in ('CX-1', 'CX-2'):
        run = review.list_runs(ref)[0]
        assert run['provider'] == 'codex' and run['status'] == 'ok'
        assert run['input_tokens'] == 100 and run['output_tokens'] == 12 and run['cost_usd'] is None
    c.post('/api/issues/CX-4/review', json={'provider': 'codex'}, headers=H)
    wait_idle()
    filled = c.get('/api/issues/CX-4').json()
    assert filled['title'] == 'Reviewed title' and filled['plan']
    assert review.list_runs('CX-4')[0]['status'] == 'ok'
    c.post('/api/issues', json={'project': 'CX', 'title': '', 'body': 'Missing title'}, headers=H)
    fake.write_text(success_script.split("issue = issues.get_issue")[0] + 'print(' + repr(events) + ')\n', 'utf-8')
    c.post('/api/issues/CX-5/review', json={'provider': 'codex'}, headers=H)
    wait_idle()
    assert review.list_runs('CX-5')[0]['status'] == 'failed'
    assert '빈 제목' in review.list_runs('CX-5')[0]['note']
    fake.write_text('import sys\nprint("failed")\nsys.exit(3)\n', 'utf-8')
    c.post('/api/issues/CX-3/review', json={'provider': 'codex'}, headers=H)
    wait_idle()
    assert review.list_runs('CX-3')[0]['status'] == 'failed'
    assert 'Codex' in c.get('/api/issues/CX-3').json()['events'][-1]['body']
    fake.write_text('print(' + repr(events) + ')\n', 'utf-8')
    c.post('/api/issues/CX-1/review', json={'provider': 'codex'}, headers=H)
    wait_idle()
    assert review.list_runs('CX-1')[0]['status'] == 'failed'
    assert '새 계획서' in review.list_runs('CX-1')[0]['note']
    fake.write_text('print(\'{"type":"turn.failed","error":{"message":"failed"}}\')\n', 'utf-8')
    c.post('/api/issues/CX-3/review', json={'provider': 'codex'}, headers=H)
    wait_idle()
    assert review.list_runs('CX-3')[0]['status'] == 'failed'
    fake.write_text('import time\ntime.sleep(3)\n', 'utf-8')
    review.TIMEOUT_SEC = .1
    c.post('/api/issues/CX-3/review', json={'provider': 'codex'}, headers=H)
    wait_idle()
    assert review.list_runs('CX-3')[0]['status'] == 'timeout'
    del os.environ['DEV_CODEX_AGENT_KEY']
    result = c.post('/api/issues/CX-1/review', json={'provider': 'codex'}, headers=H).json()
    assert result['queued'] and 'DEV_CODEX_AGENT_KEY' in result['note']
    assert len(review.list_runs('CX-1')) == 2
    db.init()
    assert jobs.list_jobs()[0]['provider'] == 'codex'
    jobs.cancel({'kind': 'human', 'name': 'admin'}, result['job_id'])
    key = c.post('/api/agents', json={'name': 'codex-test'}, headers=H).json()['key']
    c.cookies.clear()
    assert c.post('/api/issues/CX-1/review', json={'provider': 'codex'}, headers={'Authorization': 'Bearer ' + key}).status_code == 403
print('OK')
