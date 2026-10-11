"""UI ??? ??? ???? ?? ??? ?? ? ?? ???."""
import os, socket, subprocess, sys, time
import copy
from pathlib import Path


def audit_fixture(status='complete', variant='report', audit_id=1):
    """유료 호출 없이 점검 보고서와 근거·후보 상태를 재현한다."""
    evidence = [{'source_id': 'git:PROJECT_SPEC.md', 'section': '목표', 'line_start': 1, 'line_end': 2},
                {'source_id': 'git:static/app.js', 'line_start': 10, 'line_end': 12},
                {'source_id': 'issue:DEV-1'}]
    requirements = [{'id': 'r1', 'title': '목표와 현재 구현 비교', 'status': 'partial',
                     'assessment': '보고서는 저장되지만 모바일 근거 표시를 보완해야 해요.', 'evidence': evidence},
                    {'id': 'r2', 'title': '외부 계정 확인', 'status': 'needs_confirmation',
                     'assessment': '실제 계정 확인은 사람이 진행해야 해요.', 'evidence': evidence[:1]}]
    candidates = []
    for cid, title, disposition in [('c1', '보고서 근거 표시 개선', 'new_work'),
                                     ('c2', '후보 선택 접근성 개선', 'new_work'),
                                     ('c3', '기존 작업 해결 우선', 'reuse_issue')]:
        candidates.append({'id': cid, 'title': title, 'disposition': disposition,
                           'requirement_ids': ['r1'], 'evidence': evidence, 'gap': '화면과 목표 사이의 차이를 줄여요.',
                           'priority': 'high', 'scope': '프로젝트 점검 화면만 수정해요.',
                           'completion_criteria': ['근거 경로가 좁은 화면에서도 잘리지 않아요.', '키보드로 후보를 선택할 수 있어요.'],
                           'prerequisites': [] if disposition == 'new_work' else ['기존 Issue를 먼저 확인해 주세요.'],
                           'plan_draft': '## Tasks\n1. 화면 개선 | 파일: static/app.js | 확인: 모바일 근거 표시 | 선행: 없음',
                           'related_issues': ['DEV-1'] if disposition == 'reuse_issue' else []})
    if variant in ('empty', 'insufficient'):
        candidates = []
        requirements = [] if variant == 'insufficient' else [dict(requirements[0], status='complete')]
    if variant == 'confirmation':
        candidates[0]['disposition'] = 'human_confirmation'
    snapshot = {'sources': [{'id': 'git:PROJECT_SPEC.md', 'kind': 'document', 'path': 'PROJECT_SPEC.md', 'hash': 'a'*40},
                            {'id': 'git:static/app.js', 'kind': 'code', 'path': 'static/app.js', 'hash': 'b'*40},
                            {'id': 'issue:DEV-1', 'kind': 'issue', 'issue_ref': 'DEV-1', 'hash': 'c'*40}],
                'warnings': ['보존된 자료만 비교했어요.'],
                'omissions': [{'source_id': 'git:USER_GUIDE.md', 'reason': 'missing'}]}
    if variant == 'insufficient':
        snapshot['sources'] = []
        snapshot['warnings'] = ['목표 자료가 부족해요. 확인이 필요해요.']
    return {'id': audit_id, 'request_id': f'fixture-audit-{audit_id}', 'provider': 'codex', 'model': 'gpt-6.1-sol', 'actual_model': 'gpt-6.1-sol',
            'status': status, 'phase': '자료를 비교해요', 'created_at': '2026-10-10T01:00:00+00:00',
            'started_at': '2026-10-10T01:00:01+00:00', 'ended_at': None if status in ('preparing','running','cancelling') else '2026-10-10T01:01:00+00:00',
            'base_sha': '1234567890abcdef'*2 + '12345678', 'base_branch': 'main',
            'limits': {'timeout_sec': 1200, 'budget_usd': None, 'cost_enforced': False},
            'usage': {'cost_measured': False, 'cost_usd': None, 'input_tokens': 1200, 'output_tokens': 500},
            'error': '분석을 완료하지 못했어요.' if status in ('failed','timeout') else '',
            'stale': variant == 'stale', 'stale_reason': '기준 문서와 코드가 변경됐어요.' if variant == 'stale' else '',
            'snapshot': snapshot, 'publications': [],
            'report': {'summary': '목표 대비 구현 차이를 확인했어요. 기존 작업을 먼저 해결해 주세요.',
                       'requirements': requirements, 'candidates': candidates} if status == 'complete' else None}


def mock_audits(page, key='DEV'):
    """로컬 UI용 API만 대체하며 호출·충돌·응답 유실을 제어한다."""
    state = {'audits': [audit_fixture()], 'blocked': '', 'available': True, 'requests': [],
             'publish_error': '', 'link_error': '', 'start_lost': False, 'publish_lost': False, 'cancel_pending': False}

    def handle(route):
        request = route.request
        suffix = request.url.split('/audits', 1)[1]
        state['requests'].append((request.method, suffix, request.post_data_json if request.method == 'POST' else None))
        if suffix.startswith('/preflight'):
            provider = 'claude' if 'provider=claude' in suffix else 'codex'
            route.fulfill(json={'provider': provider, 'model': 'selected-model',
                                'applied_model': 'claude-opus-5-5' if provider == 'claude' else 'gpt-6.1-sol',
                                'available': state['available'], 'blocked_reason': state['blocked'],
                                'queued_jobs': [{'id': 1}], 'notes': [],
                                'limits': {'timeout_sec': 1200, 'budget_usd': 2 if provider == 'claude' else None,
                                           'cost_enforced': provider == 'claude'}})
            return
        if request.method == 'GET' and not suffix:
            route.fulfill(json={'audits': copy.deepcopy(state['audits'])}); return
        if request.method == 'POST' and not suffix:
            if state['start_lost']:
                state['start_lost'] = False; route.abort('failed'); return
            row = audit_fixture('running', audit_id=max([r['id'] for r in state['audits']] or [0])+1)
            row['provider'] = request.post_data_json['provider']
            state['audits'].insert(0, row)
            route.fulfill(json=row); return
        aid = int(suffix.split('/')[1])
        row = next(r for r in state['audits'] if r['id'] == aid)
        if suffix.endswith('/cancel'):
            row['status'] = 'cancelling' if state['cancel_pending'] else 'cancelled'
        if suffix.endswith('/link'):
            if state['link_error']:
                route.fulfill(status=409, json={'detail': state['link_error']}); return
            body = request.post_data_json
            publication = next((p for p in row['publications'] if p['candidate_id'] == body['candidate_id']), None)
            if publication is None:
                publication = {'candidate_id': body['candidate_id'], 'issue_ref': body['issue_ref'], 'issue_id': 1}
                row['publications'].append(publication)
            route.fulfill(json={'publication': publication}); return
        if suffix.endswith('/publish'):
            if state['publish_error']:
                route.fulfill(status=409, json={'detail': state['publish_error']}); return
            for cid in request.post_data_json['candidate_ids']:
                if not any(p['candidate_id'] == cid for p in row['publications']):
                    row['publications'].append({'candidate_id': cid, 'issue_ref': 'DEV-1', 'issue_id': 1})
            if state['publish_lost']:
                state['publish_lost'] = False; route.abort('failed'); return
            route.fulfill(json={'publications': row['publications']}); return
        route.fulfill(json=copy.deepcopy(row))

    page.route(f'**/api/projects/{key}/audits**', handle)
    return state, handle

FAKE_NS = r'''
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
app = FastAPI()
@app.get("/api/auth/me")
def me(request: Request):
    users = {"adm": {"id": 1, "username": "admin", "role": "admin"},
             "mem": {"id": 2, "username": "admin", "role": "user"}}
    return {"user": users.get(request.cookies.get("ns_session"))}
@app.post("/api/auth/login")
async def login(request: Request):
    b = await request.json()
    if b.get("password") != "pw":
        return JSONResponse({"detail": "아이디 또는 비밀번호가 올바르지 않아요."}, 401)
    r = JSONResponse({"user": {"id": 1, "username": "admin", "role": "admin"}})
    r.set_cookie("ns_session", "adm", path="/")
    return r
@app.post("/api/auth/logout")
def logout():
    r = JSONResponse({}); r.delete_cookie("ns_session", path="/"); return r
'''


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def wait_port(port):
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close(); return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"port {port} not up")


def child_environment(directory, ns_port, dev_port):
    """시스템 경로만 전달하고 운영 설정과 비밀값을 제거한다."""
    allowed = {'SYSTEMROOT', 'WINDIR', 'PATH', 'PATHEXT', 'COMSPEC',
               'LOCALAPPDATA', 'APPDATA', 'USERPROFILE', 'TEMP', 'TMP', 'VIRTUAL_ENV'}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    scratch = directory / 'tmp'
    scratch.mkdir(parents=True, exist_ok=True)
    env.update(DEV_DATA_DIR=str(directory / 'data'),
               DEV_DB_PATH=str(directory / 'data' / 'dev.db'),
               DEV_PORT=str(dev_port),
               DEV_NIGHTSHIFT_URL=f'http://127.0.0.1:{ns_port}',
               DEV_NIGHTSHIFT_PUBLIC_URL=f'http://127.0.0.1:{ns_port}',
               DEV_PUBLIC_URL=f'http://127.0.0.1:{dev_port}',
               DEV_CLAUDE_BIN=str(directory / 'disabled-claude'),
               DEV_CODEX_BIN=str(directory / 'disabled-codex'),
               DEV_REVIEW_MCP_CONFIG=str(directory / 'no-mcp.json'),
               TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch),
               FASTMCP_HOME=str(directory / 'fastmcp'),
               PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
    return env



# 앱 lifespan 대신 빈 DB만 초기화한다. 복구·병합·작업 타이머는 시작하지 않는다.
BOOTSTRAP = '''
import socket, sys
sys.path.insert(0, sys.argv[1])
allowed = {int(sys.argv[2]), int(sys.argv[3])}
original = socket.socket.connect
original_connect_ex = socket.socket.connect_ex
original_getaddrinfo = socket.getaddrinfo
original_bind = socket.socket.bind
def bind(self, address):
    result = original_bind(self, address)
    if isinstance(address, tuple) and address[0] == '127.0.0.1':
        allowed.add(self.getsockname()[1])
    return result
socket.socket.bind = bind
def connect(self, address):
    if not isinstance(address, tuple) or address[0] != '127.0.0.1' or address[1] not in allowed:
        raise OSError('capture: external connection blocked')
    return original(self, address)
socket.socket.connect = connect
def connect_ex(self, address):
    if not isinstance(address, tuple) or address[0] != '127.0.0.1' or address[1] not in allowed:
        raise OSError('capture: external connection blocked')
    return original_connect_ex(self, address)
socket.socket.connect_ex = connect_ex
def getaddrinfo(host, port, *args, **kwargs):
    if host not in ('127.0.0.1', b'127.0.0.1') or int(port) not in allowed:
        raise OSError('capture: external connection blocked')
    return original_getaddrinfo(host, port, *args, **kwargs)
socket.getaddrinfo = getaddrinfo
import db
db.init()
import app, uvicorn
uvicorn.run(app.app, host='127.0.0.1', port=int(sys.argv[3]), lifespan='off')
'''

