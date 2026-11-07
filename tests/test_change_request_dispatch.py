# 저장된 수정 요청의 재검토 등록·중복·권한·낡은 피드백 거절을 임시 DB로 검사한다.
import os
import sys
import tempfile
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp(prefix='dev-dispatch-')
os.environ['DEV_DB_PATH'] = str(Path(os.environ['DEV_DATA_DIR']) / 'dev.db')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import db
import issues
import jobs
import review

db.init()
jobs.kick = lambda *a: None
H = {'kind': 'human', 'name': 'admin'}
issues.create_project(H, 'CR', 'dispatch')

def sql(q, args=()):
    with db.connect() as c:
        return c.execute(q, args).lastrowid

def fixture():
    it = issues.create_issue(H, 'CR', 'feedback')
    sql('INSERT INTO plans(issue_id,version,body,author,created_at) VALUES(?,1,?,?,?)', (it['id'], 'plan', 'agent:1', db.now_iso()))
    sql("INSERT INTO decisions(issue_id,gate,plan_version,verdict,note,actor,created_at) VALUES(?,'plan',1,'approve','','human:admin',?)", (it['id'], db.now_iso()))
    sql("UPDATE issues SET status='changes_requested' WHERE id=?", (it['id'],))
    eid = sql("INSERT INTO events(issue_id,actor,kind,body,data_json,created_at) VALUES(?,'human:admin','status',?,?,?)", (it['id'], '수정해 주세요', json.dumps({'to': 'changes_requested'}), db.now_iso()))
    return it, eid

def send(it, eid, **kw):
    return jobs.enqueue(kw.pop('actor', H), it['ref'], 'review', kw.pop('provider', 'codex'), change_request_event_id=eid, plan_version=kw.pop('version', 1), **kw)

def reject(fn):
    try:
        fn()
    except issues.StoreError as e:
        assert e.status in (400, 403, 409)
    else:
        raise AssertionError('request accepted')

it, eid = fixture()
sql("INSERT INTO attachments VALUES('shared',?,'human:other','url','reference','text/uri-list',0,NULL,'https://example.invalid',?,NULL)", (it['id'], db.now_iso()))
sql('UPDATE events SET data_json=? WHERE id=?', (json.dumps({'to': 'changes_requested', 'attachment_ids': ['shared']}), eid))
assert issues.get_issue(it['ref'])['action_context']['can_dispatch_change_request']
first = send(it, eid)
assert send(it, eid)['job_id'] == first['job_id']
detail = issues.get_issue(it['ref'])
assert detail['approval']['stale']
assert detail['plan']['revision_attachments'] == ['shared']
assert '수정해 주세요' in review.revision_prompt(detail) and 'shared' in review.revision_prompt(detail)
with db.connect() as c:
    assert c.execute('SELECT count(*) FROM jobs WHERE issue_id=?', (it['id'],)).fetchone()[0] == 1
    assert c.execute("SELECT count(*) FROM events WHERE issue_id=? AND json_extract(data_json,'$.plan_revision_request')=1", (it['id'],)).fetchone()[0] == 1
reject(lambda: send(it, eid, provider='claude'))
sql("INSERT INTO events(issue_id,actor,kind,body,data_json,created_at) VALUES(?,'human:admin','status','new feedback',?,?)", (it['id'], json.dumps({'to':'changes_requested'}), db.now_iso()))
sql("UPDATE issues SET status='changes_requested' WHERE id=?", (it['id'],))
reject(lambda: send(it, eid))
for case in ('other', 'version', 'claim', 'old', 'blank', 'agent', 'mixed', 'run', 'merge', 'queued', 'attachment', 'plan', 'task'):
    x, e = fixture()
    if case == 'other': e = eid
    if case == 'claim':
        sql("UPDATE issues SET claimed_by='agent:1',lease_until=? WHERE id=?", ((datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(), x['id']))
    if case == 'old':
        sql("INSERT INTO events(issue_id,actor,kind,body,data_json,created_at) VALUES(?,'human:admin','status','','{}',?)", (x['id'], db.now_iso()))
    if case == 'blank': sql("UPDATE events SET body='  ' WHERE id=?", (e,))
    if case == 'attachment': sql('UPDATE events SET data_json=? WHERE id=?', (json.dumps({'to':'changes_requested','attachment_ids':['shared']}), e))
    if case == 'plan': sql('INSERT INTO plans(issue_id,version,body,author,created_at) VALUES(?,2,?,?,?)', (x['id'],'new','agent:1',db.now_iso()))
    if case in ('run', 'merge', 'queued', 'task'):
        child = issues.create_issue(H, 'CR', 'child', parent=x['ref'])
        if case == 'task':
            x = child
        elif case == 'queued':
            sql("INSERT INTO jobs(issue_id,mode,status,created_at,provider,actor) VALUES(?,'execute','queued',?,'codex','human:admin')", (child['id'], db.now_iso()))
        else:
            rid = sql("INSERT INTO runs(issue_id,mode,status,started_at,provider,actor) VALUES(?,'execute',?,?,'codex','human:admin')", (child['id'], 'running' if case=='run' else 'ok', db.now_iso()))
            if case == 'merge':
                sql("INSERT INTO execution_completion(run_id,issue_id,ref,actor,task_sha,auto_merge,phase,owner_event,cfg_json,updated_at) VALUES(?,?,?,'human:admin','abc',1,'applying',?,'{}',?)", (rid,child['id'],child['ref'],e,db.now_iso()))
    reject(lambda: send(x, e, **({'version':2} if case=='version' else {'actor':{'kind':'agent','id':1}} if case=='agent' else {'note':'mixed'} if case=='mixed' else {})))
print('PASS: saved feedback, attachments, stale approval, prompt, duplicate, rejection')
