"""토큰 표시 단위 API의 인증·입력·영속성 검사."""
import os
import sys
import tempfile
from pathlib import Path

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as A  # noqa: E402
import auth  # noqa: E402


def fake_nightshift(req):
    token = req.headers.get('cookie', '').split('=', 1)[-1]
    user = {'adm': {'id': 1, 'username': 'admin', 'role': 'admin'},
            'mem': {'id': 2, 'username': 'member', 'role': 'user'}}.get(token)
    return httpx.Response(200, json={'user': user})


auth._client = httpx.AsyncClient(transport=httpx.MockTransport(fake_nightshift))
with TestClient(A.app) as client:
    url = '/api/settings/tokens'
    assert client.get(url).status_code == 401
    client.cookies.set('ns_session', 'mem')
    assert client.get(url).status_code == 403
    assert client.put(url, json={'unit': 'k'}, headers={'X-Requested-With': 'dev'}).status_code == 403
    client.cookies.set('ns_session', 'adm')
    assert client.get(url).json() == {'unit': 'auto'}
    assert client.put(url, json={'unit': 'k'}).status_code == 403
    headers = {'X-Requested-With': 'dev'}
    for invalid in ('bad', None, 1, '', 'K', [], {}):
        assert client.put(url, json={'unit': invalid}, headers=headers).status_code == 400
    assert client.put(url, json={'unit': 'M', 'extra': True}, headers=headers).status_code == 400
    for unit in ('k', 'M', 'full', 'auto'):
        assert client.put(url, json={'unit': unit}, headers=headers).json() == {'unit': unit}
        assert client.get(url).json() == {'unit': unit}
    assert client.put(url, json={'unit': 'M'}, headers=headers).status_code == 200
    agent = client.post('/api/agents', json={'name':'token reader', 'kind':'chat'},
        headers=headers).json()['key']
    client.cookies.clear()
    assert client.get(url).status_code == 401
    bearer = {'Authorization': f'Bearer {agent}', **headers}
    assert client.get(url, headers=bearer).status_code == 403
    assert client.put(url, json={'unit':'k'}, headers=bearer).status_code == 403
    client.cookies.set('ns_session', 'adm')
    assert client.get(url).json() == {'unit': 'M'}
print('token settings API OK')
