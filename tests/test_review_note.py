"""메모 재검토는 승인/종료/Task 생성 없이 큐와 사람 지시를 함께 보존한다."""
import os
import sys
import tempfile
from pathlib import Path

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import httpx
from fastapi.testclient import TestClient
import app as A, auth, automation, auto_settings, db, issues, jobs, review

jobs.pump = lambda: None
jobs.start_timer = lambda: None
auth._client = httpx.AsyncClient(transport=httpx.MockTransport(
    lambda req: httpx.Response(200, json={'user': {'id': 1, 'username': 'admin', 'role': 'admin'}})))
me = {'kind': 'human', 'name': 'admin'}
H = {'X-Requested-With': 'dev'}
NOTE = 'SQLite는 임시예요. PostgreSQL로 통일하고 Claude 문구를 그대로 보존하세요.'

with TestClient(A.app) as client:
    client.cookies.set('ns_session', 'adm')
    project = issues.create_project(me, 'NOTE', '재검토', local_path='/missing/note')
    setting = auto_settings.update_settings(me, 'NOTE', {'auto_plan_approve': True})
    setting['project_id'] = project['id']
    it = issues.create_issue(me, 'NOTE', '계획 수정', labels=['goal'])
    plan = issues.post_plan(me, it['ref'], 'SQLite 사용 계획')
    request = {'provider': 'codex', 'note': NOTE, 'plan_version': plan['version']}
    route = f"/api/issues/{it['ref']}/review"
    for change in ({'note': ''}, {'note': {}}, {'plan_version': 99}, {'plan_version': None}):
        response = client.post(route, json={**request, **change}, headers=H)
        assert response.status_code in (400, 409), response.text
    assert not jobs.list_jobs()
    assert not issues.get_issue(it['ref'])['plan']['revision_note']
    response = client.post(route, json=request, headers=H)
    assert response.status_code == 200 and response.json()['queued'], response.text
    jid = response.json()['job_id']
    current = issues.get_issue(it['ref'])
    assert current['status'] != 'closed' and current['approval'] is None and not current['children']
    assert current['plan']['revision_note'] == NOTE
    assert len([e for e in current['events'] if e['data'].get('plan_revision_request')]) == 1
    assert NOTE in review.prompt_for(it['ref'])
    assert NOTE in review.codex_command_for(it['ref'])[-1]  # 사람 메모의 Claude를 Codex로 치환하지 않는다.
    assert client.post(route, json=request, headers=H).status_code == 409
    decision = client.post(f"/api/issues/{it['ref']}/decision", json={'verdict': 'approve', 'plan_version': 1}, headers=H)
    assert decision.status_code == 409
    jobs.cancel(me, jid)
    assert issues.get_issue(it['ref'])['plan']['revision_note'] == NOTE
    assert client.post(route, json=request, headers=H).status_code == 200  # 취소/실패 후 재요청
    queued_id = jobs.list_jobs()[0]['id']
    new = issues.post_plan(me, it['ref'], 'PostgreSQL 사용 계획')
    try:
        review.start(me, it['ref'], 'claude')
        raise AssertionError('대기 중 바뀐 계획서는 시작하지 않아야 한다')
    except issues.StoreError as error:
        assert error.status == 409 and '계획서가 바뀌었어요' in str(error)
    jobs.cancel(me, queued_id)
    assert not issues.get_issue(it['ref'])['plan']['revision_note']
    assert NOTE not in review.prompt_for(it['ref'])
    assert client.post(route, json=request, headers=H).status_code == 409
    issues.decide(me, it['ref'], 'approve', plan_version=new['version'])
    assert client.post(route, json={**request, 'plan_version': new['version']}, headers=H).status_code == 409

    # Auto 승인은 요청한 이전 판을 큐 취소/실패 뒤에도 승인하지 않는다.
    auto = issues.create_issue(me, 'NOTE', 'Auto 재검토')
    old = issues.post_plan(me, auto['ref'], '## Tasks\n1. 연결 | 파일: a.py | 확인: 동작 | 선행: 없음')
    response = client.post(f"/api/issues/{auto['ref']}/review", json={**request, 'plan_version': old['version']}, headers=H)
    assert response.status_code == 200, response.text
    jobs.cancel(me, response.json()['job_id'])
    with db.connect() as conn:
        conn.execute("UPDATE issues SET status='triage' WHERE id=?", (auto['id'],))
        rid = conn.execute("INSERT INTO runs(issue_id,mode,provider,status,actor,started_at,ended_at) VALUES(?,'review','codex','ok','human:admin',?,?)",
                           (auto['id'], db.now_iso(), db.now_iso())).lastrowid
        conn.execute('INSERT INTO review_plan_runs VALUES(?,?)', (old['id'], rid))
        automation._approve_plans(conn, setting)
    assert issues.get_issue(auto['ref'])['approval'] is None

    key = client.post('/api/agents', json={'name': 'test-agent'}, headers=H).json()['key']
    client.cookies.clear()
    response = client.post(route, json=request, headers={'Authorization': 'Bearer ' + key})
    assert response.status_code == 403

print('review-note: queue, note, stale version, duplicate, authority, manual/auto gate, new plan OK')
