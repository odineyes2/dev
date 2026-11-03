"""기획서 본문 초기 생성(DEV-225-1) — 본문 검증·첨부 호환·중복 요청·REST/MCP 인증 경계·에이전트 작성자·승인 전 실행 거절을 임시 DB로 검사한다."""
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix='dev-init-text-'))
os.environ['DEV_DATA_DIR'] = str(TMP / 'data')
os.environ['DEV_DB_PATH'] = str(TMP / 'data' / 'dev.db')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import attachments  # noqa: E402
import auth  # noqa: E402
import automation  # noqa: E402
import db  # noqa: E402
import execute  # noqa: E402
import issues  # noqa: E402
import jobs  # noqa: E402
import orchestrate  # noqa: E402
import project_init  # noqa: E402
import project_service  # noqa: E402
import rollback  # noqa: E402

(TMP / 'projects').mkdir()
project_init.ROOT = TMP / 'projects'
started = []
project_init.start = started.append
automation.provider_available = lambda provider: True
project_init._git = lambda path, *args: 'a' * 40
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
counter = iter(range(1, 1000))


def body(**over):
    n = next(counter)
    b = {'request_id': str(uuid.uuid4()), 'key': f'T{n}', 'name': f'테스트 {n}', 'folder': f'proj{n}',
         'provider': 'codex', 'spec_markdown': '# 기획\n내용'}
    b.update(over)
    return b


def check(cond, label):
    if not cond:
        raise AssertionError(label)
    print('ok', label)


def mcp_call(client, key, tool, args=None):
    r = client.post('/mcp/', headers={'authorization': f'Bearer {key}', 'accept': 'application/json, text/event-stream'},
                    json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': tool, 'arguments': args or {}}})
    if r.status_code != 200:
        return r.status_code, None
    result = r.json()['result']
    if result.get('isError'):
        return 'error', result['content'][0]['text']
    return 200, result.get('structuredContent', {}).get('result', result.get('structuredContent'))


with TestClient(app_module.app) as client:
    # ASCII 이스케이프로 보내 짝 없는 서로게이트도 JSON 그대로 서버에 닿게 한다.
    post = lambda b: client.post('/api/projects/initialize', content=json.dumps(b),
                                 cookies=HUMAN['cookies'], headers={**HUMAN['headers'], 'content-type': 'application/json'})

    # --- REST 본문 경로 ---
    b = body(spec_markdown='﻿# 기획\n본문')
    r = post(b)
    check(r.status_code == 202 and r.json()['status'] == 'queued', '본문만으로 202')
    with db.connect() as c:
        row = c.execute('SELECT r.spec,r.owner FROM project_initializations r JOIN projects p ON p.id=r.project_id WHERE p.key=?', (b['key'],)).fetchone()
        n_att = c.execute('SELECT COUNT(*) FROM attachments').fetchone()[0]
    check(row['spec'] == '# 기획\n본문' and row['owner'] == 'human:admin', 'DB spec 일치(BOM 제거)·사람 요청자')
    check(n_att == 0, '본문 경로는 첨부를 만들지 않음')

    for bad, label in [('', '빈 문자열'), ('  \n\t ', '공백뿐'), ('﻿ \n', 'BOM·공백뿐'), (12, '숫자'), (None, 'null'), ('a\ud800b', '짝 없는 서로게이트')]:
        r = post(body(spec_markdown=bad))
        check(r.status_code == 400, f'{label} 400')
    r = post({k: v for k, v in body().items() if k != 'spec_markdown'})
    check(r.status_code == 400, '출처 누락 400')

    exact = '가' * 66666 + 'ab'   # 3*66666+2 = 200000바이트
    check(len(exact.encode()) == 200000, '정확히 200000바이트 준비')
    check(post(body(spec_markdown=exact)).status_code == 202, '200000바이트 통과')
    check(post(body(spec_markdown=exact + 'c')).status_code == 413, '200001바이트 413')

    # --- 기존 첨부 경로 ---
    human = {'kind': 'human', 'name': 'admin', 'id': 1}
    a = attachments.upload(human, 'spec.md', [b'# file spec\n'])
    check(post(body(spec_markdown='x', attachment_id=a['id'])).status_code == 400, '첨부·본문 동시 400')
    b = {k: v for k, v in body().items() if k != 'spec_markdown'}
    r = post({**b, 'attachment_id': a['id']})
    check(r.status_code == 202, '기존 첨부 경로 202')
    # 잘못된 UTF-8 파일은 첨부 단계에서 이미 거절된다(생성 요청까지 오지 않는다).
    try:
        attachments.upload(human, 'bad.md', [b'\xff\xfe broken'])
        check(False, '잘못된 UTF-8 파일 거절')
    except issues.StoreError:
        check(True, '잘못된 UTF-8 파일 거절')

    # --- 중복 요청 ---
    b = body()
    check(post(b).status_code == 202, '새 요청')
    n_start = len(started)
    with db.connect() as c:
        n_proj = c.execute('SELECT COUNT(*) FROM projects').fetchone()[0]
    again = post({**b, 'spec_markdown': ''})   # 재요청은 기획서를 다시 읽지 않는다
    with db.connect() as c:
        same_proj = c.execute('SELECT COUNT(*) FROM projects').fetchone()[0] == n_proj
    check(again.status_code == 202 and len(started) == n_start and same_proj, '같은 request_id 재요청은 생성·시작 없음')
    check(post({**b, 'key': 'OTHER'}).status_code == 409, '같은 ID 다른 Key 409')

    # --- 에이전트: REST는 막고 MCP만 연다 ---
    agent, key = auth.create_agent('테스트봇', 'anthropic', 'm')
    r = client.post('/api/projects/initialize', json=body(), headers={'authorization': f'Bearer {key}'})
    check(r.status_code == 403, '에이전트 REST 403 유지')
    try:
        project_init.request({'kind': 'system', 'name': 'x'}, body(), allow_agent=True)
        check(False, '임의 kind 거절')
    except issues.StoreError:
        check(True, '임의 kind 거절')

    r = client.post('/mcp/', headers={'accept': 'application/json, text/event-stream'},
                    json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'list_project_templates', 'arguments': {}}})
    check(r.status_code == 401, 'MCP 키 없음 401')
    code, catalog = mcp_call(client, key, 'list_project_templates')
    ids = {t['id'] for t in catalog}
    genres = {g['id'] for t in catalog for g in t.get('genres') or []}
    check(code == 200 and 'web-game' in ids and 'novel' in genres, 'MCP 카탈로그에 web-game/novel')

    b = body()
    args = {k: b[k] for k in ('request_id', 'key', 'name', 'folder', 'provider', 'spec_markdown')}
    code, res = mcp_call(client, key, 'request_project_initialization', args)
    check(code == 200 and res['status'] == 'queued', 'MCP 생성 등록')
    with db.connect() as c:
        owner = c.execute('SELECT r.owner FROM project_initializations r JOIN projects p ON p.id=r.project_id WHERE p.key=?', (b['key'],)).fetchone()['owner']
    check(owner == f"agent:{agent['id']}", '요청자는 에이전트')
    code, st = mcp_call(client, key, 'project_initialization_status', {'key': b['key']})
    check(code == 200 and st['key'] == b['key'] and 'spec' not in st, 'MCP 상태 조회(spec 없음)')
    n_start = len(started)
    code, res = mcp_call(client, key, 'request_project_initialization', args)
    check(code == 200 and len(started) == n_start, 'MCP 같은 ID 재요청은 그대로')
    code, msg = mcp_call(client, key, 'request_project_initialization', {**args, 'request_id': str(uuid.uuid4()), 'key': 'ZZ1', 'folder': 'zz1', 'spec_markdown': '   '})
    check(code == 'error' and '비어' in msg, 'MCP 빈 본문 ToolError')

    # --- publish: 에이전트 작성자, 승인·실행은 만들지 않음 ---
    with db.connect() as c:
        c.execute("UPDATE project_initializations SET status='running' WHERE request_id=?", (b['request_id'],))
    row = project_init.get(b['key'])
    plan = '## Tasks\n1. 첫 작업 | 파일: app.py | 선행: 없음\n'
    project_init.publish(row, {'description': '설명', 'issues': [{'title': '기능', 'body': '본문', 'plan': plan}]})
    label = f"agent:{agent['id']}"
    with db.connect() as c:
        pid = row['project_id']
        parent = c.execute('SELECT id,reporter FROM issues WHERE project_id=? AND parent_id IS NULL', (pid,)).fetchone()
        children = c.execute('SELECT reporter FROM issues WHERE parent_id=?', (parent['id'],)).fetchall()
        author = c.execute('SELECT author FROM plans WHERE issue_id=?', (parent['id'],)).fetchone()['author']
        actors = {r[0] for r in c.execute('SELECT actor FROM events WHERE issue_id IN (SELECT id FROM issues WHERE project_id=?)', (pid,))}
        n_dec = c.execute('SELECT COUNT(*) FROM decisions WHERE issue_id IN (SELECT id FROM issues WHERE project_id=?)', (pid,)).fetchone()[0]
        n_jobs = c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
    check(parent['reporter'] == label and author == label, '부모 reporter·plan author가 에이전트')
    check(children and all(ch['reporter'] == label for ch in children), '하위 Task reporter가 에이전트')
    check(actors == {label}, 'event actor가 에이전트')
    check(n_dec == 0 and n_jobs == 0, '승인·실행 작업 없음')
    refs = json.loads(project_init.get(b['key'])['refs_json'])
    parent_issue = issues.get_issue(refs[0])
    child_issue = issues.get_issue(parent_issue['children'][0]['ref'])
    reason = execute.blocked_reason(child_issue, parent_issue)
    check(reason and '승인되지' in reason, '사람 승인 전 실행 거절')

print('all passed')
