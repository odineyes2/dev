"""프로젝트 비밀값(DEV-239-1) — `.env` 쓰기·보존·지우기·거절, 사람 전용 쓰기·에이전트 응답, 재시작 호출, pm2 세 경로 env 주입, 실행기 `.env` 읽기 차단을 임시 DB·저장소로 검사한다."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix='dev-secrets-'))
os.environ['DEV_DATA_DIR'] = str(TMP / 'data')
os.environ['DEV_DB_PATH'] = str(TMP / 'data' / 'dev.db')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import auth  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import execute  # noqa: E402
import issues  # noqa: E402
import jobs  # noqa: E402
import orchestrate  # noqa: E402
import project_init  # noqa: E402
import project_restart  # noqa: E402
import project_secrets  # noqa: E402
import project_service  # noqa: E402
import rollback  # noqa: E402

rollback.start_recovery = lambda **kw: None
orchestrate.take_over = lambda: None
jobs.start_timer = lambda: None
jobs.pump = lambda: None
project_init.recover = lambda: None
project_service.recover = lambda: None


async def _admin(cookie):
    return {'id': 1, 'username': 'admin', 'role': 'admin'} if cookie == 'ok' else None
auth.nightshift_user = _admin

HUMAN = {'cookies': {'ns_session': 'ok'}, 'headers': {'x-requested-with': 'dev'}}
ADMIN = {'kind': 'human', 'name': 'admin'}
SECRET = 'sk-live-abcdef123456'
SPACED = 'two words #x'


def check(cond, label):
    if not cond:
        raise AssertionError(label)
    print('ok', label)


def refused(fn, status, label):
    try:
        fn()
    except issues.StoreError as error:
        check(error.status == status, f'{label} ({status})')
        return str(error)
    raise AssertionError(label + ' — 거절되지 않음')


def make_repo(name, ignore=True):
    repo = TMP / name
    repo.mkdir()
    subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
    (repo / '.env.example').write_text('# 예시\nLLM_API_KEY=\nexport OTHER_TOKEN=\nLLM_API_KEY=\nDEV_SECRET=\n', encoding='utf-8')
    if ignore:
        (repo / '.gitignore').write_text('.env\n', encoding='utf-8')
    (repo / '.env').write_text('# 기존 주석\nKEEP=1\nPORT=7777\n', encoding='utf-8')
    return repo


restart_calls = []


def fake_request(actor, key, mode='restart'):
    restart_calls.append(key)
    if fake_request.fail:
        raise issues.StoreError('다른 앱의 재시작이 진행 중이에요.', 409)
    return {'status': 'waiting', 'phase': '진행 중인 작업 종료 대기', 'error': ''}
fake_request.fail = False
project_restart.request = fake_request

config.DATA_DIR.mkdir(parents=True, exist_ok=True)
db.init()
repo = make_repo('sc')
bare = make_repo('bare', ignore=False)
issues.create_project(ADMIN, 'SC', '비밀', local_path=str(repo))
issues.create_project(ADMIN, 'BR', '무시 없음', local_path=str(bare))
issues.create_project(ADMIN, 'DV', 'dev 자신', local_path=str(config.REPO_ROOT))

# --- 키 읽기·저장 규칙 ---
check(project_secrets.keys(repo) == ['LLM_API_KEY', 'OTHER_TOKEN', 'DEV_SECRET'], '.env.example 키 순서·중복 제거·export')
r = project_secrets.save(ADMIN, 'SC', {'LLM_API_KEY': {'value': f'  {SECRET} '}, 'OTHER_TOKEN': {'value': SPACED}})
text = (repo / '.env').read_text(encoding='utf-8')
check(text == f'# 기존 주석\nKEEP=1\nPORT=7777\nLLM_API_KEY={SECRET}\nOTHER_TOKEN="{SPACED}"\n', '저장 — 기존 키·주석 보존, 공백·# 값은 따옴표')
check(r['restart'] == {'status': 'skipped'} and not restart_calls, 'service_app 없으면 재시작 생략')
check(project_secrets.env_for(repo)['OTHER_TOKEN'] == SPACED, 'env_for가 따옴표 한 겹을 벗김')
project_secrets.save(ADMIN, 'SC', {'LLM_API_KEY': {'value': 'replaced-value-9999'}})
check('LLM_API_KEY=replaced-value-9999\n' in (repo / '.env').read_text(encoding='utf-8'), '같은 키 줄 바꾸기')
project_secrets.save(ADMIN, 'SC', {'LLM_API_KEY': {'value': SECRET}})
project_secrets.save(ADMIN, 'SC', {'OTHER_TOKEN': {'clear': True}})
check((repo / '.env').read_text(encoding='utf-8') == f'# 기존 주석\nKEEP=1\nPORT=7777\nLLM_API_KEY={SECRET}\n', '지우기는 그 줄만')
for bad, label in (('a\nB=1', '개행'), ('a"b', '큰따옴표'), ('a\\b', '역슬래시'), ('   ', '빈 값'), ('x' * 4097, '너무 긴 값')):
    message = refused(lambda: project_secrets.save(ADMIN, 'SC', {'LLM_API_KEY': {'value': bad}}), 400, f'{label} 거절')
    check(bad.strip()[:20] not in message or not bad.strip(), f'{label} 오류 문구에 값 없음')
refused(lambda: project_secrets.save(ADMIN, 'SC', {'KEEP': {'value': 'x'}}), 400, '.env.example 밖 키 거절')
refused(lambda: project_secrets.save(ADMIN, 'SC', {'LLM_API_KEY': {'value': 'x', 'clear': True}}), 400, '변경 형식 거절')
refused(lambda: project_secrets.save(ADMIN, 'BR', {'LLM_API_KEY': {'value': SECRET}}), 409, '.gitignore에 .env 없으면 거절')
check(SECRET not in (bare / '.env').read_text(encoding='utf-8'), '거절 때 .env 그대로')
refused(lambda: project_secrets.save(ADMIN, 'DV', {'X': {'value': 'y'}}), 409, 'dev 자신 거절')
refused(lambda: project_secrets.save({'kind': 'agent', 'id': 1}, 'SC', {'LLM_API_KEY': {'value': 'x'}}), 403, '에이전트 저장 함수 거절')

# --- dev 값 그대로 쓰기 ---
os.environ['OTHER_TOKEN'] = 'from-dev-value-42'
os.environ['DEV_SECRET'] = 'dev-only-value'
st = {i['key']: i for i in project_secrets.status(ADMIN, 'SC')['keys']}
check(st['OTHER_TOKEN']['dev_available'] and not st['DEV_SECRET']['dev_available'] and not st['LLM_API_KEY']['dev_available'], 'dev_available은 허용 이름만')
project_secrets.save(ADMIN, 'SC', {'OTHER_TOKEN': {'from_dev': True}})
check(project_secrets.env_for(repo)['OTHER_TOKEN'] == 'from-dev-value-42', 'from_dev 복사')
refused(lambda: project_secrets.save(ADMIN, 'SC', {'DEV_SECRET': {'from_dev': True}}), 400, 'DEV_* from_dev 거절')
check('dev-only-value' not in (repo / '.env').read_text(encoding='utf-8'), 'DEV_* 값이 .env에 없음')

# --- 재시작 연결 ---
with db.connect() as c:
    c.execute("UPDATE projects SET service_app='scapp' WHERE key='SC'")
r = project_secrets.save(ADMIN, 'SC', {'LLM_API_KEY': {'value': SECRET}})
check(restart_calls == ['SC'] and r['restart']['status'] == 'waiting', 'service_app 있으면 재시작 한 번')
fake_request.fail = True
project_secrets.save(ADMIN, 'SC', {'OTHER_TOKEN': {'value': 'after-failure-77'}})
r = project_secrets.save(ADMIN, 'SC', {'OTHER_TOKEN': {'value': 'after-failure-88'}})
check(r['restart']['status'] == 'failed' and r['restart']['error'], '재시작 StoreError면 failed')
check(project_secrets.env_for(repo)['OTHER_TOKEN'] == 'after-failure-88', '재시작 실패여도 .env 저장 유지')
fake_request.fail = False

# --- REST ---
with TestClient(app_module.app) as client:
    _, agent_key = auth.create_agent('테스트봇', 'anthropic', 'm')
    bearer = {'authorization': f'Bearer {agent_key}'}
    r = client.put('/api/projects/SC/secrets', json={'changes': {'LLM_API_KEY': {'value': 'agent-wrote'}}}, headers=bearer)
    check(r.status_code == 403, '에이전트 PUT 403')
    check('agent-wrote' not in (repo / '.env').read_text(encoding='utf-8'), '에이전트 PUT은 .env를 안 바꿈')
    r = client.get('/api/projects/SC/secrets', headers=bearer)
    check(r.status_code == 200 and all(set(i) == {'key', 'set'} for i in r.json()['keys']), '에이전트 GET은 key·set만')
    check(SECRET not in r.text and SECRET[-4:] not in r.text, '에이전트 GET에 값·끝자리 없음')
    r = client.get('/api/projects/SC/secrets', **HUMAN)
    items = {i['key']: i for i in r.json()['keys']}
    check(r.status_code == 200 and items['LLM_API_KEY'] == {'key': 'LLM_API_KEY', 'set': True, 'last4': SECRET[-4:], 'dev_available': False}, '사람 GET은 last4')
    check(SECRET not in r.text and 'after-failure-88' not in r.text, '사람 GET에 원래 값 없음')
    r = client.put('/api/projects/SC/secrets', json={'changes': {'LLM_API_KEY': {'value': 'rest-value-abcd1234'}}}, **HUMAN)
    check(r.status_code == 200 and r.json()['restart']['status'] == 'waiting', '사람 PUT 200·재시작 상태')
    check('rest-value-abcd1234' not in r.text and project_secrets.env_for(repo)['LLM_API_KEY'] == 'rest-value-abcd1234', 'PUT 응답에 값 없음·저장됨')
    r = client.put('/api/projects/SC/secrets', json={'changes': {'LLM_API_KEY': {'value': 'bad"leak-zz'}}}, **HUMAN)
    check(r.status_code == 400 and 'leak-zz' not in r.text, '거절 응답에 값 없음')
    r = client.put('/api/projects/SC/secrets', json={'changes': {'LLM_API_KEY': {'value': ['list-leak-yy']}}}, **HUMAN)
    check(r.status_code == 400 and 'list-leak-yy' not in r.text, '형식 오류 응답에 값 없음')
    r = client.get('/api/projects/DV/secrets', **HUMAN)
    check(r.status_code == 409, 'dev 자신 GET 409')

with db.connect() as c:
    dump = json.dumps([[dict(x) for x in c.execute(f'SELECT * FROM {t}').fetchall()] for t in ('events', 'runs', 'projects')], ensure_ascii=False)
for value in (SECRET, SPACED, 'from-dev-value-42', 'after-failure-88', 'rest-value-abcd1234'):
    check(value not in dump, f'events·runs·projects에 값 없음: {value[:6]}…')

# --- pm2 세 경로 env 주입 ---
seen = {}
old_command, old_launcher = project_service._command, project_service.pm2_launcher
project_service._command = lambda args, label, cwd=None, environment=None, **kw: seen.update(args=args, env=environment)
project_service.pm2_launcher = lambda: ['pm2']
try:
    project_restart._restart({'health_url': 'http://127.0.0.1:9123/api/health', 'self_restart': 0, 'repo': str(repo), 'app': 'scapp'})
    check(seen['env']['LLM_API_KEY'] == 'rest-value-abcd1234' and seen['env']['KEEP'] == '1', '앱 재시작 env에 .env 값')
    check(seen['env']['PORT'] == '9123' and seen['env']['HOST'] == '127.0.0.1', '앱 재시작 PORT·HOST가 .env를 덮음')
    project_restart._restart({'health_url': f'http://127.0.0.1:{config.PORT}/api/health', 'self_restart': 1, 'repo': str(config.REPO_ROOT), 'app': 'dev'})
    check('LLM_API_KEY' not in seen['env'], 'dev 자기 재시작은 .env 주입 없음')
finally:
    project_service._command, project_service.pm2_launcher = old_command, old_launcher
env = project_service.app_environment(repo, 9124)
check(env['LLM_API_KEY'] == 'rest-value-abcd1234' and env['PORT'] == '9124' and env['HOST'] == '127.0.0.1', '서비스 연결 앱 시작 env에 .env 값·PORT·HOST')

ran = {}
old_run = orchestrate.subprocess.run
orchestrate.subprocess.run = lambda cmd, **kw: ran.update(cmd=cmd, env=kw.get('env')) or subprocess.CompletedProcess(cmd, 0, '', '')
try:
    check(orchestrate._restart(str(repo), {'restart_cmd': 'pm2 restart {app}', 'pm2_app': 'scapp'}) is None, '병합 뒤 재시작 호출')
    check(ran['env']['LLM_API_KEY'] == 'rest-value-abcd1234' and ran['cmd'] == 'pm2 restart scapp', '병합 뒤 재시작 env에 .env 값')
    orchestrate._restart(str(config.REPO_ROOT), {'restart_cmd': 'pm2 restart {app}', 'pm2_app': 'dev'})
    check(ran['env'] is None, 'dev 자기 병합 재시작 env는 그대로')
finally:
    orchestrate.subprocess.run = old_run

# --- 실행기 차단 ---
check('Read(**/.env)' in execute.BLOCKED_TOOLS, '실행기 Read(**/.env) 차단')
print('all ok')
