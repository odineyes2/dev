"""서비스 해제의 소유권·토큰·정지 순서·중단·재연결·DB 제약을 격리하여 검사한다."""
import copy
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch

TEMP = tempfile.TemporaryDirectory(prefix='dev-disconnect-')
ROOT = Path(TEMP.name)
os.environ['DEV_DATA_DIR'] = str(ROOT / 'data')
os.environ['DEV_DB_PATH'] = str(ROOT / 'data/dev.db')
os.environ['PM2_HOME'] = str(ROOT / 'pm2')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import db
import issues
import pm2_apps as apps
import project_service as service

db.init()
HUMAN = {'kind': 'human', 'name': 'admin'}


class DisconnectTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.repo = ROOT / 'repo'
        self.repo.mkdir(exist_ok=True)
        self.eco = self.repo / 'ecosystem.config.js'
        self.eco.write_text('module.exports={apps:[]}', encoding='utf-8')
        self.tunnel = ROOT / 'tunnel.yml'
        self.tunnel.write_bytes(b'original')
        with db.connect() as c:
            c.execute('DELETE FROM project_app_restarts')
            c.execute('DELETE FROM project_services')
            c.execute('DELETE FROM projects')
            self.pid = c.execute("INSERT INTO projects(key,name,local_path,service_app,service_domain,service_port,created_at) VALUES('STOP','stop',?,'web','test.example.com',8401,?)", (str(self.repo), db.now_iso())).lastrowid
            self.project = dict(c.execute('SELECT * FROM projects WHERE id=?', (self.pid,)).fetchone())
            c.execute("INSERT INTO project_services(project_id,status,action,domain,port,actor,updated_at) VALUES(?,'connected','connect','test.example.com',8401,'human:admin',?)", (self.pid, db.now_iso()))
        self.declared = [dict(name=n, cwd=apps.path_key(self.repo), exec=apps.path_key(self.repo / (n + '.py'))) for n in ('web', 'worker', 'idle')]
        self.current = [dict(r, id=i, status='online' if i < 2 else 'stopped') for i, r in enumerate(self.declared)]
        self.events = []
        self.fail = None
        self.saved_error = ''
        self.stack.enter_context(patch.object(apps, 'declarations', side_effect=lambda p: (apps.path_key(self.repo), copy.deepcopy(self.declared))))
        self.stack.enter_context(patch.object(apps, 'processes', side_effect=lambda: copy.deepcopy(self.current)))
        self.stack.enter_context(patch.object(apps, 'command', side_effect=self.command))
        for name, value in dict(config_path=lambda: self.tunnel, binary=lambda: 'fake',
                                _restart_permission=lambda: None, _candidate=lambda *a: (b'new', 't1'),
                                _healthy=lambda url: self.events.append('external' if url.startswith('https') else 'local'),
                                _switch=self.switch, _command=self.service_command,
                                pm2_launcher=lambda: ['fake-pm2'], app_environment=lambda *a: {},
                                save_pm2=self.save, _restore=lambda row: self.events.append('restore') or '').items():
            self.stack.enter_context(patch.object(service, name, value))

    def command(self, args, *a):
        self.events.append(tuple(args))
        if self.fail == args[-1] or self.fail == args[0]:
            raise apps.AppError('종료 코드 오류')
        if args[0] == 'stop':
            for r in self.current:
                if r['id'] == int(args[1]) and self.fail != 'confirm':
                    r['status'] = 'stopped'
        else:
            name = args[args.index('--only') + 1]
            rows = [r for r in self.current if r['name'] == name]
            if not rows:
                self.current.append(dict(next(r for r in self.declared if r['name'] == name), id=9, status='online'))
            for r in rows:
                r['status'] = 'online'

    def service_command(self, args, *a, **kw):
        if 'startOrRestart' in args:
            self.events.append('main')
            for r in self.current:
                if r['name'] == 'web':
                    r['status'] = 'online'
        else:
            self.events.append('validate')

    def switch(self, *args):
        self.events.append('switch')
        if self.fail == 'switch':
            raise issues.StoreError('switch failure', 409)

    def save(self, *args):
        self.events.append('save')
        return self.saved_error

    def row(self):
        with db.connect() as c:
            return dict(c.execute('SELECT * FROM project_services WHERE project_id=?', (self.pid,)).fetchone())

    def run_disconnect(self, checked=True):
        snap = apps.snapshot(self.project) if checked else None
        service._save(self.pid, status='running', action='disconnect', stop_apps=int(checked))
        service._run(self.project, 'disconnect', snap)
        return self.row()

    def test_preview_cluster_and_protection(self):
        self.current.append(dict(self.current[1], id=10))
        self.declared += [dict(name='dev', cwd=apps.path_key(self.repo), exec='dev.py'),
                          dict(name='escape', cwd=apps.path_key(ROOT / 'outside'), exec='out.py')]
        self.current += [dict(r, id=i+20, status='online') for i, r in enumerate(self.declared[3:])]
        preview = apps.preview(self.project)
        self.assertTrue(preview['available'])
        self.assertTrue(preview['stop_default'])
        self.assertEqual([a['name'] for a in preview['apps']], ['web','worker','idle'])
        self.assertEqual(preview['apps'][1]['ids'], [1,10])
        self.current.append(dict(self.current[1], id=99, cwd=apps.path_key(ROOT / 'foreign')))
        self.assertNotIn('worker', [a['name'] for a in apps.preview(self.project)['apps']])
        with db.connect() as c:
            c.execute("INSERT INTO projects(key,name,service_app,created_at) VALUES('OTHER','other','web',?)", (db.now_iso(),))
        self.assertNotIn('web', [a['name'] for a in apps.preview(self.project)['apps']])
        with patch.object(apps.config, 'REPO_ROOT', self.repo):
            self.assertEqual(apps.preview(self.project)['apps'], [])

    def test_other_root_and_ambiguous_registration(self):
        with db.connect() as c:
            c.execute("INSERT INTO projects(key,name,local_path,created_at) VALUES('OTHER','other',?,?)", (str(self.repo), db.now_iso()))
        self.assertEqual(apps.preview(self.project)['apps'], [])

    def test_token_and_input_rejection(self):
        import jobs
        with patch.object(jobs, '_active_lanes', return_value=set()), patch.object(service.threading, 'Thread') as thread:
            preview_digest = apps.preview(self.project)['token']
            self.current[0]['id'] = 88
            with self.assertRaises(issues.StoreError):
                service.request(HUMAN, 'STOP', 'disconnect', True, preview_digest)
            self.assertEqual(self.row()['status'], 'connected')
            for action, value in [('disconnect', 1), ('disconnect', 'true'), ('connect', True)]:
                with self.assertRaises(issues.StoreError):
                    service.request(HUMAN, 'STOP', action, value)
            thread.assert_not_called()
            preview_digest = apps.preview(self.project)['token']
            self.eco.write_text('module.exports={apps:[]}; // changed')
            with self.assertRaises(issues.StoreError):
                service.request(HUMAN, 'STOP', 'disconnect', True, preview_digest)
            preview_digest = apps.preview(self.project)['token']
            with patch.object(service, 'status', return_value={}):
                service.request(HUMAN, 'STOP', 'disconnect', True, preview_digest)
            self.assertEqual(self.row()['stop_apps'], 1)
            self.assertEqual(thread.call_args.kwargs['args'][2]['token'], preview_digest)

    def test_order_records_and_reconnect(self):
        row = self.run_disconnect()
        self.assertEqual(row['status'], 'disconnected')
        self.assertEqual((row['domain'],row['port'],row['backup_path'],row['applied_hash']), ('',0,'',''))
        self.assertEqual(self.events, ['validate','switch',('stop','0'),('stop','1'),'save'])
        self.assertEqual({r['app_name'] for r in apps.records(self.pid)}, {'web','worker'})
        self.events.clear()
        service._run(self.project, 'connect')
        self.assertEqual(self.row()['status'], 'connected')
        self.assertEqual(self.events, ['validate','main','local','validate','switch','external',('startOrRestart','ecosystem.config.js','--only','worker','--update-env'),'save'])
        self.assertFalse(apps.records(self.pid))
        self.assertEqual(self.current[2]['status'], 'stopped')

    def test_unchecked_and_switch_failure_never_touch_pm2(self):
        with patch.object(apps, 'snapshot', side_effect=AssertionError('must not query')):
            self.assertEqual(self.run_disconnect(False)['status'], 'disconnected')
        self.assertEqual(self.events, ['validate','switch'])
        self.events.clear()
        self.fail='switch'
        self.assertEqual(self.run_disconnect()['status'], 'failed')
        self.assertNotIn('save', self.events)
        self.assertFalse(any(isinstance(e, tuple) for e in self.events))

    def test_partial_failure_save_and_stale_id(self):
        self.fail='0'
        self.saved_error='save failed'
        row=self.run_disconnect()
        self.assertEqual(row['status'], 'disconnected')
        self.assertIn('web', row['app_error'])
        self.assertIn('재부팅', row['error'])
        self.assertEqual(self.current[1]['status'], 'stopped')
        self.assertNotIn('restore', self.events)
        self.assertEqual([s['status'] for s in service._steps(self.project,row)], ['done','done','done','failed'])
        snap=apps.snapshot(self.project)
        self.current[0]['exec']=apps.path_key(ROOT/'different.py')
        self.events.clear()
        error=apps.stop(self.project,snap['candidates'])
        self.assertIn('대상 변경',error)
        self.assertNotIn(('stop','0'),self.events)

    def test_confirmation_failure_and_empty_targets(self):
        self.fail='confirm'
        row=self.run_disconnect()
        self.assertIn('정지 확인 실패',row['error'])
        self.assertTrue(all(r['state']=='requested' for r in apps.records(self.pid)))
        self.current=[]
        self.events.clear()
        self.run_disconnect()
        self.assertEqual(self.events,['validate','switch'])

    def test_retry_worker_save_failure_and_missing_registration(self):
        self.run_disconnect()
        self.current=[r for r in self.current if r['name']!='worker']
        self.fail='startOrRestart'
        service._run(self.project,'connect')
        self.assertEqual(self.row()['status'],'connected')
        self.assertIn('worker',self.row()['error'])
        self.assertEqual([r['app_name'] for r in apps.records(self.pid)],['worker'])
        self.fail=None
        self.saved_error=service.PM2_SAVE_WARNING
        service._run(self.project,'connect')
        self.assertTrue(apps.records(self.pid))
        self.saved_error=''
        service._run(self.project,'connect')
        self.assertFalse(apps.records(self.pid))

    def test_changed_worker_is_not_started(self):
        self.run_disconnect()
        self.declared[1]['exec']=apps.path_key(ROOT/'new.py')
        self.events.clear()
        service._run(self.project,'connect')
        self.assertIn('worker',self.row()['error'])
        self.assertFalse(any(isinstance(e,tuple) and e[0]=='startOrRestart' for e in self.events))

    def test_recovery_keeps_tunnel_and_reconciles_requested(self):
        apps.remember(self.project,self.current[1],'requested')
        self.current[1]['status']='stopped'
        service._save(self.pid,status='running',action='disconnect',step=6,stop_apps=1,domain='',port=0)
        class Immediate:
            def __init__(self, target, **kwargs): self.target=target
            def start(self): self.target()
        with patch.object(service.threading,'Thread',Immediate), patch.object(service.tunnel_handoff,'cleanup'):
            service.recover()
        self.assertEqual(self.row()['status'],'disconnected')
        self.assertIn('중단',self.row()['app_error'])
        self.assertEqual(apps.records(self.pid)[0]['state'],'stopped')
        self.assertEqual(self.events,[])

    def test_loader_with_secret_free_js(self):
        self.stack.close()
        self.eco.write_text("console.log('ignored'); module.exports={apps:[{name:'sample',script:'run.js',env:{PRIVATE:'do-not-return'}}]}",encoding='utf-8')
        root,decl=apps.declarations(self.project)
        self.assertEqual(decl,[dict(name='sample',cwd=apps.path_key(self.repo),exec=apps.path_key(self.repo/'run.js'))])
        self.assertNotIn('PRIVATE',json.dumps(decl))
        self.eco.write_text("throw new Error('do-not-return')",encoding='utf-8')
        result=apps.preview(self.project)
        self.assertFalse(result['available'])
        self.assertNotIn('do-not-return',result['reason'])

    def test_pm2_metadata_is_filtered_and_environment_fixed(self):
        self.stack.close()
        raw = [dict(pm_id=4,name='worker',pm2_env=dict(pm_cwd=str(self.repo),pm_exec_path=str(self.repo/'worker.py'),status='online',env={'PRIVATE':'hidden'}))]
        with patch.object(apps,'run_json',return_value=raw) as query, patch.object(service,'pm2_launcher',return_value=['fake']):
            rows=apps.processes()
        self.assertEqual(query.call_args.args[0],['fake','jlist'])
        self.assertEqual(set(rows[0]),{'id','name','cwd','exec','status'})
        for bad in [dict(pm_id=True,name='worker',pm2_env=raw[0]['pm2_env']), {}, None]:
            with patch.object(apps,'run_json',return_value=[bad]), patch.object(service,'pm2_launcher',return_value=['fake']), self.assertRaises(apps.AppError):
                apps.processes()
        import shutil
        script=ROOT/'identity.js'
        script.write_text("process.stdout.write(JSON.stringify([{home:process.env.PM2_HOME}]))")
        self.assertEqual(apps.run_json([shutil.which('node'),str(script)])[0]['home'],os.environ['PM2_HOME'])

    def test_preview_api_is_human_only_and_post_forwards_selection(self):
        import app as api
        import auth
        from fastapi.testclient import TestClient
        async def admin(cookie):
            return {'username':'admin','role':'admin'} if cookie=='ok' else None
        # context manager를 쓰지 않아 운영 복구·대기열 수명주기를 시작하지 않는다.
        client=TestClient(api.app)
        self.addCleanup(client.close)
        with patch.object(auth,'nightshift_user',admin):
            self.assertEqual(client.get('/api/projects/STOP/service/disconnect-preview').status_code,401)
            _,key=auth.create_agent('disconnect-test')
            headers = {}
            headers['Authorization'] = 'Bearer ' + key
            self.assertEqual(client.get('/api/projects/STOP/service/disconnect-preview',headers=headers).status_code,403)
            result=client.get('/api/projects/STOP/service/disconnect-preview',cookies={'ns_session':'ok'})
            self.assertEqual(result.status_code,200)
            self.assertEqual(result.headers['cache-control'],'no-store')
            self.assertEqual(len(result.json()['apps']),3)
            with patch.object(service,'request',return_value={}) as request:
                result=client.post('/api/projects/STOP/service',cookies={'ns_session':'ok'},headers={'x-requested-with':'dev'},json={'action':'disconnect','stop_apps':True,'preview_token':'snapshot'})
                self.assertEqual(result.status_code,202)
                self.assertEqual(request.call_args.args[1:],('STOP','disconnect',True,'snapshot'))

    def test_upgrade_preserves_existing_service_defaults(self):
        old_path=ROOT/'upgrade.db'
        old_path.unlink(missing_ok=True)
        with closing(sqlite3.connect(old_path)) as c, c:
            for sql in db.MIGRATIONS[:-1]:
                c.executescript(sql)
            c.execute('PRAGMA user_version='+str(len(db.MIGRATIONS)-1))
            pid=c.execute("INSERT INTO projects(key,name,created_at) VALUES('OLD','old',?)",(db.now_iso(),)).lastrowid
            c.execute("INSERT INTO project_services(project_id,status,action,actor,updated_at) VALUES(?,'connected','connect','human:admin',?)",(pid,db.now_iso()))
        with patch.object(db.config,'DB_PATH',old_path):
            self.assertEqual(db.init(),len(db.MIGRATIONS))
            with db.connect() as c:
                row=c.execute('SELECT status,stop_apps,app_error FROM project_services').fetchone()
                self.assertEqual(tuple(row),('connected',0,''))

    def test_migration_defaults_constraints_cascade(self):
        self.assertEqual(self.row()['stop_apps'],0)
        self.assertEqual(self.row()['app_error'],'')
        for column,value in [('stop_apps',2),('stop_apps',None)]:
            with self.assertRaises(sqlite3.IntegrityError),db.connect() as c:
                c.execute('UPDATE project_services SET '+column+'=? WHERE project_id=?',(value,self.pid))
        base=[self.pid,str(self.repo),'worker',str(self.repo),'run.py',1,'requested',db.now_iso()]
        for index,value in [(1,''),(2,''),(2,'x'*81),(3,''),(4,''),(5,2),(6,'bad'),(7,''),(1,None)]:
            values=base.copy();values[index]=value
            with self.assertRaises(sqlite3.IntegrityError),db.connect() as c:
                c.execute('INSERT INTO project_service_stopped_apps VALUES(?,?,?,?,?,?,?,?)',values)
        apps.remember(self.project,self.current[0],'stopped')
        with db.connect() as c:
            c.execute('DELETE FROM projects WHERE id=?',(self.pid,))
            self.assertEqual(c.execute('SELECT count(*) FROM project_service_stopped_apps').fetchone()[0],0)


if __name__=='__main__':
    try:
        unittest.main()
    finally:
        TEMP.cleanup()
