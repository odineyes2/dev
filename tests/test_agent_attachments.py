"""에이전트 키로 첨부를 올리고 댓글에 붙이는지(DEV-242) — '첨부는 사람만' 폐기 확인."""
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix='dev-agent-attach-'))
os.environ['DEV_DATA_DIR'] = str(TMP / 'data')
os.environ['DEV_DB_PATH'] = str(TMP / 'data' / 'dev.db')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import auth  # noqa: E402
import issues  # noqa: E402
import jobs  # noqa: E402
import mcp_tools  # noqa: E402
import orchestrate  # noqa: E402
import project_init  # noqa: E402
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


def check(cond, label):
    if not cond:
        raise SystemExit(f'실패: {label}')
    print(f'통과: {label}')


with TestClient(app_module.app) as client:
    _, key1 = auth.create_agent('agent-one')
    _, key2 = auth.create_agent('agent-two')
    A1 = {'headers': {'Authorization': f'Bearer {key1}'}}
    A2 = {'headers': {'Authorization': f'Bearer {key2}'}}
    project = client.post('/api/projects', json={'key': 'AT', 'name': '첨부 시험'}, **HUMAN)
    check(project.status_code in (200, 201), f'프로젝트 만들기 {project.status_code} {project.text[:200]}')
    ref = client.post('/api/issues', json={'project': 'AT', 'title': '첨부 대상'}, **A1).json()['ref']

    up = client.post('/api/attachments', files={'file': ('spec.md', '# 목업 명세\n노드 28px'.encode(), 'text/markdown')}, **A1)
    check(up.status_code == 200, f'에이전트 업로드 허용 {up.status_code} {up.text[:200]}')
    aid = up.json()['id']

    other = client.post(f'/api/issues/{ref}/comments', json={'body': '남의 첨부', 'attachment_ids': [aid]}, **A2)
    check(other.status_code == 403, f'남의 첨부 연결 거절 {other.status_code}')

    bad = client.post(f'/api/issues/{ref}/comments', json={'body': '형식', 'attachment_ids': aid}, **A1)
    check(bad.status_code == 400, f'목록이 아닌 첨부 ID 거절 {bad.status_code}')

    ok = client.post(f'/api/issues/{ref}/comments', json={'body': '명세를 붙여요', 'attachment_ids': [aid]}, **A1)
    check(ok.status_code == 200 and ok.json()['data'].get('attachment_ids') == [aid], '첨부 붙인 댓글')

    it = client.get(f'/api/issues/{ref}', **HUMAN).json()
    check([a['id'] for a in it['attachments']] == [aid], '이슈 첨부 목록에 연결됨')
    content = client.get(f'/api/attachments/{aid}/content', **A2)
    check(content.status_code == 200 and '노드 28px' in content.text, '다른 에이전트도 읽기 가능')

    plain = client.post(f'/api/issues/{ref}/comments', json={'body': '첨부 없는 댓글'}, **A1)
    check(plain.status_code == 200 and 'attachment_ids' not in plain.json()['data'], '첨부 없는 댓글은 그대로')

    up2 = client.post('/api/attachments', files={'file': ('x.md', b'temp', 'text/markdown')}, **A1).json()['id']
    check(client.delete(f'/api/attachments/{up2}', **A2).status_code == 403, '남의 첨부 삭제 거절')
    check(client.delete(f'/api/attachments/{up2}', **A1).status_code == 204, '자기 첨부 삭제 허용')

    check(client.post('/api/attachments', files={'file': ('a.md', b'x', 'text/markdown')}).status_code in (401, 403), '키 없는 업로드 거절')

    tools = {t.name for t in __import__('asyncio').run(mcp_tools.mcp.list_tools())} if hasattr(mcp_tools.mcp, 'list_tools') else set()
    check(not tools or {'upload_attachment', 'add_comment'} <= tools, f'MCP 도구 등록 {sorted(tools)[:3]}…')
print('모두 통과')
