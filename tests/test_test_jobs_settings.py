"""프로젝트별 동시 검사 수 설정(DEV-201) — 실제 앱 라우트로 사람만·범위·프로젝트별 저장·기본값 복귀와 서버 검사 반영을 확인한다."""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
os.environ['NTFY_TOPIC'] = ''
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import app, auth, db, issues, jobs, orchestrate  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

db.init()
H = {'kind': 'human', 'name': 'admin'}
issues.create_project(H, 'DEV', 'dev')
issues.create_project(H, 'NS', 'nightshift')
CPU = os.cpu_count() or 1
CSRF = {'X-Requested-With': 'dev'}

user = AsyncMock(return_value={'id': 1, 'username': 'admin', 'role': 'admin'})
with patch.object(auth, 'nightshift_user', user), patch.object(jobs, 'start_timer'), TestClient(app.app) as client:
    rows = {p['project']: p for p in client.get('/api/settings/test-jobs').json()['projects']}
    assert rows['DEV']['value'] == min(4, CPU) and rows['DEV']['source'] == '기본값' and rows['DEV']['saved'] is None, rows
    assert rows['NS']['value'] == 1 and rows['NS']['name'] == 'nightshift'
    assert client.get('/api/settings/test-jobs').json()['max'] == CPU

    assert client.put('/api/settings/test-jobs', json={'project': 'NS', 'value': 2}).status_code == 403          # CSRF 헤더 없음
    r = client.put('/api/settings/test-jobs', json={'project': 'NS', 'value': 2}, headers=CSRF)
    assert r.status_code == 200, r.text
    ns = next(p for p in r.json()['projects'] if p['project'] == 'NS')
    assert (ns['value'], ns['saved'], ns['source'], ns['default']) == (min(2, CPU), min(2, CPU), 'Settings', 1), ns
    assert orchestrate.test_jobs('NS') == min(2, CPU) and orchestrate.test_jobs('DEV') == min(4, CPU)            # 프로젝트별로 따로

    for bad in (0, CPU + 1, '3', 1.5, True):
        assert client.put('/api/settings/test-jobs', json={'project': 'NS', 'value': bad}, headers=CSRF).status_code == 400, bad
    assert client.put('/api/settings/test-jobs', json={'project': 'ZZ', 'value': 1}, headers=CSRF).status_code == 404

    r = client.put('/api/settings/test-jobs', json={'project': 'NS', 'value': None}, headers=CSRF)              # 지우면 기본값으로
    assert next(p for p in r.json()['projects'] if p['project'] == 'NS')['source'] == '기본값'
    assert orchestrate.test_jobs('NS') == 1

    with patch.object(orchestrate, 'settings', return_value={'test_jobs': 3}):                                  # orchestrate.json 값
        assert orchestrate.test_jobs_info('NS')['source'] == 'orchestrate.json' and orchestrate.test_jobs('NS') == min(3, CPU)

    with patch.object(auth, 'agent_for_key', return_value={'id': 1, 'name': 'agent', 'model': ''}):             # 에이전트는 못 읽고 못 바꾼다
        assert client.get('/api/settings/test-jobs', headers={'Authorization': 'Bearer fake'}).status_code == 403
        assert client.put('/api/settings/test-jobs', json={'project': 'NS', 'value': 2}, headers={'Authorization': 'Bearer fake'}).status_code == 403
    user.return_value = {'id': 2, 'username': 'member', 'role': 'user'}                                         # 관리자 아닌 사람
    assert client.get('/api/settings/test-jobs').status_code == 403

try:
    orchestrate.set_test_jobs({'kind': 'agent', 'id': 1}, 'NS', 2)
    raise AssertionError('에이전트가 바꿈')
except issues.StoreError as error:
    assert error.status == 403
print('OK — 동시 검사 수 설정 라우트·권한·범위·프로젝트별·기본값 복귀')
