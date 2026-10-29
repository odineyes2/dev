"""수신 전용 인증·멱등·미연결 저장과 마이그레이션을 임시 DB에서 검사한다."""
import os
import sys
import tempfile
import sqlite3
from pathlib import Path

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))

from fastapi.testclient import TestClient
import app as A
import auth
import config
import db
import issues
import manual_usage as M

# 직전 스키마에 저장된 설정을 보존하면서 새 판을 두 번 적용한다.
config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(config.DB_PATH) as c:
    for version, sql in enumerate(db.MIGRATIONS[:-1], 1):
        c.executescript(sql + f'PRAGMA user_version={version};')
    c.execute("INSERT INTO app_settings(key,value,updated_at,updated_by) VALUES('preserve','yes',?,'test')", (db.now_iso(),))
assert db.init() == len(db.MIGRATIONS)
assert db.init() == len(db.MIGRATIONS)
with db.connect() as c:
    assert c.execute("SELECT value FROM app_settings WHERE key='preserve'").fetchone()[0] == 'yes'
human = {'kind': 'human', 'name': 'admin', 'id': 1}
issues.create_project(human, 'DEV', 'dev')
issue = issues.create_issue(human, 'DEV', 'usage')
agent, agent_key = auth.create_agent('usage test')
M.rotate_key()
key = (config.DATA_DIR / 'manual_usage.key').read_text()
headers = {'Authorization': 'Bearer ' + key}
url = '/api/manual-usage/ingest'
item = dict(message_id='message-1', model='claude-opus', input_tokens=12,
            output_tokens=3, cache_creation_tokens=4, cache_read_tokens=5,
            ts='2026-10-30T00:00:00Z', ref=issue['ref'], content='discard this')

with TestClient(A.app) as client:
    def send(items=None, session='session-1', **kwargs):
        return client.post(url, json={'session_id': session, 'items': items or [item]}, **kwargs)

    assert send().status_code == 401
    client.cookies.set('ns_session', 'not-a-receiving-key')
    assert send().status_code == 401
    client.cookies.clear()
    assert send(headers={'Authorization': 'Bearer mu_invalid'}).status_code == 401
    assert send(headers={'Authorization': 'Bearer ' + agent_key}).status_code == 401
    assert send(headers=headers).json() == {'inserted': 1}
    assert send(headers=headers).json() == {'inserted': 0}
    assert send(session='other-session', headers=headers).json() == {'inserted': 0}
    assert send([{**item, 'message_id': 'unlinked', 'ref': None},
                 {**item, 'message_id': 'unknown', 'ref': 'DEV-99999'}], headers=headers).json() == {'inserted': 2}
    assert client.get('/api/projects', headers=headers).status_code == 401
    assert client.get('/mcp/', headers=headers).status_code == 401
    assert client.get(url, headers=headers).status_code == 401
    assert client.post(url, headers=headers, content='not json').status_code == 400
    assert client.post(url, headers=headers, json=[]).status_code == 400
    for patch in ({'input_tokens': -1}, {'output_tokens': True}, {'cache_read_tokens': 2**63},
                  {'model': ''}, {'ts': 'bad'}, {'ts': '2026-10-30T00:00:00'}):
        assert send([{**item, **patch}], headers=headers).status_code == 400
    assert send([item] * 501, headers=headers).status_code == 400
    # 묶음의 뒷부분이 잘못되어도 앞부분을 저장하지 않는다.
    assert send([{**item, 'message_id': 'atomic'}, {}], headers=headers).status_code == 400
    with db.connect() as c:
        rows = {r['message_id']: dict(r) for r in c.execute('SELECT * FROM manual_usage')}
        assert len(rows) == 3
        assert rows['message-1']['issue_id'] == issue['id']
        assert rows['message-1']['cache_creation_tokens'] == 4
        assert rows['unlinked']['issue_id'] is None and rows['unlinked']['ref_text'] is None
        assert rows['unknown']['issue_id'] is None and rows['unknown']['ref_text'] == 'DEV-99999'
        assert 'content' not in rows['message-1']
        stored = c.execute('SELECT value FROM app_settings WHERE key=?', (M.KEY_SETTING,)).fetchone()[0]
        assert stored != key and len(stored) == 64
        c.execute('DELETE FROM issues WHERE id=?', (issue['id'],))
    with db.connect() as c:
        assert c.execute('SELECT issue_id FROM manual_usage WHERE message_id=?', ('message-1',)).fetchone()[0] is None
    M.rotate_key()
    assert send(headers=headers).status_code == 401
    new_headers = {'Authorization': 'Bearer ' + (config.DATA_DIR / 'manual_usage.key').read_text()}
    assert send(headers=new_headers).json() == {'inserted': 0}

print('manual usage API OK')
