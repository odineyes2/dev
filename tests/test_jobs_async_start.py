"""비동기 착수의 실제 HTTP 응답·실패 기록·단일 펌프를 검사한다."""
import os, sys, tempfile, time, threading
from pathlib import Path
from unittest.mock import patch
os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))
import httpx
from fastapi.testclient import TestClient
import app as A, auth, db, issues, jobs, review, execute

db.init()
me = {'kind': 'human', 'name': 'admin'}
issues.create_project(me, 'AS', 'async', '', '/nowhere/async')
for i in range(8):
    issues.create_issue(me, 'AS', str(i), type_ids=[1])
auth._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={'user': {'id': 1, 'username': 'admin', 'role': 'admin'}})))
H = {'X-Requested-With': 'dev'}
entered, release = threading.Event(), threading.Event()
def slow(ref):
    entered.set()
    assert release.wait(10)
    return [sys.executable, '-c', 'print("ok")']
with TestClient(A.app) as client:
    client.cookies.set('ns_session', 'adm')
    with patch.object(review, 'command_for', slow):
        start = time.monotonic()
        result = client.post('/api/issues/AS-1/review', headers=H).json()
        assert time.monotonic() - start < 1, result
        assert result['started'], result
        assert entered.wait(5)
        duplicate = client.post('/api/issues/AS-1/review', headers=H).json()
        assert duplicate['job_id'] == result['job_id'] and duplicate['started']
        queued = client.post('/api/issues/AS-2/review', headers=H).json()
        assert queued['queued'] and queued['note']
        assert client.post('/api/issues/AS-2/review', json={'provider': 'codex'}, headers=H).status_code == 409
        release.set()
        jobs.settle()
    assert review.list_runs('AS-1')
    jobs.cancel(me, queued['job_id'])
    for _ in range(100):
        if not jobs.busy(): break
        time.sleep(.05)
    def failure(*args): raise issues.StoreError('준비 실패', 409)
    with patch.object(review, 'start', failure):
        r = client.post('/api/issues/AS-3/review', headers=H).json()
        jobs.settle()
        assert jobs.job_for(issues.get_issue('AS-3')['id'])['note'] == '준비 실패'
        jobs.cancel(me, r['job_id'])
    with patch.object(review, 'start', side_effect=issues.StoreError('없어요', 404)):
        r = client.post('/api/issues/AS-4/review', headers=H).json()
        jobs.settle()
        jobs._start_notice(r['job_id'])
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM events WHERE json_extract(data_json,'$.job_id')=? AND json_extract(data_json,'$.start_skipped')=1", (r['job_id'],)).fetchone()[0] == 1
        assert any(e['body'].startswith('⏭ 맡긴 검토') for e in issues.get_issue('AS-4')['events'])
    with patch.object(review, 'start', side_effect=RuntimeError('실패')), patch('logging.exception') as logged:
        r = client.post('/api/issues/AS-5/review', headers=H).json()
        jobs.settle()
        assert '착수 중 오류' in jobs.job_for(issues.get_issue('AS-5')['id'])['note']
        logged.assert_called_once()
        jobs.cancel(me, r['job_id'])
    release.clear()
    def slow_execute(*args):
        assert release.wait(10)
        raise issues.StoreError('실행 준비 실패', 409)
    with patch.object(execute, 'blocked_reason', return_value=None), patch.object(execute, 'predecessor_merge_reason', return_value=None), patch.object(execute, 'start', side_effect=slow_execute):
        start = time.monotonic()
        r = client.post('/api/issues/AS-6/execute', headers=H).json()
        assert time.monotonic()-start < 1 and r['started'], r
        release.set()
        jobs.settle()
        assert jobs.job_for(issues.get_issue('AS-6')['id'])['note'] == '실행 준비 실패'
        jobs.cancel(me, r['job_id'])
active = peak = 0
lock = threading.Lock()
gate = threading.Event()
def pump():
    global active, peak
    with lock: active += 1; peak = max(peak, active)
    gate.wait(5)
    with lock: active -= 1
with patch.object(jobs, 'pump', pump):
    for _ in range(30): jobs.kick()
    gate.set()
    jobs.settle()
assert peak == 1
print('OK — 비동기 응답·중복·실패 기록·단일 kick')
