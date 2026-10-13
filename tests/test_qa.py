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

# 임시 저장소와 가짜 provider로 실행·증거·오류를 검증한다. 운영 저장소는 변경하지 않는다.
import subprocess, time, threading
import qa, qa_runner, jobs, review, orchestrate
repo = Path(tempfile.mkdtemp())
(repo / 'tests').mkdir()
(repo / 'tests/test_local.py').write_text("print('real local failure')\nraise SystemExit(1)\n",encoding='utf-8')
(repo / 'tests/test_success.py').write_text("assert 6 * 7 == 42\nprint('real local success')\n",encoding='utf-8')
(repo / 'tests/test_access.py').write_text("open(__import__('os').environ['QA_FORBIDDEN'])\n",encoding='utf-8')
(repo / 'a.py').write_text('answer = 42\n',encoding='utf-8')
def git(*args):
    result = subprocess.run(['git','-c','user.name=qa','-c','user.email=qa@test',*args],cwd=repo,capture_output=True,text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()
git('init','-q','-b','main'); git('add','.'); git('commit','-qm','fixture')
issues.create_project(human,'QR','runner',local_path=str(repo))
auth.create_agent('independent Claude',vendor='anthropic',model='claude-opus-5-5')
def make_issue(title='QA runner', provider='codex'):
    root = issues.create_issue(human,'QR',title,body='요구를 직접 검사한다.\n실패 근거를 저장한다.',type_ids=[1])
    child = issues.create_issue(human,'QR','Task',body='**바꿀 파일**: a.py, tests/test_local.py',parent=root['ref'],type_ids=[1])
    with db.connect() as c:
        c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at,provider) VALUES(?,'execute','ok','human:admin',?,?)",(child['id'],db.now_iso(),provider))
    issues.set_status(human,root['ref'],'in_review')
    return root,child
def latest(root):
    with db.connect() as c:
        return dict(c.execute('SELECT * FROM qa_rounds WHERE issue_id=? ORDER BY id DESC LIMIT 1',(root['id'],)).fetchone())
def prepare(root):
    qa.sync()
    row = latest(root)
    with db.connect() as c:
        agent = qa.select_agent(c,json.loads(row['snapshot_json']))
        rid = c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at,provider,model) VALUES(?,'qa','running','human:system/qa',?,?,?)",(root['id'],db.now_iso(),agent['provider'],agent['model'])).lastrowid
        c.execute("UPDATE qa_rounds SET status='running',run_id=?,provider=?,model=? WHERE id=?",(rid,agent['provider'],agent['model'],row['id']))
    return latest(root)
def fake(row,payload,folder,deadline):
    assert 'DEV_CODEX_AGENT_KEY' not in qa_runner.environment(folder,login=True)
    if payload['phase'] == 'checks':
        return {'checks':['tests/test_local.py']},{'input_tokens':2,'output_tokens':3,'cost_usd':0.1}
    items = [{'requirement_id':r['id'],'verdict':'fail','evidence':[{'file':'a.py','line':1},{'check_id':'check:1'}],
              'task_refs':[payload['snapshot']['family'][1]['ref']],'suggestion':'검사 실패를 고친다.'} for r in payload['snapshot']['requirements']]
    return {'items':items},{'input_tokens':4,'output_tokens':5,'cost_usd':0.2}

root,child = make_issue()
row = prepare(root)
assert row['provider'] == 'claude'
before_status = issues.get_issue(root['ref'])['status']
with patch.object(qa_runner,'analyze',side_effect=fake):
    qa_runner.run(row)
assert latest(root)['status'] == 'failed'
assert issues.get_issue(root['ref'])['status'] == before_status == 'in_review'
result = qa.panel(root['ref'])['rounds'][0]
assert result['input_tokens'] == 6 and result['output_tokens'] == 8
assert all(item['verdict']=='fail' for item in result['items'])
assert result['checks']['check:1']['returncode'] == 1
usage_row = next(r for r in usage.project_usage('QR')['issues'] if r['ref'] == root['ref'])
assert usage_row['input_tokens'] == 6 and usage_row['output_tokens'] == 8
logs = db.config.DATA_DIR / 'qa-evidence' / str(row['id'])
assert 'real local failure' in (logs / 'check-1.log').read_text()
assert not git('worktree','list','--porcelain').count('detached')
qa.sync(); qa.sync()
assert len(qa.panel(root['ref'])['rounds']) == 1

passed,_ = make_issue('passed')
passed_row = prepare(passed)
def passing(row,payload,folder,deadline):
    result,counts = fake(row,payload,folder,deadline)
    if payload['phase'] == 'checks': result['checks'] = ['tests/test_success.py']
    else:
        for item in result['items']: item['verdict'] = 'pass'
    return result,counts
with patch.object(qa_runner,'analyze',side_effect=passing): qa_runner.run(passed_row)
assert latest(passed)['status'] == 'passed'
# Auto QA ON(위 사람 설정 위임): 통과하면 트리 전체를 위임 actor로 Done 한다.
done = issues.get_issue(passed['ref'])
assert done['status'] == 'done' and all(ch['status'] == 'done' for ch in done['children'])
assert done['events'][-1]['actor'].startswith('human:auto/delegation/')
# 앞의 실패 회차는 관련 Task 재작업 1/5로 등록되고 최상위는 그대로다.
assert issues.get_issue(child['ref'])['status'] == 'changes_requested'
assert qa.panel(root['ref'])['rework_count'] == 1

# 통과는 실제 성공한 검사와 모든 항목이 있어야 한다.
value = json.loads(row['snapshot_json'])
files = {'a.py':'answer = 42\n'}
report = fake(row,{'phase':'verdict','snapshot':value},repo,time.monotonic()+1)[0]
checks = {'check:1':{'returncode':0}}
for item in report['items']: item['verdict'] = 'pass'
assert qa.validate_report(report,value,files,checks)
for invalid in ({'items':[]},{'items':report['items']*2},'bad json'):
    try: qa.validate_report(invalid,value,files,checks)
    except ValueError: pass
    else: raise AssertionError(invalid)
for field,replacement in [('evidence',[]),('evidence',[{'file':'../outside.py','line':1}]),('evidence',[{'check_id':'fake'}]),('task_refs',['QR-999'])]:
    wrong = json.loads(json.dumps(report)); wrong['items'][0][field] = replacement
    try: qa.validate_report(wrong,value,files,checks)
    except ValueError: pass
    else: raise AssertionError(wrong)
try: qa.validate_report(report,value,files,{'check:1':{'returncode':1}})
except ValueError: pass
else: raise AssertionError('실패 검사를 통과로 인정함')

# 잘못된 결과·timeout·소스 변경은 제품 실패/재작업으로 세지 않는다.
for mode in ('invalid','timeout','modified','stale'):
    root2,_ = make_issue(mode)
    row2 = prepare(root2)
    def broken(row,payload,folder,deadline):
        if mode == 'timeout': raise TimeoutError('timeout fixture')
        if payload['phase'] == 'verdict':
            if mode == 'invalid': return {'items':[]},{}
            if mode == 'modified': (qa_runner.worktree_path(row) / 'a.py').write_text('changed\n')
            if mode == 'stale': issues.set_status(human,root2['ref'],'on_hold')
        return fake(row,payload,folder,deadline)
    with patch.object(qa_runner,'analyze',side_effect=broken): qa_runner.run(row2)
    assert latest(root2)['status'] == ('stale' if mode == 'stale' else 'inconclusive'), latest(root2)
    assert qa.panel(root2['ref'])['rework_count'] == 0
    assert issues.get_issue(root2['ref'])['status'] == ('on_hold' if mode == 'stale' else 'in_review')
    if mode == 'timeout':
        assert review.list_runs(root2['ref'])[0]['status'] == 'timeout'

# 운영 파일 접근과 임의 명령은 실제 검사 프로세스에서 거부한다.
with tempfile.TemporaryDirectory() as temp:
    scratch = Path(temp)
    forbidden = Path(tempfile.mkdtemp()) / 'production.db'
    forbidden.write_text('private fixture')
    env = qa_runner.environment(scratch)
    env['QA_FORBIDDEN'] = str(forbidden)
    code,out,err = qa_runner.execute_command([sys.executable,'-c',qa_runner.CHECK_BOOTSTRAP,str(repo),str(scratch),'tests/test_access.py'],repo,env,time.monotonic()+5)
    assert code != 0 and 'QA 운영/외부 데이터 접근 차단' in err and 'private fixture' not in out+err
    try: qa_runner.direct_checks({'checks':['../production.py']},{},repo,scratch,scratch,time.monotonic()+5)
    except ValueError: pass
    else: raise AssertionError('저장소 밖 명령 허용')
    fake_provider = scratch / 'provider.py'
    fake_provider.write_text('import json; print(json.dumps({"result":json.dumps({"checks":["tests/test_local.py"]}),"usage":{"input_tokens":2,"output_tokens":1}}))')
    with patch.object(qa_runner,'command',return_value=[sys.executable,str(fake_provider)]):
        parsed,counts = qa_runner.analyze({'provider':'claude','model':'fake'},{},scratch,time.monotonic()+5)
    assert parsed['checks'] == ['tests/test_local.py'] and counts['input_tokens'] == 2
    fake_provider.write_text('import time; time.sleep(10)')
    with patch.object(qa_runner,'command',return_value=[sys.executable,str(fake_provider)]):
        try: qa_runner.analyze({'provider':'claude','model':'fake'},{},scratch,time.monotonic()+0.1)
        except TimeoutError: pass
        else: raise AssertionError('provider timeout 미작동')

# 재시작은 과금 재실행 없이 orphaned로 종료하고 Issue 상태를 유지한다.
orphan,_ = make_issue('orphan'); prepare(orphan)
qa_runner.recover(); qa_runner.recover(); qa.sync()
assert latest(orphan)['status'] == 'orphaned'
assert issues.get_issue(orphan['ref'])['status'] == 'in_review'
assert review.list_runs(orphan['ref'])[0]['status'] == 'orphaned'

# 구현 이력 부재·혼합 provider에서는 같은 provider로 조용히 검사하지 않는다.
mixed,second = make_issue('mixed')
with db.connect() as c:
    c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at,provider) VALUES(?,'execute','ok','human:admin',?,'claude')",(mixed['id'],db.now_iso()))
    val = qa.snapshot(c,mixed['id'])
    try: qa.select_agent(c,val)
    except ValueError: pass
    else: raise AssertionError('혼합 provider 독립성 위반')
    val['implementations'] = []
    try: qa.select_agent(c,val)
    except ValueError: pass
    else: raise AssertionError('이력 없는 provider 허용')

# 펌프 중복·전체 상한: fake worker가 멈춰 있는 동안 다음 QA는 시작하지 않는다.
queued,_ = make_issue('queue'); another,_ = make_issue('queue2')
release = threading.Event(); entered = threading.Event()
def held_worker(row):
    entered.set(); release.wait(5); qa.finish(row['id'],'inconclusive','fixture')
with db.connect() as c:
    c.execute("INSERT OR REPLACE INTO app_settings VALUES('queue_concurrency','1',?,'human:admin')",(db.now_iso(),))
with patch.object(qa_runner,'run',side_effect=held_worker), patch.object(jobs,'pump'):
    qa_runner.pump()
    if not entered.wait(3):
        with db.connect() as c:
            raise AssertionError(([dict(r) for r in c.execute('SELECT id,status,note FROM qa_rounds')], jobs.start_block(c,'QR','qa'),[dict(r) for r in c.execute("SELECT id,status,mode FROM runs WHERE status='running'")]))
    qa_runner.pump()
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM runs WHERE mode='qa' AND status='running'").fetchone()[0] == 1
    assert issues.get_issue(queued['ref'])['status'] == 'in_review'
    release.set()
    for _ in range(100):
        if not any(jobs._threads.values()): break
        time.sleep(0.02)
assert not any(jobs._threads.values())
print('OK QA direct failure evidence, result validation, isolation guards, independence, timeout, stale, restart, duplicate pump and concurrency')

# base·본문·Plan·Task 결과·사람 설정 변경은 각각 기존 결과의 키를 무효화한다.
with db.connect() as c: c.execute('UPDATE global_auto_settings SET auto_qa=0')   # OFF라 통과해도 in_review로 남는다
passed,_ = make_issue('stable')
with patch.object(qa_runner,'analyze',side_effect=passing): qa_runner.run(prepare(passed))
stable = latest(passed)
with db.connect() as c:
    assert qa.valid(c,stable)
    c.execute('UPDATE issues SET body=body || ? WHERE id=?',('changed',passed['id']))
    assert not qa.valid(c,stable)
    c.rollback()
    c.execute("INSERT INTO plans(issue_id,version,body,author,created_at) VALUES(?,1,'## 확인\n- 실제 성공','human:admin',?)",(passed['id'],db.now_iso()))
    assert not qa.valid(c,stable)
    c.rollback()
    c.execute("UPDATE runs SET model='changed' WHERE issue_id IN (SELECT id FROM issues WHERE parent_id=?) AND mode='execute'",(passed['id'],))
    assert not qa.valid(c,stable)
    c.rollback()
    c.execute('UPDATE global_auto_settings SET auto_qa=1-auto_qa WHERE singleton=1')
    assert not qa.valid(c,stable)
    c.rollback()
    (repo / 'new.py').write_text('new = True\n')
    git('add','.'); git('commit','-qm','new base fixture')
    assert not qa.valid(c,stable)

# 캡처 OFF는 실행하지 않고 근거에 미실시로 남긴다.
with tempfile.TemporaryDirectory() as temp:
    folder = Path(temp)
    with patch('regression.enabled',side_effect=lambda key,*a:key != 'agent_capture'), patch.object(qa_runner,'execute_command',return_value=(0,'fixture','')) as command:
        checks = qa_runner.direct_checks({'checks':['tests/test_success.py'],'capture':True},
                    {'tests/test_success.py':'print(42)'},repo,folder,folder,time.monotonic()+5)
    assert command.call_count == 1 and checks['capture']['returncode'] is None
    assert '미실시' in checks['capture']['output']
print('OK QA pass state invariance, snapshot invalidation and disabled capture evidence')

# DEV-114-3: Auto QA 반영 — OFF 불변, goal·사람 검사 차단, 재작업 5회 후 on_hold, 새 Task 승인 전 실행 금지.
def judge(root, verdict, task_refs=True):
    row = prepare(root)
    def answer(r,payload,folder,deadline):
        result,counts = passing(r,payload,folder,deadline)
        if payload['phase'] != 'checks':
            for item in result['items']:
                item['verdict'] = verdict
                if not task_refs: item['task_refs'] = []
        return result,counts
    with patch.object(qa_runner,'analyze',side_effect=answer): qa_runner.run(row)
    return latest(root)
def statuses(root):
    issue = issues.get_issue(root['ref'])
    return [issue['status']] + [ch['status'] for ch in issue['children']]
def set_auto_qa(on):
    with db.connect() as c: c.execute('UPDATE global_auto_settings SET auto_qa=?', (int(on),))

set_auto_qa(False)
for verdict in ('pass','fail'):
    off,_ = make_issue('off '+verdict)
    assert judge(off,verdict)['status'] == ('passed' if verdict == 'pass' else 'failed')
    assert statuses(off) == ['in_review','backlog'] and qa.panel(off['ref'])['rework_count'] == 0
set_auto_qa(True)

goal,_ = make_issue('goal')
with db.connect() as c: c.execute("UPDATE issues SET labels_json='[\"goal\"]' WHERE id=?", (goal['id'],))
assert judge(goal,'pass')['status'] == 'passed' and statuses(goal) == ['in_review','backlog']
checked,_ = make_issue('human checks')
with db.connect() as c:
    c.execute("INSERT INTO plans(issue_id,version,body,author,created_at) VALUES(?,1,?,'human:admin',?)", (checked['id'],'## 사람이 할 일\n1. 실제 화면 확인',db.now_iso()))
assert judge(checked,'pass')['status'] == 'passed' and statuses(checked) == ['in_review','backlog']

newtask,_ = make_issue('new task')
assert judge(newtask,'fail',task_refs=False)['status'] == 'failed'
assert statuses(newtask) == ['on_hold','backlog'] and qa.panel(newtask['ref'])['rework_count'] == 0

limit,task = make_issue('limit')
for attempt in range(1, 6):
    row = judge(limit,'fail')
    assert row['status'] == 'failed' and statuses(limit) == ['in_review','changes_requested'], statuses(limit)
    qa.apply(row['id'])   # 중복 반영은 횟수를 늘리지 않는다
    assert qa.panel(limit['ref'])['rework_count'] == attempt
    qa.sync(); assert latest(limit)['id'] == row['id']   # 묶음이 끝나기 전엔 재검사하지 않는다
    # 재작업 실행이 병합돼 in_review가 되면 위임으로 Done 처리하고 묶음을 닫는다.
    with db.connect() as c:
        c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at,provider) VALUES(?,'execute','ok','human:admin','9999','codex')", (task['id'],))
        issues._set_status(c, human, issues._find(c, task['id']), 'in_review')
        qa.sync_rework(c)
    assert statuses(limit) == ['in_review','done']
    with db.connect() as c: c.execute("UPDATE issues SET status='backlog' WHERE id=?", (task['id'],))
assert judge(limit,'fail')['status'] == 'failed'
assert statuses(limit)[0] == 'on_hold' and qa.panel(limit['ref'])['rework_count'] == 5
print('OK Auto QA done/rework/on_hold, OFF invariance, goal·human-check·new-task gates')
