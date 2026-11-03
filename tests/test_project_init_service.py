"""초기 생성의 자동 서비스 연결(DEV-247-1) — 도메인 있음/없음·연결 실패 보존·기존/허용 밖 도메인 거절·대기 상한·재시도 멱등·사람 전용 버튼을 임시 DB와 모의 cloudflared로 검사한다."""
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix='dev-init-service-'))
os.environ['DEV_DATA_DIR'] = str(TMP / 'data')
os.environ['DEV_DB_PATH'] = str(TMP / 'data' / 'dev.db')
TUNNEL = TMP / 'tunnel' / 'config.yml'
TUNNEL.parent.mkdir(parents=True)
ORIGINAL = 'tunnel: t1\ningress:\n- hostname: dev.lomebrote.com\n  service: http://127.0.0.1:8300\n- service: http_status:404\n'
TUNNEL.write_text(ORIGINAL, encoding='utf-8')
os.environ['DEV_TUNNEL_CONFIG'] = str(TUNNEL)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))

import yaml  # noqa: E402

import automation  # noqa: E402
import db  # noqa: E402
import issues  # noqa: E402
import jobs  # noqa: E402
import project_init  # noqa: E402
import project_service  # noqa: E402

db.init()
(TMP / 'projects').mkdir()
project_init.ROOT = TMP / 'projects'
project_init.start = lambda key: None
project_init.recover = lambda: None
project_init._git = lambda path, *args: 'a' * 40
automation.provider_available = lambda provider: True
jobs.pump = lambda: None
project_service.recover = lambda: None

calls = []
fail_external = [False]
project_service._command = lambda args, label, cwd=None, environment=None: calls.append(label)
project_service._restart = lambda: calls.append('restart')
project_service._restart_permission = lambda: None
project_service.binary = lambda: 'cloudflared'
project_service.pm2_launcher = lambda: ['pm2']


def _healthy(url, seconds=30):
    calls.append(url)
    if url.startswith('https://') and fail_external[0]:
        raise issues.StoreError('서비스 health 확인에 실패했어요: ' + url, 409)
project_service._healthy = _healthy

real_run = project_service._run
run_calls = []


def _run(project, action):
    run_calls.append(project['key'])
    real_run(project, action)
project_service._run = _run

PLAN = '## Tasks\n1. 첫 작업 | 파일: app.py | 선행: 없음\n'
RESULT = {'description': '설명', 'runtime': {'kind': 'python', 'entry': 'app.py'}, 'files': [],
          'issues': [{'title': '기능', 'body': '본문', 'plan': PLAN}]}
project_init.generate = lambda row, correction='': RESULT
project_init.validate = lambda result, root, row=None: result


def _materialize(row, result):
    target = Path(row['local_path'])
    target.mkdir(exist_ok=True)
    (target / 'ecosystem.config.js').write_text('module.exports={apps:[]}', encoding='utf-8')
project_init.materialize = _materialize

HUMAN = {'kind': 'human', 'name': 'admin', 'id': 1}
counter = iter(range(1, 1000))


def body(**over):
    n = next(counter)
    b = {'request_id': str(uuid.uuid4()), 'key': f'S{n}', 'name': f'서비스 {n}', 'folder': f'svc{n}',
         'provider': 'codex', 'spec_markdown': '# 기획\n내용', 'service_port': 8500 + n}
    b.update(over)
    return b


def check(cond, label):
    if not cond:
        raise AssertionError(label)
    print('ok', label)


def rejected(b, code):
    try:
        project_init.request(HUMAN, b)
    except issues.StoreError as e:
        return e.status == code
    return False


def state(key):
    with db.connect() as c:
        init = c.execute('SELECT r.* FROM project_initializations r JOIN projects p ON p.id=r.project_id WHERE p.key=?', (key,)).fetchone()
        svc = c.execute('SELECT * FROM project_services WHERE project_id=?', (init['project_id'],)).fetchone()
        n = c.execute('SELECT COUNT(*) FROM issues WHERE project_id=?', (init['project_id'],)).fetchone()[0]
    return dict(init), dict(svc) if svc else None, n


def hosts():
    return [r.get('hostname') for r in yaml.safe_load(TUNNEL.read_text(encoding='utf-8'))['ingress']]


# (a) 도메인이 있으면 연결 뒤 complete
b = body(service_domain='a1.lomebrote.com')
project_init.request(HUMAN, b)
project_init._run(b['key'])
init, svc, n = state(b['key'])
check(init['status'] == 'complete' and n >= 1, '(a) 연결 뒤 초기 생성 complete·Issue 등록')
check(svc and svc['status'] == 'connected' and svc['domain'] == 'a1.lomebrote.com' and svc['actor'] == project_service.INIT_ACTOR, '(a) project_services connected')
check(hosts()[0] == 'a1.lomebrote.com' and 'dev.lomebrote.com' in hosts(), '(a) 새 hostname이 맨 앞, 기존 규칙 유지')
check('DNS 연결' in calls and 'https://a1.lomebrote.com/api/health' in calls, '(a) DNS·외부 HTTPS 확인 실행')
check(project_init.status(b['key'])['service']['status'] == 'connected', '(a) status에 service 포함')
after_a = TUNNEL.read_text(encoding='utf-8')

# (b) 외부 HTTPS 실패 — 생성 결과는 지키고 터널은 복원
fail_external[0] = True
b2 = body(service_domain='b1.lomebrote.com')
project_init.request(HUMAN, b2)
project_init._run(b2['key'])
init, svc, n = state(b2['key'])
check(init['status'] == 'complete' and n >= 1 and json.loads(init['refs_json']), '(b) 연결 실패에도 complete·Issue 유지')
check(svc['status'] == 'failed' and svc['error'], '(b) service failed·error 있음')
check(TUNNEL.read_text(encoding='utf-8') == after_a, '(b) 터널 설정 원본으로 복원')
fail_external[0] = False

# (c)(d) 도메인 제한
check(rejected(body(service_domain='dev.lomebrote.com'), 400), '(c) 기존 hostname 400')
check(rejected(body(service_domain='app.example.com'), 400), '(d) 허용 영역 밖 400')
check(rejected(body(service_domain='lomebrote.com'), 400), '(d) 영역 자체도 400')

# (e) 도메인 없으면 연결 단계 생략
b3 = body()
project_init.request(HUMAN, b3)
before = len(run_calls)
project_init._run(b3['key'])
init, svc, n = state(b3['key'])
check(init['status'] == 'complete' and svc is None and len(run_calls) == before, '(e) 도메인 없음: _run 미호출·행 없음')

# (f) 다른 줄이 바쁘면 대기 상한 뒤 failed
project_service.INIT_CONNECT_WAIT_SEC = 0
jobs._threads[('OTHER', 'review')] = 1
b4 = body(service_domain='f1.lomebrote.com')
project_init.request(HUMAN, b4)
before = len(run_calls)
project_init._run(b4['key'])
del jobs._threads[('OTHER', 'review')]
init, svc, n = state(b4['key'])
check(init['status'] == 'complete' and n >= 1, '(f) 대기 상한에도 생성 결과 유지')
check(svc['status'] == 'failed' and '끝나지 않아' in svc['error'] and len(run_calls) == before, '(f) 연결 미실행·failed')
with db.connect() as c:
    check(not c.execute("SELECT 1 FROM project_services WHERE status='running'").fetchone(), '(f) 터널 잠금 해제')
project_service.INIT_CONNECT_WAIT_SEC = 1800

# (g) refs가 있으면 다시 돌려도 Issue를 만들지 않음 — 연결 중 재시작(recover) 뒤 [다시 생성]
b5 = body(service_domain='g1.lomebrote.com')
project_init.request(HUMAN, b5)
original_connect = project_service.connect_for_initialization
project_service.connect_for_initialization = lambda project: (_ for _ in ()).throw(RuntimeError('재시작'))
project_init._run(b5['key'])
project_service.connect_for_initialization = original_connect
init, svc, n_before = state(b5['key'])
check(init['status'] == 'failed' and json.loads(init['refs_json']), '(g) 연결 중 중단은 failed·refs 보존')
try:
    project_init.cancel(HUMAN, b5['key'])
    check(False, '(g) Issue 등록 뒤 취소 409')
except issues.StoreError as e:
    check(e.status == 409, '(g) Issue 등록 뒤 취소 409')
project_init.retry(HUMAN, b5['key'])
project_init._run(b5['key'])
init, svc, n_after = state(b5['key'])
check(init['status'] == 'complete' and n_after == n_before and svc['status'] == 'connected', '(g) 재시도: Issue 수 그대로·연결 완료')

# (h) 화면 버튼 경로는 사람 전용 그대로
try:
    project_service.request({'kind': 'agent', 'id': 1, 'name': 'bot'}, b3['key'], 'connect')
    check(False, '(h) 에이전트 request 403')
except issues.StoreError as e:
    check(e.status == 403, '(h) 에이전트 request 403')

print('all passed')
