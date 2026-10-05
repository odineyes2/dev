"""Codex 실행 — 실제 git worktree에서 API·커밋·결과 등록·실패 시 병합 방지를 확인한다."""
import json, os, sys, tomllib
from pathlib import Path

# 기존 실행 조건·worktree·원격 차단 검사를 재사용한다(임시 저장소와 DB).
import test_execute as base
import execute, issues, review, orchestrate, jobs

os.environ['DEV_CODEX_AGENT_KEY'] = 'test-secret'
projects = [{'key': 'DEV', 'local_path': r'C:\Projects\dev'},
            {'key': 'NS', 'local_path': r'C:\Projects\nightshift'}]
scope = lambda body: execute.scope_reason({'project_key': 'DEV', 'body': body}, projects)
assert 'NS' in scope('**바꿀 파일**: dev/static/style.css, nightshift/static/style.css')
assert 'NS' in scope(r'파일: C:\Projects\nightshift\static\style.css | 확인: 검사')
assert scope('**바꿀 파일**: static/style.css\n참고: nightshift/CLAUDE.md') is None
assert scope('파일: my-nightshift/style.css') is None
assert scope('파일: dev/static/style.css') is None
cmd = execute.codex_command_for('EX-1-2', 'EX-1')
assert cmd[cmd.index('--sandbox') + 1] == 'workspace-write'
assert '--output-schema' in cmd and '--dangerously-bypass-approvals-and-sandbox' not in cmd
assert 'sandbox_workspace_write.network_access=false' in cmd
if os.name == 'nt':
    assert 'windows.sandbox="elevated"' in cmd
assert 'test-secret' not in ' '.join(cmd)
mcp = tomllib.loads(next(v for v in cmd if v.startswith('mcp_servers=')))['mcp_servers']['dev']
assert 'mcp__dev__read_attachment' in execute.ALLOWED_TOOLS
assert set(mcp['enabled_tools']) == {'whoami', 'get_issue', 'list_projects', 'read_attachment'}
assert all(v['approval_mode'] == 'approve' for v in mcp['tools'].values())

fake = base.tmp / 'fake_codex.py'
result = {'outcome': 'ready', 'summary': 'Add feature; checked output', 'tests': ['python check passed']}

def output_script(result, edit=True):
    final = json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(result)}})
    return ('import pathlib,time\ntime.sleep(.3)\n'
            + ("pathlib.Path('codex.txt').write_text('implemented\\n')\n" if edit else '')
            + 'print(' + repr(final) + ')\n'
            + 'print(\'{"type":"turn.completed","usage":{"input_tokens":120,"output_tokens":24}}\')\n')

fake.write_text(output_script(result), 'utf-8')
execute.codex_command_for = lambda ref, parent: [sys.executable, str(fake)]
merged = []
original_handle = orchestrate.handle
orchestrate.handle = lambda actor, ref: merged.append(ref) or None
main_before = base.git(base.repo, 'rev-parse', 'main').stdout

try:
    with base.TestClient(base.A.app) as c:
        c.cookies.set('ns_session', 'adm')
        post = lambda ref, provider='codex': c.post(f'/api/issues/{ref}/execute', json={'provider': provider}, headers=base.H)
        assert post('EX-1-2', 'other').status_code == 400
        assert post('EX-1').status_code == 409
        assert post('EX-2-1').status_code == 409
        issues.create_project(base.me, 'OTHER', 'Other project', local_path=str(base.tmp / 'other'))
        cross = issues.create_issue(base.me, 'EX', 'Cross repository task',
                                    body='**바꿀 파일**: other/static/style.css', parent='EX-1')
        rejected = post(cross['ref'])
        assert rejected.status_code == 409 and 'OTHER' in rejected.json()['detail'], rejected.text
        assert not review.list_runs(cross['ref']) and not execute.worktree_path(cross['ref']).exists()
        assert post('EX-1-2').json()['started']
        assert issues.get_issue('EX-1-2')['status'] == 'in_progress'
        assert post('EX-1-2').json()['started'] and len(review.list_runs('EX-1-2')) == 3
        assert post('EX-1-2', 'claude').status_code == 409
        assert execute.blocked_reason(issues.get_issue('EX-1-2'), issues.get_issue('EX-1'))
        current = review.list_runs('EX-1-2')[0]['id']
        assert execute.completion_blocked_reason(issues.get_issue('EX-1-2'), current + 1)
        task = issues.create_issue(base.me, 'EX', 'Another task', parent='EX-1')
        queued = post(task['ref']).json()
        assert queued['queued'] and jobs.list_jobs()[0]['provider'] == 'codex'
        assert issues.get_issue(task['ref'])['status'] == 'waiting'
        assert post(task['ref']).json()['job_id'] == queued['job_id']
        jobs.cancel(base.me, queued['job_id'])
        base.wait_idle()
        run = review.list_runs('EX-1-2')[0]
        assert run['provider'] == 'codex' and run['mode'] == 'execute' and run['status'] == 'ok', run
        assert run['input_tokens'] == 120 and run['output_tokens'] == 24 and run['cost_usd'] is None
        issue = issues.get_issue('EX-1-2')
        assert issue['status'] == 'in_review' and any(e['kind'] == 'commit' for e in issue['events'])
        wt = execute.worktree_path('EX-1-2')
        assert (wt / 'codex.txt').exists() and not (base.repo / 'codex.txt').exists()
        assert base.git(base.repo, 'rev-parse', 'main').stdout == main_before
        assert base.git(wt, 'status', '--porcelain').stdout == ''
        assert merged == ['EX-1-2']
        assert not review.list_runs(task['ref']) and issues.get_issue(task['ref'])['status'] == 'backlog'
        issues.set_status(base.me, 'EX-1-2', 'changes_requested', 'retry')
        head = base.git(wt, 'rev-parse', 'HEAD').stdout
        fake.write_text(output_script({'outcome': 'blocked', 'summary': 'Tests failed', 'tests': []}, False), 'utf-8')
        assert post('EX-1-2').json()['started']
        base.wait_idle()
        # 막힘(blocked)은 실패가 아니라 사람 차례 — on_hold로 두고 이유를 남긴다(backlog로 되돌려 같은 이유로 다시 돌지 않게, NS-31-1).
        assert review.list_runs('EX-1-2')[0]['status'] == 'ok'
        held = issues.get_issue('EX-1-2')
        assert held['status'] == 'on_hold' and 'Tests failed' in held['events'][-1]['body']
        assert base.git(wt, 'rev-parse', 'HEAD').stdout == head and merged == ['EX-1-2']
        issues.set_status(base.me, 'EX-1-2', 'changes_requested', 'retry')
        fake.write_text(output_script(result, False), 'utf-8')
        post('EX-1-2'); base.wait_idle()
        assert review.list_runs('EX-1-2')[0]['status'] == 'failed'
        assert '코드 변경' in review.list_runs('EX-1-2')[0]['note']
        assert merged == ['EX-1-2']
        # 대기 후 착수와 실패 복구, 시간 초과 후 재시도를 확인한다.
        fake.write_text('import sys\nsys.exit(1)\n', 'utf-8')   # 진짜 실패는 착수 전 상태로 복구
        post('EX-1-2')
        queued = post(task['ref']).json()
        assert queued['queued'] and issues.get_issue(task['ref'])['status'] == 'waiting'
        base.wait_idle()
        task_issue = issues.get_issue(task['ref'])
        assert task_issue['status'] == 'backlog'
        assert any(e['kind'] == 'status' and e['data']['to'] == 'in_progress' for e in task_issue['events'])
        timeout_before = execute.TIMEOUT_SEC
        execute.TIMEOUT_SEC = .1
        fake.write_text('import time\ntime.sleep(3)\n', 'utf-8')
        post(task['ref']); base.wait_idle()
        assert review.list_runs(task['ref'])[0]['status'] == 'timeout'
        assert issues.get_issue(task['ref'])['status'] == 'backlog'
        execute.TIMEOUT_SEC = timeout_before
        # 사람이 바꾼 상태는 ready 응답에도 커밋하거나 덮어쓰지 않는다.
        fake.write_text(output_script(result), 'utf-8')
        for manual in ('on_hold', 'in_progress'):
            issues.set_status(base.me, task['ref'], 'backlog')
            post(task['ref'])
            issues.set_status(base.me, task['ref'], 'on_hold')
            issues.set_status(base.me, task['ref'], manual)
            base.wait_idle()
            assert review.list_runs(task['ref'])[0]['status'] == 'failed'
            assert issues.get_issue(task['ref'])['status'] == manual
            assert not any(e['kind'] == 'commit' for e in issues.get_issue(task['ref'])['events'])
        # 실행 도중 부모 계획서가 바뀌면 완료 검증에서 거절한다.
        issues.set_status(base.me, task['ref'], 'backlog')
        post(task['ref'])
        issues.post_plan(base.me, 'EX-1', 'Updated approval required')
        base.wait_idle()
        assert review.list_runs(task['ref'])[0]['status'] == 'failed'
        assert '승인되지' in review.list_runs(task['ref'])[0]['note']
        assert issues.get_issue(task['ref'])['status'] == 'backlog'
        issues.decide(base.me, 'EX-1', 'approve', '', plan_version=2)
        post(task['ref']); base.wait_idle()
        assert review.list_runs(task['ref'])[0]['status'] == 'ok'
        assert issues.get_issue(task['ref'])['status'] == 'in_review'
        del os.environ['DEV_CODEX_AGENT_KEY']
        missing = post('EX-1-2').json()
        assert missing['queued'] and 'DEV_CODEX_AGENT_KEY' in missing['note']
        assert issues.get_issue('EX-1-2')['status'] == 'waiting'
        jobs.cancel(base.me, missing['job_id'])
        assert issues.get_issue('EX-1-2')['status'] == 'changes_requested'
        c.cookies.clear()
        assert c.post('/api/issues/EX-1-2/execute', json={'provider': 'codex'}, headers={'Authorization': 'Bearer ' + base.key}).status_code == 403
finally:
    orchestrate.handle = original_handle

# Codex의 실제 결과 등록도 자동 병합을 거칠 때 조기 전환·알림을 하지 않는다.
from unittest.mock import patch
import notify
orchestrate.SETTINGS.write_text('{"EX":{"auto_merge":true}}')
os.environ['DEV_CODEX_AGENT_KEY'] = 'test-secret'
auto_task = issues.create_issue(base.me, 'EX', 'codex auto', parent='EX-1')
fake.write_text(output_script(result).replace('codex.txt', 'codex-auto.txt'), 'utf-8')
def inspect_auto(path):
    if 'merge-worktrees' not in str(path):   # 완료 등록 전 Task worktree 전체 검사
        return None
    assert issues.get_issue(auto_task['ref'])['status'] == 'in_progress'
    assert orchestrate.completion(auto_task['ref'])['phase'] == 'checking'
with patch.object(orchestrate, 'run_tests', side_effect=inspect_auto) as checked, patch.object(notify, 'send') as sent:
    execute.start(base.me, auto_task['ref'], 'codex')
    base.wait_idle()
    assert checked.call_count == 1   # Task worktree 검사만 — base가 그대로라 같은 트리의 시험 병합 검사는 생략
    assert sent.call_count == 1
assert issues.get_issue(auto_task['ref'])['status'] == 'in_review'
assert orchestrate.completion(auto_task['ref'])['phase'] == 'complete'
print('OK Codex execute')
