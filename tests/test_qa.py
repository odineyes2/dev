"""QA ??? ????? ????? ????? HTTP ??? ????."""
import os, sys, tempfile, sqlite3, json
from pathlib import Path
from unittest.mock import patch
os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))
import db, issues, auth, app, auto_settings, usage
from fastapi.testclient import TestClient

# QA ?? DB? ??? ?? ?? ???????? ????? ????.
with sqlite3.connect(db.config.DB_PATH) as c:
    for version, sql in enumerate(db.MIGRATIONS[:-1], 1):
        c.executescript(sql + f'PRAGMA user_version={version};')
    now = db.now_iso()
    c.execute("INSERT INTO projects(key,name,created_at) VALUES('QA','QA',?)", (now,))
    c.execute("INSERT INTO issues(project_id,number,title,reporter,created_at,updated_at) VALUES(1,1,'QA','human:admin',?,?)", (now,now))
    rid = c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at,input_tokens,cost_usd) VALUES(1,'review','ok','human:admin',?,12,0.5)", (now,)).lastrowid
    c.execute("INSERT INTO plans(issue_id,version,body,author,created_at) VALUES(1,1,'plan','human:admin',?)", (now,))
    c.execute('INSERT INTO review_plan_runs VALUES(1,?)', (rid,))
    before = c.execute('SELECT * FROM runs').fetchall()
    triggers = c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='runs' ORDER BY name").fetchall()
assert db.init() == len(db.MIGRATIONS)
assert db.init() == len(db.MIGRATIONS)
with db.connect() as c:
    assert [tuple(r) for r in c.execute('SELECT * FROM runs')] == before
    assert [tuple(r) for r in c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='runs' ORDER BY name")] == triggers
    assert c.execute('SELECT run_id FROM review_plan_runs').fetchone()[0] == rid
    assert not c.execute('PRAGMA foreign_key_check').fetchall()
    qrun = c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at,input_tokens,cost_usd) VALUES(1,'qa','ok','human:admin',?,7,0.2)", (now,)).lastrowid
    qid = c.execute("INSERT INTO qa_rounds(issue_id,round_number,snapshot_key,snapshot_json,status,run_id,created_at) VALUES(1,1,'key','{}','passed',?,?)", (qrun,now)).lastrowid
    c.execute("INSERT INTO qa_items VALUES(?,'r','pass','[{}]','[]','')", (qid,))
    c.execute("INSERT INTO qa_rework_batches(issue_id,qa_id,attempt,status,task_refs_json,created_at) VALUES(1,?,1,'registered','[]',?)", (qid,now))
assert usage.project_usage('QA')['summary']['input_tokens'] == 19
human = {'kind':'human','name':'admin'}
task = issues.create_issue(human,'QA','Task',parent='QA-1')
for sql, args in [
    ("INSERT INTO qa_rounds(issue_id,round_number,snapshot_key,snapshot_json,created_at) VALUES(1,2,'key','{}',?)",(now,)),
    ("INSERT INTO qa_rounds(issue_id,round_number,snapshot_key,snapshot_json,created_at) VALUES(?,1,'task','{}',?)",(task['id'],now)),
    ("INSERT INTO qa_items VALUES(?,'empty','pass','[]','[]','')",(qid,)),
    ("UPDATE runs SET mode='invalid' WHERE id=?",(qrun,)),
    ("UPDATE qa_rounds SET run_id=? WHERE id=?",(rid,qid)),
    ("UPDATE qa_rework_batches SET issue_id=? WHERE qa_id=?",(task['id'],qid)),
    ("UPDATE qa_rework_batches SET attempt=6 WHERE qa_id=?",(qid,))]:
    try:
        with db.connect() as c: c.execute(sql,args)
    except sqlite3.IntegrityError: pass
    else: raise AssertionError(sql)
with patch.object(app.jobs, 'pump'), TestClient(app.app) as client:
    assert client.get('/api/health').status_code == 200
    for url in ('/api/issues/QA-1/qa','/api/settings/auto'):
        assert client.get(url).status_code == 401
    key = auth.create_agent('qa-test')[1]
    headers = {'Authorization':'Bearer '+key}
    assert client.get('/api/issues/QA-1/qa',headers=headers).status_code == 200
    assert client.get('/api/settings/auto',headers=headers).status_code == 200
    assert client.patch('/api/settings/auto',headers=headers,json={'auto_qa':True}).status_code == 403
    client.cookies.set('ns_session','temporary')
    client.headers['x-requested-with'] = 'dev'
    with patch.object(auth,'nightshift_user',return_value={'role':'admin','username':'admin'}):
        data = client.get('/api/issues/QA-1/qa').json()
        assert data['rework_count'] == 1 and data['rounds'][0]['items'][0]['verdict'] == 'pass'
        assert data['rounds'][0]['cost_usd'] == 0.2
        assert client.get('/api/issues/QA-999/qa').status_code == 404
        assert client.get('/api/issues/'+task['ref']+'/qa').status_code == 400
        settings = client.get('/api/settings/auto').json()
        assert settings['auto_qa'] is False and 'qa_provider_order' in settings['agent_orders']
        change = {'auto_qa':True,'qa_provider_order':['codex','claude']}
        result = client.patch('/api/settings/auto',json=change)
        assert result.status_code == 200 and result.json()['auto_qa'] is True, result.text
        assert client.patch('/api/settings/auto',json={'auto_review':True}).json()['auto_qa'] is True
        ids = [a['id'] for a in result.json()['agent_orders']['qa_provider_order']]
        assert client.patch('/api/settings/auto',json={'agent_orders':{'qa_provider_order':ids[::-1]}}).status_code == 200
        for invalid in ({'auto_qa':1},{'qa_provider_order':['codex']},{'agent_orders':{'qa_provider_order':[999]}},{'unknown':True}):
            assert client.patch('/api/settings/auto',json=invalid).status_code == 400
        with db.connect() as c:
            event = c.execute("SELECT after_json FROM project_auto_settings_events ORDER BY id DESC LIMIT 1").fetchone()
            assert json.loads(event[0])['auto_qa'] is True
    with patch.object(auth,'nightshift_user',return_value={'role':'member'}):
        assert client.get('/api/issues/QA-1/qa').status_code == 403
print('OK QA migration, constraints, usage and API/settings permissions')
