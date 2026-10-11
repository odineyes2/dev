"""점검 호출은 모의 CLI와 임시 DB에서 검사하며 유료 모델을 호출하지 않는다."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))
import db
import issues
import jobs
import orchestrate
import project_audit as audit
import project_audit_runner as runner
import project_init
import review
import rollback
import usage

HUMAN = {'kind': 'human', 'name': 'admin', 'id': 1}
REPORT = {'summary': '목표 자료가 부족해 확인이 필요하다.', 'requirements': [], 'candidates': []}


class AuditRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.old_db = db.config.DB_PATH
        db.config.DB_PATH = self.root / 'audit.db'
        self.addCleanup(setattr, db.config, 'DB_PATH', self.old_db)
        db.init()
        with db.connect() as c:
            self.pid = c.execute("INSERT INTO projects(key,name,local_path,created_at) VALUES('AUD','audit',?,?)",
                                 (str(self.root), db.now_iso())).lastrowid
            self.other = c.execute("INSERT INTO projects(key,name,created_at) VALUES('OTH','other',?)", (db.now_iso(),)).lastrowid
        self.issue = issues.create_issue(HUMAN, 'AUD', '검토', '')
        self.snapshot = {'project': 'AUD', 'base_branch': 'main', 'base_sha': 'a' * 40,
                         'repo_path': str(self.root), 'history_fingerprint': 'h', 'sources': [],
                         'has_goal_material': False}
        self.addCleanup(runner._workers.clear)
        self.addCleanup(runner._processes.clear)
        self.pump = patch.object(jobs, 'pump')
        self.pump.start()
        self.addCleanup(self.pump.stop)

    def row(self, provider='claude', request='one', timeout=3):
        return audit.create_audit(self.pid, request, provider, 'human:admin', model='gpt-6.1-sol' if provider == 'codex' else 'claude-opus-5-5',
                limits={'timeout_sec': timeout, 'budget_usd': 2 if provider == 'claude' else None,
                        'input_bytes': audit.MAX_INPUT_BYTES, 'output_bytes': audit.MAX_OUTPUT_BYTES})

    def get(self, row):
        return audit.get_audit(self.pid, row['id'])

    def denied(self, call, status=409):
        with self.assertRaises(issues.StoreError) as error:
            call()
        self.assertEqual(error.exception.status, status)

    def run_row(self, row, output, *, code=0):
        # 실제 Popen·입출력·종료 처리는 짧은 모의 CLI에 연결한다.
        script = 'import sys; sys.stdin.read(); print(' + repr(output) + '); sys.exit(' + str(code) + ')'
        with patch.object(audit, 'collect_snapshot', return_value=self.snapshot), patch.object(
                runner, 'command', return_value=[sys.executable, '-c', script]):
            runner._run({'key': 'AUD'}, row)
        return self.get(row)

    def test_claude_and_codex_model_usage_and_no_paid_call(self):
        claude = {'structured_output': REPORT, 'model': 'claude-opus-5-5',
                  'usage': {'input_tokens': 12, 'cache_read_input_tokens': 3, 'output_tokens': 5}, 'total_cost_usd': .02}
        result = self.run_row(self.row(), json.dumps(claude))
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['phase'], '점검 완료')
        self.assertEqual(result['actual_model'], 'claude-opus-5-5')
        self.assertEqual(result['usage']['input_tokens'], 15)
        self.assertEqual(result['usage']['cost_usd'], .02)
        codex = '\n'.join(json.dumps(e) for e in [
            {'type': 'thread.started', 'model': 'gpt-6.1-sol'},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(REPORT)}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 9, 'output_tokens': 4}}])
        result = self.run_row(self.row('codex', 'two'), codex)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['actual_model'], 'gpt-6.1-sol')
        self.assertEqual(result['usage']['input_tokens'], 9)
        self.assertFalse(result['usage']['cost_measured'])
        data = usage.project_usage('AUD')
        self.assertEqual(data['audit_summary']['run_count'], 2)
        self.assertEqual(data['audit_summary']['unmeasured_cost_run_count'], 1)
        self.assertEqual(data['summary']['total_tokens'], 33)
        self.assertTrue(data['summary']['partial'])

    def test_commands_block_tools_mcp_web_and_secrets(self):
        for provider in ('claude', 'codex'):
            with self.subTest(provider=provider):
                row = self.row(provider, provider)
                cmd = runner.command(row, self.root)
                self.assertIn('--model', cmd)
                self.assertEqual(cmd[cmd.index('--model') + 1], row['model'])
                if provider == 'claude':
                    self.assertEqual(cmd[cmd.index('--tools') + 1], '')
                    self.assertIn('--restricted', cmd)
                    self.assertIn('--strict-mcp-config', cmd)
                    self.assertEqual(json.loads((self.root / 'empty-mcp.json').read_text()), {'mcpServers': {}})
                    self.assertEqual(cmd[cmd.index('--max-budget-usd') + 1], '2')
                else:
                    self.assertIn('mcp_servers={}', cmd)
                    self.assertIn('web_search="disabled"', cmd)
                    for feature in runner.DISABLED_FEATURES:
                        self.assertIn(f'features.{feature}=false', cmd)
                    self.assertIn('danger-full-access', cmd)
                audit.finish_audit(row['id'], 'failed')
        with patch.dict(os.environ, {'DEV_CODEX_AGENT_KEY': 'not-inherited', 'OPENAI_API_KEY': 'not-inherited',
                                     'GITHUB_TOKEN': 'not-inherited', 'CODEX_HOME': '/login-location'}):
            env = runner._environment()
            self.assertNotIn('DEV_CODEX_AGENT_KEY', env)
            self.assertNotIn('OPENAI_API_KEY', env)
            self.assertNotIn('GITHUB_TOKEN', env)
            self.assertEqual(env['CODEX_HOME'], '/login-location')

    def test_preflight_models_limits_auth_and_pending_jobs(self):
        jobs.enqueue(HUMAN, self.issue['ref'], 'review')
        with patch.object(review, 'TIMEOUT_SEC', 17), patch.dict(os.environ, {'DEV_EXEC_BUDGET_USD': '3.5'}):
            claude = runner.preflight(HUMAN, 'AUD', 'claude', 'claude-sonnet-5-5')
            codex = runner.preflight(HUMAN, 'AUD', 'codex', 'gpt-6.1-sol')
        self.assertEqual(claude['model'], 'claude-sonnet-5-5')
        self.assertEqual(claude['limits']['timeout_sec'], 17)
        self.assertEqual(claude['limits']['budget_usd'], 3.5)
        self.assertTrue(claude['queued_jobs'])
        self.assertIsNone(codex['limits']['budget_usd'])
        self.assertFalse(codex['limits']['cost_enforced'])
        self.denied(lambda: runner.preflight({'kind': 'agent'}, 'AUD'), 403)
        self.denied(lambda: runner.preflight(HUMAN, 'AUD', 'codex', 'unknown'), 400)
        self.denied(lambda: runner.preflight(HUMAN, 'AUD', 'codex', ['invalid']), 400)

    def test_start_race_serializes_review_and_audit(self):
        barrier = threading.Barrier(2)
        def attempt(fn):
            barrier.wait()
            try:
                fn()
                return 'started'
            except issues.StoreError as error:
                self.assertEqual(error.status, 409)
                return 'blocked'
        with patch.object(review, 'LOG_DIR', self.root / 'logs'), ThreadPoolExecutor(2) as pool:
            results = [pool.submit(attempt, fn) for fn in [lambda: self.row(), lambda: review.begin(HUMAN, self.issue, 'review')]]
            self.assertCountEqual([r.result() for r in results], ['started', 'blocked'])

    def test_concurrency_and_same_project_initialization_block(self):
        jobs.set_concurrency(HUMAN, 1)
        row = self.row()
        with db.connect() as c:
            self.assertIn('상한', jobs.start_block(c, 'OTH', 'review'))
            self.assertIn('점검', jobs.start_block(c, 'AUD', 'execute'))
        audit.finish_audit(row['id'], 'failed')
        with db.connect() as c:
            c.execute("INSERT INTO project_initializations(project_id,request_id,owner,provider,spec,status,created_at,updated_at) VALUES(?, 'init', 'human:admin','claude','spec','running',?,?)",
                      (self.pid, db.now_iso(), db.now_iso()))
        self.denied(lambda: self.row(request='init-block'))
        with db.connect() as c:
            self.assertIn('초기 생성', jobs.start_block(c, 'AUD', 'review'))

    def test_merge_rollback_and_dev_restart_boundaries(self):
        row = self.row()
        with patch.object(orchestrate, '_get') as get:
            self.assertTrue(orchestrate._busy({'pm2_app': jobs.SELF_PM2_APP}))
            get.assert_not_called()
        with db.connect() as c:
            self.denied(lambda: rollback.prepare(c, HUMAN, self.issue, [], ''), 409)
        audit.finish_audit(row['id'], 'failed')
        with patch.object(rollback, '_records', return_value=[{'project_key': 'AUD', 'phase': 'restarting'}]):
            self.denied(lambda: self.row(request='rollback-block'))
        with patch.object(jobs, '_pending_merges', return_value=[{'ref': 'AUD-1', 'phase': 'checking'}]), patch.object(
                orchestrate, 'settings', return_value={'pm2_app': jobs.SELF_PM2_APP}):
            self.denied(lambda: audit.create_audit(self.other, 'other-block', 'codex', 'human:admin'))

    def test_timeout_and_bounded_output(self):
        row = self.row(timeout=.1)
        with patch.object(audit, 'collect_snapshot', return_value=self.snapshot), patch.object(
                runner, 'command', return_value=[sys.executable, '-c', 'import time; time.sleep(5)']):
            runner._run({'key': 'AUD'}, row)
        self.assertEqual(self.get(row)['status'], 'timeout')
        result = self.run_row(self.row(request='large'), 'x' * (audit.MAX_OUTPUT_BYTES + 1))
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('x' * 20, result['error'])
        self.assertEqual(runner._processes, {})

    def test_cancel_retains_occupancy_until_worker_exits(self):
        row = self.row()
        release, entered = threading.Event(), threading.Event()
        def delayed(*args):
            entered.set()
            release.wait(3)
            return REPORT, '', {}
        runner._workers.add(row['id'])
        with patch.object(audit, 'collect_snapshot', return_value=self.snapshot), patch.object(runner, 'analyze', side_effect=delayed):
            thread = threading.Thread(target=runner._run, args=({'key': 'AUD'}, row))
            thread.start()
            self.assertTrue(entered.wait(3))
            try:
                cancelled = runner.cancel(HUMAN, 'AUD', row['id'])
                self.assertEqual(cancelled['status'], 'cancelling')
                self.denied(lambda: self.row(request='too-early'))
                self.denied(lambda: runner.cancel(HUMAN, 'OTH', row['id']), 404)
                self.denied(lambda: runner.cancel({'kind': 'agent'}, 'AUD', row['id']), 403)
            finally:
                release.set()
                thread.join(3)
        self.assertEqual(self.get(row)['status'], 'cancelled')
        self.assertIsNone(self.get(row)['report'])
        self.assertEqual(self.row(request='retry')['status'], 'preparing')

    def test_cancel_kills_actual_mock_cli_before_releasing(self):
        row = self.row()
        runner._workers.add(row['id'])
        with patch.object(audit, 'collect_snapshot', return_value=self.snapshot), patch.object(
                runner, 'command', return_value=[sys.executable, '-c', 'import time; time.sleep(10)']):
            thread = threading.Thread(target=runner._run, args=({'key': 'AUD'}, row))
            thread.start()
            deadline = time.monotonic() + 3
            while row['id'] not in runner._processes and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(.01)
            proc = runner._processes[row['id']]
            runner.cancel(HUMAN, 'AUD', row['id'])
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertIsNotNone(proc.poll())
        self.assertEqual(self.get(row)['status'], 'cancelled')

    def test_restart_recovery_no_rerun_and_late_result_rejected(self):
        row = self.row()
        audit.save_snapshot(row['id'], self.snapshot)
        with db.connect() as c:
            c.execute("UPDATE project_audits SET status='running' WHERE id=?", (row['id'],))
        with patch.object(subprocess, 'Popen') as popen:
            runner.recover()
            popen.assert_not_called()
        self.assertEqual(self.get(row)['status'], 'failed')
        self.denied(lambda: audit.finish_audit(row['id'], 'complete', report=REPORT))
        new = self.row(request='new')
        runner._run({'key': 'AUD'}, row)
        self.assertEqual(self.get(new)['status'], 'preparing')
        runner.cancel(HUMAN, 'AUD', new['id'])
        self.assertEqual(self.get(new)['status'], 'cancelled')
        another = self.row(request='another')
        with db.connect() as c:
            c.execute("UPDATE project_audits SET status='cancelling' WHERE id=?", (another['id'],))
        runner.recover()
        self.assertEqual(self.get(another)['status'], 'cancelled')

    def test_sensitive_invalid_and_failed_cli_outputs_are_not_saved(self):
        outputs = [json.dumps({'structured_output': REPORT, 'result': 'api_key=supersecretvalue'}),
                   '{invalid', json.dumps({'is_error': True, 'structured_output': REPORT})]
        for n, out in enumerate(outputs):
            result = self.run_row(self.row(request=str(n)), out)
            self.assertEqual(result['status'], 'failed')
            self.assertIsNone(result['report'])
            self.assertNotIn('supersecretvalue', result['error'])
        result = self.run_row(self.row(request='exit'), json.dumps({'structured_output': REPORT}), code=2)
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['usage']['cost_measured'])
        result = self.run_row(self.row(request='paid-failure'), json.dumps({'is_error': True,
            'usage': {'input_tokens': 7, 'output_tokens': 1}, 'total_cost_usd': .01}))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['usage']['cost_usd'], .01)
        self.assertEqual(result['usage']['input_tokens'], 7)

    def test_cli_failure_diagnostic_keeps_exit_code_and_safe_category(self):
        for n, (output, category) in enumerate([
            ('invalid_json_schema: schema rejected; api_key=supersecretvalue', 'invalid_json_schema'),
            ("You've hit your weekly limit", 'usage_limit'),
            ('rate_limit_exceeded', 'usage_limit'),
            ('unknown failure; api_key=supersecretvalue', 'CLI')
        ]):
            with self.subTest(category=category):
                result = self.run_row(self.row(provider='codex', request=f'diagnostic-{n}'), output, code=1)
                self.assertEqual(result['status'], 'failed')
                self.assertIn('종료 코드 1', result['error'])
                self.assertIn(category, result['error'])
                self.assertNotIn('supersecretvalue', result['error'])
                self.assertIsNone(result['report'])

    def test_report_validation_failure_keeps_reason_location_and_usage(self):
        self.snapshot.update(has_goal_material=True, sources=[
            {'id': 'doc', 'kind': 'document', 'content': '# Goal\nDetails', 'lines': 2},
            {'id': 'code', 'kind': 'code', 'content': 'print(1)', 'lines': 1}])
        for index, (evidence, status, expected) in enumerate([
            ([{'source_id': 'doc', 'line_start': 1, 'line_end': 999}], 'partial', 'EVIDENCE_LINES'),
            ([{'source_id': 'not-collected'}], 'partial', 'EVIDENCE_SOURCE'),
            ([{'source_id': 'doc'}, {'source_id': 'code'}], 'complete', 'COMPLETE_EVIDENCE')
        ]):
            with self.subTest(expected=expected):
                report = {'summary': 'result', 'requirements': [{'id': 'r1', 'title': 'goal', 'status': status,
                    'assessment': 'assessment', 'evidence': evidence}], 'candidates': []}
                result = self.run_row(self.row(request=f'validation-{index}'), json.dumps({
                    'structured_output': report, 'usage': {'input_tokens': 100, 'output_tokens': 20}}))
                self.assertEqual(result['status'], 'failed')
                self.assertIn(expected, result['error'])
                self.assertIn('requirements[0]', result['error'])
                self.assertNotIn('로그인', result['error'])
                self.assertEqual(result['usage']['input_tokens'], 100)
                self.assertIsNone(result['report'])

    def test_internal_error_keeps_stage_and_type_without_raw_exception(self):
        row = self.row(request='internal-error')
        with patch.object(audit, 'collect_snapshot', return_value=self.snapshot), patch.object(
                runner, 'analyze', side_effect=RuntimeError('api_key=supersecretvalue')):
            runner._run({'key': 'AUD'}, row)
        result = self.get(row)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('에이전트 분석', result['error'])
        self.assertIn('RuntimeError', result['error'])
        self.assertNotIn('supersecretvalue', result['error'])

    def test_preparing_restart_and_model_metadata_reject_late_tools(self):
        row = self.row()
        runner.recover()
        self.assertEqual(self.get(row)['status'], 'failed')
        self.assertIsNotNone(self.get(row)['ended_at'])
        with self.assertRaises(ValueError):
            runner.parse_output(json.dumps({'model': {}, 'structured_output': REPORT}), 'claude', '')
        with self.assertRaises(ValueError):
            runner.parse_output(json.dumps({'type': 'item.completed',
                'item': {'type': 'command_execution', 'command': 'unexpected'}}), 'codex', '')

    def test_git_initialization_ignores_inherited_git_configuration(self):
        with patch.dict(os.environ, {'GIT_CONFIG_COUNT': '2', 'GIT_CONFIG_KEY_0': 'credential.helper'}), patch.object(
                subprocess, 'run', return_value=type('Result', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()) as run:
            project_init._git(self.root, 'init', '-b', 'main')
        self.assertFalse(any(k.upper().startswith('GIT_') for k in run.call_args.kwargs['env']))

    def test_idempotent_start_does_not_launch_twice(self):
        with patch.object(threading.Thread, 'start') as launch:
            first = runner.start(HUMAN, 'AUD', 'same', 'codex')
            second = runner.start(HUMAN, 'AUD', 'same', 'codex')
            self.assertEqual(first['id'], second['id'])
            launch.assert_called_once()
        self.denied(lambda: runner.start(HUMAN, 'AUD', 'same', 'claude'))
        runner._workers.clear()
        runner.cancel(HUMAN, 'AUD', first['id'])


if __name__ == '__main__':
    unittest.main()
