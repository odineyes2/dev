"""재시작 안내 상태의 단계·대기 목록·조회 권한을 임시 DB와 실제 API로 확인한다."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import db
import config
import issues
import jobs
import orchestrate
import reproduce
from fastapi.testclient import TestClient
import app

H = {'kind': 'human', 'name': 'admin'}


class RestartStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stopall)
        patch.object(config, 'DB_PATH', Path(self.tmp.name) / 'test.db').start()
        db.init()
        issues.create_project(H, 'DEV', 'dev')
        issues.create_project(H, 'NS', 'nightshift')
        self.merge = issues.create_issue(H, 'DEV', '병합')
        self.other = issues.create_issue(H, 'NS', '다른 실행')
        self.since = (datetime.now(timezone.utc) - timedelta(minutes=12, seconds=1)).isoformat()
        with db.connect() as c:
            self.run = c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at) VALUES(?,'execute','running','human:admin',?)",
                                 (self.merge['id'], self.since)).lastrowid
        self.client = TestClient(app.app)
        self.addCleanup(self.client.close)

    def record(self, phase, target=None):
        with db.connect() as c:
            c.execute("INSERT OR REPLACE INTO execution_completion(run_id,issue_id,ref,actor,task_sha,auto_merge,phase,owner_event,cfg_json,updated_at) "
                      "VALUES(?,?,?,'human:admin','abc',1,?,0,?,?)",
                      (self.run, self.merge['id'], self.merge['ref'], phase,
                       json.dumps({'pm2_app': target or jobs.SELF_PM2_APP}), self.since))

    def state(self):
        with db.connect() as c:
            before = c.total_changes
            result = jobs.self_restart_state(c)
            self.assertEqual(c.total_changes, before)
            return result

    def test_phases_self_exclusion_and_no_external_calls(self):
        self.assertIsNone(self.state())
        with patch.object(orchestrate, '_busy', side_effect=AssertionError('조회는 외부 요청·고아 정리를 하지 않는다')):
            for phase in ('ready', 'checking', 'building', 'applying'):
                self.record(phase)
                state = self.state()
                self.assertEqual(state['phase'], 'merging')
                self.assertEqual(state['busy'], [])
                self.assertEqual(state['ref'], self.merge['ref'])
                self.assertEqual(state['process_id'], orchestrate.PROCESS_ID)
                self.assertEqual(state['revision'], orchestrate.BOOT_SHA)
            for phase in ('deployed', 'restart_requested', 'rollback_requested', 'rollback_applied'):
                self.record(phase)
                self.assertEqual(self.state()['phase'], 'restarting')
            for phase in ('complete', 'held'):
                self.record(phase)
                self.assertIsNone(self.state())
            self.record('deployed', 'nightshift')
            self.assertIsNone(self.state())

    def test_busy_waiting_order_and_elapsed_minutes(self):
        self.record('deployed')
        with db.connect() as c:
            c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at,provider) VALUES(?,'review','running','human:admin',?,'codex')",
                      (self.other['id'], self.since))
            for issue in (self.merge, self.other):
                c.execute("INSERT INTO jobs(issue_id,mode,actor,status,created_at) VALUES(?,'execute','human:admin','queued',?)", (issue['id'], self.since))
            c.execute('UPDATE jobs SET sort_key=-1 WHERE issue_id=?', (self.other['id'],))
        state = self.state()
        self.assertEqual(state['phase'], 'waiting')
        self.assertEqual(state['waiting'], [self.other['ref'], self.merge['ref']])
        self.assertEqual(state['busy'], [{'ref': self.other['ref'], 'mode': 'review', 'provider': 'codex', 'minutes': 12}])

    def test_auxiliary_work_and_old_reproduction_process(self):
        self.record('deployed')
        with db.connect() as c:
            c.execute("INSERT INTO app_settings(key,value,updated_at,updated_by) VALUES(?,?,?,'human:admin')",
                      (reproduce.PREFIX + self.other['ref'], json.dumps({'status': 'running', 'process': 'old', 'started_at': self.since}), self.since))
        self.assertEqual(self.state()['phase'], 'restarting')
        with db.connect() as c:
            c.execute("UPDATE app_settings SET value=?", (json.dumps({'status': 'running', 'process': reproduce.PROCESS, 'started_at': self.since}),))
        self.assertEqual(self.state()['busy'][0]['mode'], 'reproduce')
        self.assertEqual(self.state()['phase'], 'waiting')
        with db.connect() as c:
            c.execute('DELETE FROM app_settings')
            c.execute("INSERT INTO project_initializations(project_id,request_id,owner,provider,spec,status,created_at,updated_at) "
                      "VALUES(?,'init','human:admin','claude','','running',?,?)", (self.other['project_id'], self.since, self.since))
        self.assertEqual(self.state()['busy'][0]['mode'], 'initialization')
        with db.connect() as c:
            c.execute("INSERT INTO project_audits(project_id,request_id,input_fingerprint,provider,actor,status,created_at,updated_at) "
                      "VALUES(?,'audit','','codex','human:admin','preparing',?,?)", (self.merge['project_id'], self.since, self.since))
        self.assertEqual({r['mode'] for r in self.state()['busy']}, {'audit', 'initialization'})

    def test_authenticated_jobs_response_and_anonymous_denial(self):
        self.record('checking')
        with patch.object(app, '_resolve_actor', AsyncMock(return_value=(None, 'anon'))):
            self.assertEqual(self.client.get('/api/jobs').status_code, 401)
        with patch.object(app, '_resolve_actor', AsyncMock(return_value=(H, ''))):
            response = self.client.get('/api/jobs')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['self_restart']['phase'], 'merging')
            self.assertIn('jobs', response.json())
        with patch.object(app, '_resolve_actor', AsyncMock(return_value=({'kind': 'agent', 'id': 1}, ''))):
            self.assertEqual(self.client.get('/api/jobs').status_code, 200)


if __name__ == '__main__':
    unittest.main()
