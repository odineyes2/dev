"""실행 진행 단계 표시(DEV-206) — 단계별 짧은 표시·ⓘ 상세, 멈춤 판단, 서버 검사 진행 n/N, 이슈 API에 실리는지."""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
os.environ['NTFY_TOPIC'] = ''
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as A  # noqa: E402
import auth  # noqa: E402
import db, issues, orchestrate, run_progress, test_runner  # noqa: E402,E401

db.init()
me = {'kind': 'human', 'name': 'admin'}

# 1. 단계마다 짧은 표시와 상세 — 구현은 제공자·모델·시간 제한, 재작업은 실패한 검사.
run_progress.set_phase('X-1-1', 'implement', attempt=1, provider='Codex', model='gpt-x', limit=1800)
v = run_progress.view('X-1-1', time.time() + 7 * 60)
assert v['short'] == '구현 중' and not v['stalled'], v
assert '구현 중 (1차)' in v['long'] and 'Codex gpt-x' in v['long'] and '경과 7분 / 시간 제한 30분' in v['long'], v['long']
assert '시도마다' in v['long']
failure = ('R05 전체 검사 실패 — T-DEV-001 test_usage.py: 종료 코드 1 (0x00000001)\n전체 로그: x\n...\n\n'
           'R05 전체 검사 실패 — T-DEV-002 test_db.py: 종료 코드 124 (0x0000007C)\n전체 로그: y')
assert run_progress.failed_names(failure) == ['test_usage.py', 'test_db.py']
run_progress.set_phase('X-1-1', 'rework', attempt=2, provider='Codex', limit=1800, failed=run_progress.failed_names(failure))
v = run_progress.view('X-1-1')
assert v['short'] == '재작업 중 (2차)' and '마지막 시도' in v['long'] and 'test_usage.py, test_db.py' in v['long'], v

# 2. 모델 호출이 시간 제한을 넘겨도 끝나지 않으면 멈춘 것으로 보인다.
v = run_progress.view('X-1-1', time.time() + 1800 + run_progress.STALL_GRACE_SEC + 5)
assert v['stalled'] and v['short'] == '⚠️ 멈춘 것 같아요' and v['long'].startswith('⚠️ 재작업 중'), v

# 3. 전체 검사 — n/N, 지금 도는 검사, 검사 하나가 시간 제한을 크게 넘기면 멈춤(DEV-191-1처럼).
run_progress.set_phase('X-1-1', 'checks', provider='Codex', limit=1800)
v = run_progress.view('X-1-1')
assert v['short'] == '전체 검사 중' and not v['stalled'] and '개 끝' not in v['long'], v
run_progress.checks_begin('X-1-1', 3)
run_progress.check_start('X-1-1', 'test_a.py'); run_progress.check_end('X-1-1', 'test_a.py', False)
run_progress.check_start('X-1-1', 'test_ui.py', test_runner.BROWSER_TIMEOUT_SEC)
v = run_progress.view('X-1-1')
assert '1/3개 끝 (실패 1개)' in v['long'] and '지금: test_ui.py' in v['long'] and '제한 20분' in v['long'] and not v['stalled'], v['long']
# 브라우저 검사는 일반 제한(10분)을 넘어도 멈춘 게 아니고, 자기 제한(20분)을 넘겨야 멈춘 것이다(DEV-209).
assert not run_progress.view('X-1-1', time.time() + test_runner.TIMEOUT_SEC + test_runner.PIPE_GRACE_SEC * 2 + 120)['stalled']
v = run_progress.view('X-1-1', time.time() + test_runner.BROWSER_TIMEOUT_SEC + test_runner.PIPE_GRACE_SEC * 2 + 120)
assert v['stalled'] and v['short'] == '⚠️ 멈춘 것 같아요', v
run_progress.check_end('X-1-1', 'test_ui.py', True)
run_progress.check_start('X-1-1', 'test_b.py')   # 제한을 안 주면 일반 제한
assert run_progress.view('X-1-1', time.time() + test_runner.TIMEOUT_SEC + test_runner.PIPE_GRACE_SEC * 2 + 120)['stalled']
# 재작업으로 넘어가면 앞 검사 진행은 지운다.
run_progress.set_phase('X-1-1', 'rework', attempt=2, limit=1800)
run_progress.set_phase('X-1-1', 'recheck', limit=1800)
assert '개 끝' not in run_progress.view('X-1-1')['long'] and run_progress.view('X-1-1')['short'] == '다시 검사 중'
run_progress.clear('X-1-1')
assert run_progress.view('X-1-1') is None
# 도는 실행이 없는 이슈의 검사(병합 등)는 진행을 남기지 않는다.
run_progress.checks_begin('X-9-9', 2)
assert run_progress.view('X-9-9') is None
print('OK — 단계별 표시·상세, 멈춤 판단, 검사 n/N')

# 4. 서버 완료 전 검사가 파일마다 진행을 남긴다.
repo = Path(tempfile.mkdtemp())
subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=repo, check=True)
(repo / 'tests').mkdir()
for name, body in {'test_ok': 'pass', 'test_bad': 'import sys; sys.exit(2)'}.items():
    (repo / 'tests' / f'{name}.py').write_text(f'"""{name}"""\n{body}\n', encoding='utf-8')
subprocess.run(['git', 'add', '-A'], cwd=repo, check=True)
subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'init'], cwd=repo, check=True)
issues.create_project(me, 'PX', 'PX', '', str(repo))
parent = issues.create_issue(me, 'PX', '부모')['ref']
task = issues.create_issue(me, 'PX', '작업', parent=parent)['ref']
seen = []
real = run_progress.view
with patch.object(orchestrate, 'run_test', side_effect=lambda repo, t, args=(), env=None: (
        seen.append(real(task)['long']), test_runner.run(repo, t, args, dict(os.environ)))[1]):
    run_progress.set_phase(task, 'checks', limit=1800)
    failed = orchestrate.run_tests(str(repo), 'server_completion', task)
assert 'test_bad.py' in failed
assert any('/2개 끝' in s and '지금: test_' in s for s in seen), seen
assert '2/2개 끝 (실패 1개)' in run_progress.view(task)['long'], run_progress.view(task)['long']
print('OK — 서버 검사가 파일마다 진행을 남긴다')

# 5. 이슈 API — in_progress Task에만 run_progress가 실리고, 목록·상세·부모의 하위 Task 모두에 보인다.
auth._client = httpx.AsyncClient(transport=httpx.MockTransport(
    lambda req: httpx.Response(200, json={'user': {'id': 1, 'username': 'admin', 'role': 'admin'}})))
with db.connect() as c:
    c.execute("UPDATE issues SET status='in_progress' WHERE id=?", (issues.get_issue(task)['id'],))
run_progress.set_phase(task, 'rework', attempt=2, limit=1800)
with TestClient(A.app) as client:
    client.cookies.set('ns_session', 'adm')
    detail = client.get(f'/api/issues/{task}').json()
    assert detail['run_progress']['short'] == '재작업 중 (2차)', detail.get('run_progress')
    listed = next(i for i in client.get('/api/issues', params={'project': 'PX', 'parent': parent}).json()['issues'] if i['ref'] == task)
    assert listed['run_progress']['phase'] == 'rework'
    child = next(ch for ch in client.get(f'/api/issues/{parent}').json()['children'] if ch['ref'] == task)
    assert child['run_progress']['phase'] == 'rework'
    assert client.get(f'/api/issues/{parent}').json()['run_progress'] is None   # in_progress가 아니면 싣지 않는다
print('OK — 이슈 API 목록·상세·하위 Task에 실행 단계가 실린다')

# 6. In Progress 경과 시간(DEV-208) — 도는 실행이 있으면 실행 시작부터, 없으면 마지막으로 In Progress가 된 때부터.
from datetime import datetime, timedelta, timezone  # noqa: E402


def ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec='seconds')


manual = issues.create_issue(me, 'PX', '대화에서 작업')['ref']
idle = issues.create_issue(me, 'PX', '대기')['ref']
with db.connect() as c:
    ids = {ref: issues.get_issue(ref)['id'] for ref in (task, manual, idle)}
    c.execute("UPDATE issues SET status='in_progress' WHERE id=?", (ids[manual],))
    c.execute("INSERT INTO events(issue_id,actor,kind,body,data_json,created_at) VALUES(?,?,?,?,?,?)",
              (ids[manual], 'agent:1', 'status', '', '{"from":"backlog","to":"in_progress"}', ago(200)))
    c.execute("INSERT INTO events(issue_id,actor,kind,body,data_json,created_at) VALUES(?,?,?,?,?,?)",
              (ids[manual], 'agent:1', 'status', '', '{"from":"on_hold","to":"in_progress"}', ago(75)))   # 마지막 것부터
    c.execute("INSERT INTO events(issue_id,actor,kind,body,data_json,created_at) VALUES(?,?,?,?,?,?)",
              (ids[task], 'human:admin', 'status', '', '{"from":"waiting","to":"in_progress"}', ago(500)))
    c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at) VALUES(?,?,?,?,?)", (ids[task], 'execute', 'failed', 'human:admin', ago(300)))
    c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at) VALUES(?,?,?,?,?)", (ids[task], 'execute', 'running', 'human:admin', ago(72)))
items = [issues.get_issue(ref) for ref in (task, manual, idle)]
run_progress.elapsed(items)
seconds = {it['ref']: it['in_progress_seconds'] for it in items}
assert 72 * 60 - 5 <= seconds[task] <= 72 * 60 + 30, seconds            # 도는 실행 시작부터(끝난 실행·상태 이벤트가 아니라)
assert 75 * 60 - 5 <= seconds[manual] <= 75 * 60 + 30, seconds          # 실행이 없으면 마지막으로 In Progress가 된 때부터
assert seconds[idle] is None, seconds                                    # In Progress가 아니면 없다
with TestClient(A.app) as client:
    client.cookies.set('ns_session', 'adm')
    listed = {i['ref']: i for i in client.get('/api/issues', params={'project': 'PX'}).json()['issues']}
    assert listed[manual]['in_progress_seconds'] >= 75 * 60 - 5 and listed[idle]['in_progress_seconds'] is None
    detail = client.get(f'/api/issues/{parent}').json()
    child = next(ch for ch in detail['children'] if ch['ref'] == task)
    assert child['in_progress_seconds'] >= 72 * 60 - 5 and detail['in_progress_seconds'] is None
    assert client.get(f'/api/issues/{manual}').json()['in_progress_seconds'] >= 75 * 60 - 5
print('OK — In Progress 경과 시간: 실행 시작·마지막 In Progress 기준, 목록·상세·하위 Task')
