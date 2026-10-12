"""복구 API·대기열·한 번의 CLI 호출·원 기록 보존을 임시 DB/Git으로 확인한다."""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
os.environ['DEV_CODEX_AGENT_KEY'] = 'test-only'
os.environ['NTFY_TOPIC'] = ''
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import httpx
from fastapi.testclient import TestClient
import app as A, auth, auto_settings, automation, db, execute, issues, jobs, orchestrate, review, troubleshoot

db.init()
me = {'kind': 'human', 'name': 'admin'}
repo = Path(tempfile.mkdtemp())
def git(*args, cwd=repo):
    result = orchestrate._git(str(cwd), *args)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()
git('init', '-q', '-b', 'main')
git('config', 'user.name', 'test'); git('config', 'user.email', 'test@local')
(repo / 'base.txt').write_text('base')
git('add', '.'); git('commit', '-qm', 'base')
issues.create_project(me, 'FIX', '복구 검사', local_path=str(repo))
parent = issues.create_issue(me, 'FIX', 'parent')
issues.post_plan(me, parent['ref'], '승인 범위의 코드 오류 수정')
issues.decide(me, parent['ref'], 'approve', plan_version=1)
claude, _ = auth.create_agent('작업 Agent', 'anthropic', 'claude-sonnet-4-6')
codex, key = auth.create_agent('복구 Agent', 'openai', 'gpt-6.1-sol')
auto_settings.update_settings(me, None, {'agent_orders': {'troubleshooter_provider_order': [codex['id'], claude['id']]}})
assert not auto_settings.get_settings()['auto_troubleshoot']

def failed(note='AssertionError: test failed', status='failed', ended='2026-10-12T00:00:00+00:00'):
    task = issues.create_issue(me, 'FIX', '실패 Task', parent=parent['ref'])
    wt = execute.prepare_worktree(str(repo), task['ref'])
    log, rid = review.begin(me, task, 'execute', 'codex')
    log.write_text(note + '\npassword=verysecret123', encoding='utf-8')
    with db.connect() as c:
        c.execute('INSERT INTO task_execution_results(run_id,plan_version) VALUES(?,1)', (rid,))
        c.execute('UPDATE runs SET status=?,ended_at=?,note=?,input_tokens=123,output_tokens=45 WHERE id=?', (status, ended, note, rid))
    with db.connect() as c:
        issues._set_status(c, me, issues._find(c, task['ref']), 'on_hold', '실패', {'run_id': rid})
    return issues.get_issue(task['ref']), wt, rid

def queue(task, **kw):
    with patch.object(automation, 'provider_available', return_value=True), patch.object(jobs, 'pump'):
        return troubleshoot.request(me, task['ref'], troubleshoot.snapshot(task), **kw)

task, wt, source = failed()
failure = troubleshoot.snapshot(task)
assert failure['available'] and failure['classification'] == 'code'
assert 'verysecret123' not in failure['diagnostic'] and 'AssertionError' in failure['diagnostic']
auth._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={'user': {'username': 'admin', 'role': 'admin'}})))
client = TestClient(A.app)
url = '/api/issues/' + task['ref'] + '/troubleshoot'
H = {'X-Requested-With': 'dev'}
assert client.post(url, headers=H, json=failure).status_code == 401
client.cookies.set('ns_session', 'test')
assert client.post(url, json=failure).status_code == 403
assert client.post(url, headers={'Authorization': 'Bearer ' + key}, json=failure).status_code == 403
assert client.post(url, headers=H, json={}).status_code == 400
assert client.post(url, headers=H, json={**failure, 'task_sha': 'stale'}).status_code == 409
with patch.object(automation, 'provider_available', return_value=True), patch.object(jobs, 'pump'):
    result = client.post(url, headers=H, json=failure)
    assert result.status_code == 202, result.text
    queued = result.json()
    assert client.post(url, headers=H, json=failure).status_code == 409
with db.connect() as c:
    job = dict(c.execute('SELECT * FROM jobs WHERE id=?', (queued['job_id'],)).fetchone())
assert job['provider'] == 'codex' and job['model'] == 'gpt-6.1-sol'

# 기존 worktree 변경을 보존하고 복구 실행에만 시간 제한·프롬프트를 붙인다.
(wt / 'base.txt').write_text('기존 에이전트의 미완성 변경', encoding='utf-8')
with patch.object(review, 'launch') as launch:
    execute.start(me, task['ref'], job['provider'], job['model'], troubleshoot_id=queued['id'])
    args = launch.call_args.args[-1]
    assert args[-3] == min(600, execute.TIMEOUT_SEC)
    assert '트러블슈팅 TS-' in args[5][-1]
assert (wt / 'base.txt').read_text(encoding='utf-8') == '기존 에이전트의 미완성 변경'
with db.connect() as c:
    t = c.execute('SELECT * FROM troubleshoot_requests WHERE id=?', (queued['id'],)).fetchone()
    recovery_run = t['run_id']
    assert t['status'] == 'running' and recovery_run != source

# 서버 검사 실패에서도 복구 CLI는 추가 재시도/이관을 하지 않는다.
cmd = [sys.executable, '-c', 'import json; print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"ready"}}))']
with patch.object(orchestrate, 'run_tests', return_value='FAILED: related test'), patch.object(review.subprocess, 'Popen', wraps=review.subprocess.Popen) as calls:
    status = review.run_headless(me, task['ref'], args[3], recovery_run, cmd, wt, None, 10, '실행', 'codex')
    assert status == 'failed' and calls.call_count == 1
troubleshoot.sync()
with db.connect() as c:
    assert c.execute('SELECT status FROM troubleshoot_requests WHERE id=?', (queued['id'],)).fetchone()[0] == 'failed'
    assert tuple(c.execute('SELECT input_tokens,output_tokens,status FROM runs WHERE id=?', (source,)).fetchone()) == (123,45,'failed')
assert troubleshoot.snapshot(issues.get_issue(task['ref']))['available']
assert troubleshoot.snapshot(issues.get_issue(task['ref']))['is_recovery']

# 사용 한도·중단은 자동 수정 대상에서 제외한다.
orchestrate.SETTINGS.write_text('{"FIX":{"auto_merge":false}}', encoding='utf-8')
success, success_wt, success_source = failed()
success_request = queue(success)
with patch.object(review, 'launch') as started, patch.object(automation, 'provider_available', return_value=True):
    jobs.pump()
    success_args = started.call_args.args[-1]
ready = json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps({'outcome':'ready','summary':'범위 내 코드 수정','tests':['관련 검사 통과']})}})
code = 'from pathlib import Path; Path("fixed.txt").write_text("fixed"); print(' + repr(ready) + ')'
with patch.object(orchestrate, 'run_tests', return_value=None) as checks:
    assert review.run_headless(me, success['ref'], success_args[3], success_args[4], [sys.executable,'-c',code], success_wt, None, 10, '실행','codex') == 'ok'
    checks.assert_called_once()
troubleshoot.sync()
with db.connect() as c:
    assert c.execute('SELECT status FROM troubleshoot_requests WHERE id=?', (success_request['id'],)).fetchone()[0] == 'succeeded'
assert issues.get_issue(success['ref'])['status'] == 'in_review'
assert orchestrate.completion(success['ref'])['task_sha'] == git('rev-parse','HEAD',cwd=success_wt)
issues.set_status(me, success['ref'], 'on_hold', '사람이 잠시 보류')
assert troubleshoot.snapshot(issues.get_issue(success['ref'])) is None
with db.connect() as c:
    c.execute("UPDATE execution_completion SET phase='failed',note='시험 병합의 선택된 검사가 실패했어요. AssertionError' WHERE run_id=?", (success_args[4],))
issues.set_status(me, success['ref'], 'changes_requested', '시험 병합 검사 실패')
merge_failure = troubleshoot.snapshot(issues.get_issue(success['ref']))
assert merge_failure['available'] and merge_failure['stage'] == 'merge' and '시험 병합' in merge_failure['diagnostic']

limited, _, _ = failed("You've hit your weekly limit")
assert troubleshoot.snapshot(limited)['classification'] == 'account'
assert not troubleshoot.snapshot(limited)['available']
old, _, _ = failed()
auto_settings.update_settings(me, None, {'auto_troubleshoot': True})
with patch.object(automation, 'provider_available', return_value=True):
    troubleshoot.sync()
with db.connect() as c:
    assert not c.execute('SELECT 1 FROM troubleshoot_requests WHERE issue_id=?', (old['id'],)).fetchone()
future, _, _ = failed(ended='2099-01-01T00:00:00+00:00')
manual_hold, _, _ = failed(ended='2099-01-01T00:00:00+00:00')
issues.set_status(me, manual_hold['ref'], 'backlog', '사람이 상태 변경')
assert not troubleshoot.snapshot(issues.get_issue(manual_hold['ref']))['automatic_available']
with patch.object(automation, 'provider_available', return_value=True):
    troubleshoot.sync(); troubleshoot.sync()
with db.connect() as c:
    pending = c.execute('SELECT * FROM troubleshoot_requests WHERE issue_id=?', (future['id'],)).fetchall()
    assert len(pending) == 1 and pending[0]['automatic'] == 1
    assert not c.execute('SELECT 1 FROM troubleshoot_requests WHERE issue_id=?', (manual_hold['id'],)).fetchone()
    future_job = dict(c.execute('SELECT * FROM jobs WHERE id=?', (pending[0]['job_id'],)).fetchone())
    future_job['ref'] = future['ref']
    assert automation.valid_job(c, future_job)
auto_settings.update_settings(me, None, {'auto_troubleshoot': False})
try:
    review.begin(me, issues.get_issue(future['ref']), 'execute', 'codex')
    raise AssertionError('OFF 직후 착수 허용')
except sqlite3.IntegrityError:
    pass
troubleshoot.sync()
with db.connect() as c:
    assert c.execute('SELECT status FROM troubleshoot_requests WHERE issue_id=?', (future['id'],)).fetchone()[0] == 'cancelled'
assert issues.get_issue(future['ref'])['status'] == 'on_hold'
with db.connect() as c:
    assert c.execute('SELECT cancellation_reason FROM jobs WHERE id=?', (future_job['id'],)).fetchone()[0] == 'auto_troubleshoot_off'

# 재시작으로 끊긴 유료 호출은 자동 재호출하지 않는다.
crash, _, _ = failed()
queued_crash = queue(crash)
with patch.object(review, 'launch'):
    execute.start(me, crash['ref'], 'codex', 'gpt-6.1-sol', troubleshoot_id=queued_crash['id'])
db.init(); troubleshoot.sync()
with db.connect() as c:
    assert c.execute('SELECT status FROM troubleshoot_requests WHERE id=?', (queued_crash['id'],)).fetchone()[0] == 'needs_input'

# Plan 변경과 사람 상태 변경은 미착수 복구를 취소한다.
dependency, _, _ = failed()
issues.set_status(me, success['ref'], 'done')
with db.connect() as c:
    c.execute('INSERT INTO issue_deps(issue_id,blocked_by_id) VALUES(?,?)', (dependency['id'], success['id']))
assert '병합을 먼저' in troubleshoot.snapshot(issues.get_issue(dependency['ref']))['blocked']
git('merge','--ff-only',execute.branch_name(success['ref']))
assert troubleshoot.snapshot(issues.get_issue(dependency['ref']))['available']

stale, stale_wt, _ = failed()
stale_request = queue(stale)
(stale_wt / 'changed.txt').write_text('다른 작업자가 수정')
git('add','.',cwd=stale_wt); git('commit','-qm','new head',cwd=stale_wt)
troubleshoot.sync()
with db.connect() as c:
    assert c.execute('SELECT status FROM troubleshoot_requests WHERE id=?', (stale_request['id'],)).fetchone()[0] == 'cancelled'

restored, restored_wt, _ = failed()
original_sha = git('rev-parse','HEAD',cwd=restored_wt)
issues.set_status(me, restored['ref'], 'done')
assert restored['ref'] in execute.prune_worktrees() and not restored_wt.exists()
issues.set_status(me, restored['ref'], 'on_hold')
restored = issues.get_issue(restored['ref'])
restored_request = queue(restored)
with patch.object(review, 'launch'), patch.object(automation, 'provider_available', return_value=True):
    execute.start(me, restored['ref'], 'codex', 'gpt-6.1-sol', troubleshoot_id=restored_request['id'])
assert restored_wt.is_dir() and git('rev-parse','HEAD',cwd=restored_wt) == original_sha
db.init(); troubleshoot.sync()

changed, _, _ = failed()
changed_request = queue(changed)
issues.post_plan(me, parent['ref'], '새 범위')
troubleshoot.sync()
with db.connect() as c:
    assert c.execute('SELECT status FROM troubleshoot_requests WHERE id=?', (changed_request['id'],)).fetchone()[0] == 'cancelled'
    assert not c.execute('SELECT 1 FROM provider_run_failures WHERE run_id=?', (recovery_run,)).fetchone()
    assert not c.execute('PRAGMA foreign_key_check').fetchall()
print('OK troubleshooter: API, role/model, one call, original usage, OFF race, restart, stale Plan')
