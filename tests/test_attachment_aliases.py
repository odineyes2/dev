"""이미지 번호의 이관·권한·REST/MCP·공통 프롬프트 계약을 임시 DB에서 검사한다."""
from contextlib import closing
import json
import os
import sqlite3
import struct
import sys
import tempfile
import zlib
from pathlib import Path
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory(prefix='dev-aliases-')
os.environ['DEV_DATA_DIR'] = str(Path(TMP.name) / 'data')
os.environ['DEV_DB_PATH'] = str(Path(TMP.name) / 'data' / 'dev.db')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))

from fastapi.testclient import TestClient
import app
import attachments
import auth
import db
import issues
import jobs
import mcp_tools
import orchestrate
import project_init
import project_service
import review
import rollback

rollback.start_recovery = lambda **kw: None
orchestrate.take_over = lambda: None
jobs.start_timer = lambda: None
jobs.pump = lambda: None
project_init.recover = lambda: None
project_service.recover = lambda: None

async def admin(cookie):
    return {'id': 1, 'username': 'admin', 'role': 'admin'} if cookie == 'ok' else None

auth.nightshift_user = admin
HUMAN = {'cookies': {'ns_session': 'ok'}, 'headers': {'x-requested-with': 'dev'}}

def underlying(tool):
    return getattr(tool, 'fn', tool)

with TestClient(app.app) as client:
    _, key = auth.create_agent('alias-agent')
    agent = {'headers': {'Authorization': f'Bearer {key}'}}
    assert client.post('/api/projects', json={'key': 'IM', 'name': 'aliases'}, **HUMAN).is_success

    def upload(name='same.png'):
        data = b'\x89PNG\r\n\x1a\n' if name.endswith('.png') else b'# doc'
        response = client.post('/api/attachments', files={'file': (name, data)}, **agent)
        assert response.is_success, response.text
        assert response.json()['alias'] is None
        return response.json()['id']

    def create(ids, mapping=None, **extra):
        body = dict(project='IM', title='images', attachment_ids=ids, **extra)
        if mapping is not None:
            body['attachment_image_numbers'] = mapping
        return client.post('/api/issues', json=body, **agent)

    def get(ref):
        return client.get('/api/issues/' + ref, **agent).json()

    a, b, doc = upload(), upload(), upload('doc.md')
    with db.connect() as c:
        c.execute("UPDATE attachments SET created_at='2026-01-01' WHERE id IN (?,?,?)", (a,b,doc))
    response = create([b, doc, a])
    assert response.is_success, response.text
    ref = response.json()['ref']
    full = get(ref)
    byid = {x['id']: x for x in full['attachments']}
    assert byid[b]['alias'] == '이미지1' and byid[a]['alias'] == '이미지2'
    assert byid[doc]['image_number'] is None
    assert [x['id'] for x in full['attachments'] if x['kind']=='image'] == [b,a]
    assert issues.compact_issue(full)['attachments'] == [
        {k: x[k] for k in ('id','kind','name','url','image_number','alias')} for x in full['attachments']]
    for _ in range(2):
        assert client.post(f'/api/issues/{ref}/comments', json={'body':'retry','attachment_ids':[b,a]}, **agent).is_success
    assert get(ref)['attachments'] == full['attachments']
    assert client.delete('/api/attachments/'+a, **agent).status_code == 204
    d = upload()
    assert client.post(f'/api/issues/{ref}/comments', json={'body':'add','attachment_ids':[d]}, **agent).is_success
    assert next(x for x in get(ref)['attachments'] if x['id']==d)['image_number'] == 3

    x, y = upload(), upload()
    for bad in ({x:1}, {x:1,y:1}, {x:3,y:2}, {x:True,y:2}, {x:'1',y:2},
                {x:0,y:2}, {x:-1,y:2}, {x:1,y:2147483648}, {x:1,y:2,doc:3}):
        assert create([x,y], bad).status_code == 400, bad
    explicit = create([x,y], {x:2,y:5})
    assert explicit.is_success, explicit.text
    explicit_ref = explicit.json()['ref']
    assert [v['image_number'] for v in get(explicit_ref)['attachments']] == [2,5]
    z = upload()
    client.post(f'/api/issues/{explicit_ref}/comments', json={'body':'add','attachment_ids':[z]}, **agent)
    assert next(v for v in get(explicit_ref)['attachments'] if v['id']==z)['image_number']==6
    assert create([x]).status_code == 403
    _, otherkey = auth.create_agent('other')
    other = {'headers': {'Authorization':f'Bearer {otherkey}'}}
    temp = upload()
    assert client.post(f'/api/issues/{ref}/comments', json={'body':'bad','attachment_ids':[temp]}, **other).status_code==403
    assert client.delete('/api/attachments/'+temp, **other).status_code==403
    with db.connect() as c:
        c.execute("UPDATE attachments SET expires_at='2000-01-01' WHERE id=?", (temp,))
    assert create([temp]).status_code==409

    child = create([], parent=ref).json()['ref']
    with patch.object(review.filemap, 'section', return_value=''):
        for execution in (False, True):
            text = review.preamble(child, execution=execution)
            assert b in text and '이미지1' in text and child in text and ref in text
    with patch.object(mcp_tools, '_actor', return_value={'kind':'agent','id':1}):
        result = underlying(mcp_tools.read_attachment)(ref,b)
        info = json.loads(result[0].text)
        assert info['id']==b and info['alias']=='이미지1'
        compact = underlying(mcp_tools.get_issue)(ref, compact=True)
        assert next(v for v in compact['attachments'] if v['id']==b)['alias']=='이미지1'
        m = upload()
        mcp_issue = underlying(mcp_tools.create_issue)('IM','MCP', attachment_ids=[m], attachment_image_numbers={m:4})
        assert mcp_issue['ref']
        assert get(mcp_issue['ref'])['attachments'][0]['alias']=='이미지4'

    # DB 제약은 앱 검증을 우회하는 SQL에도 적용한다.
    for sql, args in [
        ('UPDATE attachments SET image_number=? WHERE id=?', (1,doc)),
        ('UPDATE attachments SET image_number=? WHERE id=?', (1.5,b)),
        ('UPDATE attachments SET image_number=? WHERE id=?', (0,b)),
        ('UPDATE attachments SET image_number=? WHERE id=?', (1,d)),
        ('UPDATE issues SET next_image_number=0 WHERE id=?', (full['id'],)),
        ('UPDATE issues SET next_image_number=1.5 WHERE id=?', (full['id'],)),
    ]:
        try:
            with db.connect() as c:
                c.execute(sql,args)
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError(sql)
    with db.connect() as c:
        c.execute('UPDATE issues SET next_image_number=2147483648 WHERE id=?', (full['id'],))
    assert client.post(f'/api/issues/{ref}/comments', json={'body':'full','attachment_ids':[upload()]}, **agent).status_code==400
    with db.connect() as c:
        assert c.execute('SELECT next_image_number FROM issues WHERE id=?', (full['id'],)).fetchone()[0]==2147483648

    # 캡처의 직접 연결도 카운터를 쓰고 같은 manifest 재수집은 번호를 소비하지 않는다.
    capture_ref = create([]).json()['ref']
    capture_issue = get(capture_ref)
    with db.connect() as c:
        run_id = c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at) VALUES(?,'execute','ok','agent:1','now')", (capture_issue['id'],)).lastrowid
    cwd = Path(TMP.name) / 'worktree'
    directory = cwd / '.ui-captures' / 'run'
    directory.mkdir(parents=True)
    def chunk(kind, data):
        return struct.pack('>I',len(data)) + kind + data + struct.pack('>I',zlib.crc32(kind+data))
    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,2,0,0,0)) + chunk(b'IDAT',zlib.compress(b'\0\xff\0\0')) + chunk(b'IEND',b'')
    (directory/'shot.png').write_bytes(png)
    (directory/'manifest.json').write_text(json.dumps({'version':1,'captures':[{'path':'.ui-captures/run/shot.png'}]}), encoding='utf-8')
    assert attachments.collect_captures(capture_ref,run_id,cwd,set())==(1,[])
    assert attachments.collect_captures(capture_ref,run_id,cwd,set())==(0,[])
    assert get(capture_ref)['attachments'][0]['alias']=='이미지1'
    with db.connect() as c:
        assert c.execute('SELECT next_image_number FROM issues WHERE id=?', (capture_issue['id'],)).fetchone()[0]==2

    # 개수·합계 용량 거절은 연결과 번호 변경을 모두 롤백한다.
    pending = upload()
    for setting,value in [('ATTACHMENT_MAX_COUNT',1),('ATTACHMENT_TOTAL_BYTES',len(png))]:
        with patch.object(attachments.config,setting,value):
            assert client.post(f'/api/issues/{capture_ref}/comments', json={'body':'limit','attachment_ids':[pending]}, **agent).status_code==413
    with db.connect() as c:
        assert c.execute('SELECT image_number,issue_id FROM attachments WHERE id=?',(pending,)).fetchone()[:] == (None,None)

# 이전 스키마 자체에서 이관해 기존 행과 제약을 보존하는지 확인한다.
old = Path(TMP.name) / 'legacy.db'
with patch.object(db.config, 'DB_PATH', old):
    with closing(sqlite3.connect(old)) as c, c:
        for i, sql in enumerate(db.MIGRATIONS[:-1], 1):
            c.executescript(sql + f'PRAGMA user_version={i};')
        c.execute("INSERT INTO projects(key,name,created_at) VALUES('OLD','old','now')")
        c.execute("INSERT INTO issues(project_id,number,title,body,status,priority,labels_json,reporter,created_at,updated_at) VALUES(1,1,'old',?,'backlog','none','[]','human:admin','now','now')", ('본문 이미지99\n\n---\n첨부 이미지:\n[이미지 7] = unique.png\n[이미지 3] = same.png',))
        for aid,name,kind in [('a','same.png','image'),('b','unique.png','image'),('c','same.png','image'),('d','doc.md','markdown')]:
            c.execute('INSERT INTO attachments VALUES(?,1,?,?,?,?,?,?,NULL,?,NULL)', (aid,'human:admin',kind,name,'image/png',1,aid*32,'now'))
        for number,body in [(2,'본문 [이미지 99] = lone.png'),(3,'---\n첨부 이미지:\n[이미지 4] = first.png\n[이미지 4] = second.png')]:
            iid = c.execute("INSERT INTO issues(project_id,number,title,body,status,priority,labels_json,reporter,created_at,updated_at) VALUES(1,?,'old',?,'backlog','none','[]','human:admin','now','now')", (number,body)).lastrowid
            for suffix,name in [('e','first.png'),('f','second.png')]:
                aid = str(number)+suffix
                c.execute('INSERT INTO attachments VALUES(?,?,?,?,?,?,?,?,NULL,?,NULL)',(aid,iid,'human:admin','image',name,'image/png',1,aid*16,'now'))
    db.init()
    with db.connect() as c:
        rows = c.execute('SELECT id,image_number FROM attachments WHERE issue_id=1 ORDER BY id').fetchall()
        assert [tuple(r) for r in rows] == [('a',1),('b',7),('c',2),('d',None)]
        assert [r[0] for r in c.execute('SELECT next_image_number FROM issues ORDER BY id')]==[8,3,3]
        assert [r[0] for r in c.execute('SELECT image_number FROM attachments WHERE issue_id IN (2,3) ORDER BY id')]==[1,2,1,2]
        assert c.execute('SELECT body FROM issues').fetchone()[0].startswith('본문 이미지99')
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]
print(f'attachment aliases: all checks passed (migration {db.IMAGE_NUMBER_MIGRATION})')
