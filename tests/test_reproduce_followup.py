"""재현 통과 뒤 실제 시험 병합·후속 검사·운영 확인 경로를 자동으로 이어간다."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
os.environ['NTFY_TOPIC'] = ''
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import db, config, execute, issues, jobs, orchestrate, project_build, regression, reproduce, review

db.init()
me = {'kind':'human', 'name':'admin'}
repo = Path(tempfile.mkdtemp())
project_build._git(str(repo), 'init', '-q', '-b', 'main')
(repo/'base.txt').write_text('base')
project_build._git(str(repo), 'add', '.')
project_build._git(str(repo), 'commit', '-qm', 'base')
issues.create_project(me, 'AF', 'auto follow-up', local_path=str(repo))
parent = issues.create_issue(me, 'AF', 'parent')
issues.post_plan(me, parent['ref'], 'approved')
issues.decide(me, parent['ref'], 'approve', plan_version=1)
orchestrate.SETTINGS.write_text(json.dumps({'AF':{'auto_merge':True, 'pm2_app':'unit-app',
    'health_url':'http://unit.invalid/health', 'restart_when':['tests/*']}}))

for full_exit in (0, 1):
    task = issues.create_issue(me, 'AF', 'case '+str(full_exit), parent=parent['ref'])
    ref = task['ref']
    work = execute.prepare_worktree(str(repo), ref)
    (work/'tests').mkdir(exist_ok=True)
    (work/'tests/test_ui.py').write_text('''import sys
page = shots = None
def check_progress_refresh(page, shots):
    print('focused passed')
if '--progress-only' in sys.argv:
    check_progress_refresh(page, shots)
else:
    print('full regression')
    raise SystemExit(FULL_EXIT)
'''.replace('FULL_EXIT',str(full_exit)))
    project_build._git(str(work), 'add', '.')
    project_build._git(str(work), 'commit', '-qm', 'case '+str(full_exit))
    sha = project_build._git(str(work), 'rev-parse', 'HEAD')
    _, rid = review.begin(me, task, 'execute', 'codex')
    issues.link_commit(me, ref, sha)
    log = config.DATA_DIR/'merge-tests'/('test_ui-'+str(rid)+'.log')
    log.parent.mkdir(exist_ok=True)
    log.write_text('File "tests/test_ui.py", line 3, in check_progress_refresh\nTimeoutError')
    issues.set_status(me, ref, 'changes_requested', '전체 로그: '+str(log))
    with db.connect() as c:
        c.execute("UPDATE runs SET status='ok',ended_at=? WHERE id=?", (db.now_iso(),rid))
        c.execute("INSERT INTO task_execution_results(run_id,plan_version,state) VALUES(?,1,'blocked')",(rid,))
        owner = c.execute("SELECT MAX(id) FROM events WHERE issue_id=? AND kind='status'",(task['id'],)).fetchone()[0]
        c.execute("INSERT INTO execution_completion(run_id,issue_id,ref,actor,task_sha,auto_merge,phase,note,cfg_json,updated_at,owner_event) VALUES(?,?,?,?,?,1,'failed','','{}',?,?)",
                  (rid,task['id'],ref,issues.actor_label(me),sha,db.now_iso(),owner))
    regression.save_record('regression-selection:'+ref+':server_merge', {'results':{'test_ui.py':'failed'}})
    with patch.object(reproduce, 'Thread'):
        row = reproduce.start(me, ref, reproduce.panel(issues.get_issue(ref))['token'])
    before = project_build._git(str(repo), 'rev-parse','HEAD')
    busy = SimpleNamespace(acquire=lambda **kw:False)
    normal_thread = threading.Thread
    def thread(*args, **kwargs):
        return SimpleNamespace(start=lambda:None) if kwargs.get('target') == jobs.run_then_pump else normal_thread(*args,**kwargs)
    with patch('threading.Thread',side_effect=thread), patch.object(orchestrate,'_lock',busy), patch.object(review,'launch',side_effect=AssertionError('Agent 호출 금지')):
        reproduce.worker(ref,str(repo),row)
    assert issues.get_issue(ref)['status']=='in_progress'
    assert reproduce.result(ref)['followup']=='running'
    assert orchestrate.completion(ref)['phase']=='ready'
    assert project_build._git(str(repo),'rev-parse','HEAD')==before
    # 중간 검토 상태에서 완료 승인이 우회되지 않는다(부모 일괄 완료 포함).
    for candidate in (ref, parent['ref']):
        try:
            issues.set_status(me,candidate,'done',confirm_checks=True)
            raise AssertionError('미반영 결과 완료 승인 허용')
        except issues.StoreError as error:
            assert '반영 확인' in str(error)
    with patch.object(orchestrate,'_busy',return_value=False), patch.object(orchestrate,'_restart',return_value=None) as restart, \
         patch.object(orchestrate,'_verified_health',return_value=True) as health, \
         patch.object(orchestrate,'run_test',wraps=orchestrate.run_test) as tests:
        orchestrate.handle(me,ref)
    assert tests.call_count==1
    if full_exit==0:
        assert issues.get_issue(ref)['status']=='in_review'
        assert reproduce.result(ref)['followup']=='complete'
        assert orchestrate.completion(ref)['phase']=='complete'
        assert restart.called and health.called
        assert project_build._git(str(repo),'rev-parse','HEAD')!=before
    else:
        assert issues.get_issue(ref)['status']=='changes_requested'
        assert reproduce.result(ref)['followup']=='failed'
        assert not restart.called and not health.called
        assert project_build._git(str(repo),'rev-parse','HEAD')==before
print('Automatic follow-up: queued while merging, full regression, merge, restart/health, failure isolation, review and approval gates OK')
