"""점검 자료·근거·영속성은 임시 DB와 임시 Git만 사용해 확인한다."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))
import db
import issues
import project_audit as audit


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.previous_db = db.config.DB_PATH
        db.config.DB_PATH = self.root / 'audit.db'
        self.addCleanup(setattr, db.config, 'DB_PATH', self.previous_db)
        db.init()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'audit-test')
        self.git('config', 'user.email', 'audit@example.invalid')
        self.write('PROJECT_SPEC.md', '# 목표\n검색 결과를 표시한다.\n')
        self.write('app.py', 'def search():\n    return []\n')
        self.commit()
        with db.connect() as c:
            self.pid = c.execute('''INSERT INTO projects(key,name,local_path,description,created_at)
                VALUES('AUD','점검',?,'설명은 목표를 대체하지 않는다',?)''',
                (str(self.repo), db.now_iso())).lastrowid

    def git(self, *args, input=None):
        result = subprocess.run(['git', '-c', 'core.hooksPath=' + os.devnull,
                                 '-c', 'commit.gpgsign=false', *args], cwd=self.repo,
                                input=input, capture_output=True, check=True,
                                env={k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')})
        return result.stdout.decode('utf-8').strip()

    def write(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')

    def commit(self):
        self.git('add', '-A')
        self.git('commit', '-m', 'fixture')

    def denied(self, fn, status=400):
        with self.assertRaises(issues.StoreError) as caught:
            fn()
        self.assertEqual(caught.exception.status, status)

    def report(self):
        return {'summary': '검색은 부분 구현이다.', 'requirements': [
            {'id': 'r1', 'title': '검색', 'status': 'partial', 'assessment': '결과가 비어 있다.',
             'evidence': [{'source_id': 'git:PROJECT_SPEC.md', 'section': '# 목표'},
                          {'source_id': 'git:app.py', 'line_start': 1, 'line_end': 2}]}],
            'candidates': [
                {'id': 'c1', 'title': '검색 결과 구현', 'requirement_ids': ['r1'],
                 'evidence': [{'source_id': 'git:PROJECT_SPEC.md'}], 'gap': '결과 누락',
                 'priority': 'high', 'scope': 'app.py', 'completion_criteria': ['검색 결과 반환'],
                 'prerequisites': [], 'plan_draft': 'app.py의 검색 결과를 구현한다.',
                 'disposition': 'new_work', 'related_issues': []}]}

    def test_fixed_sha_ignores_working_tree_and_branch_change(self):
        first = self.git('rev-parse', 'HEAD')
        self.write('app.py', 'working tree change')
        original_git = audit._git
        moved = False

        def racing_git(repo, *args):
            nonlocal moved
            result = original_git(repo, *args)
            if args[:2] == ('rev-parse', '--verify') and not moved:
                moved = True
                self.write('app.py', 'new branch commit')
                self.commit()
            return result

        with patch.object(audit, '_git', side_effect=racing_git):
            snapshot = audit.collect_snapshot('AUD')
        self.assertEqual(snapshot['base_sha'], first)
        code = next(s for s in snapshot['sources'] if s['id'] == 'git:app.py')
        self.assertIn('return []', code['content'])
        self.assertNotEqual(self.git('rev-parse', 'HEAD'), first)
        self.assertTrue(any(o['reason'] == 'missing' for o in snapshot['omissions']))

    def test_secrets_links_binary_and_size(self):
        secret = 'sk-' + 'Q' * 24
        self.write('.env', 'API_KEY=' + secret)
        self.write('.ssh/id_rsa', 'credential')
        self.write('credentials.json', 'credential')
        self.write('secrets.py', 'credential')
        self.write('key.pem', 'credential')
        self.write('ordinary.py', 'token = "' + secret + '"')
        self.write('quoted.py', 'api_key = "supersecretvalue"')
        self.write('big.txt', 'x' * 1100)
        (self.repo / 'binary.dat').write_bytes(b'\0abc')
        self.commit()
        link_oid = self.git('hash-object', '-w', '--stdin', input=b'../../outside')
        self.git('update-index', '--add', '--cacheinfo', '120000,' + link_oid + ',link')
        self.git('update-index', '--add', '--cacheinfo', '160000,' + self.git('rev-parse', 'HEAD') + ',submodule')
        self.git('commit', '-m', 'links')
        with db.connect() as c:
            c.execute('''INSERT INTO issues(project_id,number,title,body,reporter,created_at,updated_at)
                VALUES(?,1,'이력',?,'human:admin',?,?)''', (self.pid, 'password="secretvalue"', db.now_iso(), db.now_iso()))
        snapshot = audit.collect_snapshot('AUD', max_file_bytes=1000)
        serialized = audit._json(snapshot)
        self.assertNotIn(secret, serialized)
        self.assertNotIn('supersecretvalue', serialized)
        self.assertNotIn('secretvalue', serialized)
        omitted = {o['source_id']: o['reason'] for o in snapshot['omissions']}
        for path in ('.env', '.ssh/id_rsa', 'credentials.json', 'secrets.py', 'key.pem'):
            self.assertEqual(omitted['git:' + path], 'restricted_path')
        self.assertEqual(omitted['git:ordinary.py'], 'sensitive_content')
        self.assertEqual(omitted['git:quoted.py'], 'sensitive_content')
        self.assertEqual(omitted['git:link'], 'non_regular_blob')
        self.assertEqual(omitted['git:submodule'], 'non_regular_blob')
        self.assertEqual(omitted['git:big.txt'], 'file_size_limit')
        self.assertEqual(omitted['git:binary.dat'], 'binary')
        self.assertFalse(audit.contains_sensitive(snapshot))
        self.assertFalse(audit._allowed_path('../outside'))
        self.assertFalse(audit._allowed_path('C:/outside'))
        self.assertFalse(audit._allowed_path('a\\b'))
        structured = {'usage': {'api_key': 'confidentialvalue', 'input_tokens': 12}}
        self.assertTrue(audit.contains_sensitive(structured))
        cleaned = audit._safe(structured)
        self.assertFalse(audit.contains_sensitive(cleaned))
        self.assertNotIn('confidentialvalue', audit._json(cleaned))
        self.assertEqual(cleaned['usage']['input_tokens'], 12)

    def test_non_regular_document_parent_and_read_failure(self):
        oid = self.git('hash-object', '-w', '--stdin', input=b'../../outside')
        self.git('update-index', '--add', '--cacheinfo', '120000,' + oid + ',docs')
        self.git('commit', '-m', 'linked docs')
        original = audit._git

        def failing_git(repo, *args):
            if args[:2] == ('cat-file', 'blob'):
                raise issues.StoreError('모의 읽기 실패', 409)
            return original(repo, *args)

        with patch.object(audit, '_git', side_effect=failing_git):
            snapshot = audit.collect_snapshot('AUD')
        omitted = {o['source_id']: o['reason'] for o in snapshot['omissions']}
        self.assertEqual(omitted['git:docs/project/01_PRD.md'], 'non_regular_parent')
        self.assertEqual(omitted['git:app.py'], 'read_failed')

    def test_full_history_over_500_including_tasks_and_fingerprint(self):
        now = db.now_iso()
        with db.connect() as c:
            c.executemany('''INSERT INTO issues(project_id,number,title,reporter,created_at,updated_at,sub_of,sub_number)
                VALUES(?,?,'작업','human:admin',?,?,?,?)''',
                [(self.pid, i, now, now, 1 if i > 500 else None, i - 500 if i > 500 else None) for i in range(1, 551)])
            ids = [r[0] for r in c.execute('SELECT id FROM issues')]
            for iid in ids:
                c.execute("INSERT INTO plans(issue_id,version,body,author,created_at) VALUES(?,1,'계획','human:admin',?)", (iid, now))
                c.execute("INSERT INTO decisions(issue_id,gate,plan_version,verdict,actor,created_at) VALUES(?,'plan',1,'approve','human:admin',?)", (iid, now))
                c.execute("INSERT INTO events(issue_id,actor,kind,created_at) VALUES(?,'human:admin','status',?)", (iid, now))
            run = c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at) VALUES(?,'execute','ok','human:admin',?)", (ids[-1], now)).lastrowid
            c.execute('INSERT INTO task_execution_results(run_id,plan_version,state) VALUES(?,1,\'unmerged\')', (run,))
        first = audit.collect_snapshot('AUD')
        for table in ('issues', 'plans', 'decisions', 'events'):
            self.assertEqual(sum(s.get('table') == table for s in first['sources']), 550)
        self.assertIn('AUD-1-50', {s.get('issue_ref') for s in first['sources']})
        self.assertTrue(any(s.get('table') == 'task_execution_results' for s in first['sources']))
        with db.connect() as c:
            c.execute("UPDATE plans SET body='변경' WHERE issue_id=?", (ids[-1],))
        second = audit.collect_snapshot('AUD')
        self.assertNotEqual(first['history_fingerprint'], second['history_fingerprint'])
        limited = audit.collect_snapshot('AUD', max_input_bytes=4000)
        self.assertLessEqual(len(audit._json(limited).encode('utf-8')), 4000)
        self.assertGreater(limited['omitted_count'], len(limited['omissions']))
        self.assertTrue(any(o['reason'] == 'total_input_limit' for o in limited['omissions']))
        self.assertTrue(limited['omissions_truncated'])
        self.assertIn('git:PROJECT_SPEC.md', {s['id'] for s in limited['sources']})

    def test_spec_conflict_and_no_goal_zero_candidates(self):
        now = db.now_iso()
        with db.connect() as c:
            c.execute('''INSERT INTO project_initializations(project_id,request_id,owner,provider,spec,status,created_at,updated_at)
                VALUES(?,'init','human:admin','codex','다른 목표','complete',?,?)''', (self.pid, now, now))
        snapshot = audit.collect_snapshot('AUD')
        self.assertTrue(snapshot['goal_conflict'])
        self.denied(lambda: audit.validate_report(self.report(), snapshot))
        with db.connect() as c:
            c.execute('DELETE FROM project_initializations')
        self.git('rm', 'PROJECT_SPEC.md')
        self.git('commit', '-m', 'no goal')
        missing = audit.collect_snapshot('AUD')
        self.assertFalse(missing['has_goal_material'])
        empty = {'summary': '목표 근거가 부족해 확인이 필요하다.', 'requirements': [], 'candidates': []}
        self.assertEqual(audit.validate_report(empty, missing), empty)
        self.denied(lambda: audit.validate_report(self.report(), missing))

    def test_report_actual_evidence_and_schema(self):
        snapshot = audit.collect_snapshot('AUD')
        report = self.report()
        self.assertEqual(audit.validate_report(report, snapshot), report)
        schema = json.loads(audit.SCHEMA_PATH.read_text(encoding='utf-8'))
        self.assertEqual(set(schema['required']), set(report))
        self.assertEqual(set(schema['$defs']['candidate']['required']), set(report['candidates'][0]))
        mutations = [
            lambda r: r['requirements'][0]['evidence'].append({'source_id': 'git:uncollected.py'}),
            lambda r: r['requirements'][0]['evidence'][0].update(section='없는 절'),
            lambda r: r['requirements'][0]['evidence'][1].update(line_end=999),
            lambda r: r['requirements'][0]['evidence'][1].update(line_start=None, line_end=None),
            lambda r: r['requirements'][0]['evidence'][0].update(source_id=[]),
            lambda r: r['requirements'][0].update(status='complete'),
            lambda r: r['requirements'].append(copy.deepcopy(r['requirements'][0])),
            lambda r: r['candidates'][0].update(related_issues=['OTHER-1']),
            lambda r: r['candidates'][0].update(disposition='reuse_issue'),
            lambda r: r['candidates'][0].update(requirement_ids=['unknown']),
            lambda r: r.update(candidates=r['candidates'] * 4),
            lambda r: r.update(summary='api_key="confidentialvalue"'),
            lambda r: r.update(summary='x' * audit.MAX_OUTPUT_BYTES),
        ]
        for mutate in mutations:
            bad = copy.deepcopy(report)
            mutate(bad)
            self.denied(lambda: audit.validate_report(bad, snapshot))
        now = db.now_iso()
        with db.connect() as c:
            iid = c.execute("INSERT INTO issues(project_id,number,title,reporter,created_at,updated_at) VALUES(?,1,'t','human:admin',?,?)", (self.pid, now, now)).lastrowid
            rid = c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at) VALUES(?,'execute','ok','human:admin',?)", (iid, now)).lastrowid
        snapshot = audit.collect_snapshot('AUD')
        complete = self.report()
        complete['requirements'][0]['status'] = 'complete'
        complete['requirements'][0]['evidence'].append({'source_id': 'runs:' + str(rid)})
        complete['candidates'] = []
        audit.validate_report(complete, snapshot)
        with db.connect() as c:
            c.execute("UPDATE runs SET status='failed' WHERE id=?", (rid,))
        self.denied(lambda: audit.validate_report(complete, audit.collect_snapshot('AUD')))

    def test_persistence_idempotency_atomic_report_and_late_result(self):
        start = lambda: audit.create_audit(self.pid, 'same', 'codex', 'human:admin', model='test', inputs={'scope': 'all'})
        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(lambda _: start(), range(2)))
        self.assertEqual(rows[0]['id'], rows[1]['id'])
        aid = rows[0]['id']
        self.denied(lambda: audit.create_audit(self.pid, 'same', 'claude', 'human:admin'), 409)
        self.denied(lambda: audit.create_audit(self.pid, 'same', 'codex', 'human:admin', model='test', inputs={'scope': 'other'}), 409)
        self.denied(lambda: audit.create_audit(self.pid, 'second', 'codex', 'human:admin'), 409)
        snapshot = audit.collect_snapshot('AUD')
        audit.save_snapshot(aid, snapshot)
        self.denied(lambda: audit.save_snapshot(aid, snapshot), 409)
        bad = self.report()
        bad['candidates'][0]['requirement_ids'] = ['unknown']
        self.denied(lambda: audit.finish_audit(aid, 'complete', report=bad))
        with db.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM project_audit_requirements').fetchone()[0], 0)
        report = self.report()
        audit.finish_audit(aid, 'complete', report=report, actual_model='actual', usage={'input_tokens': 42})
        result = audit.get_audit(self.pid, aid)
        self.assertEqual(result['report'], report)
        self.assertEqual(result['snapshot'], snapshot)
        self.assertEqual(result['actual_model'], 'actual')
        self.assertEqual(result['usage']['input_tokens'], 42)
        self.denied(lambda: audit.finish_audit(aid, 'failed', error='late'), 409)
        self.denied(lambda: audit.get_audit(self.pid + 99, aid), 404)
        newer = audit.create_audit(self.pid, 'new', 'claude', 'human:admin')
        audit.finish_audit(newer['id'], 'failed', error='password="confidentialvalue"')
        self.assertEqual(audit.get_audit(self.pid, newer['id'])['error'], '[민감값 제외]')
        self.assertEqual(len(audit.list_audits(self.pid)), 2)

    def test_empty_report_round_trip_cancellation_and_no_implicit_work(self):
        self.git('rm', 'PROJECT_SPEC.md')
        self.git('commit', '-m', 'no goal')
        snapshot = audit.collect_snapshot('AUD')
        started = audit.create_audit(self.pid, 'empty-report', 'codex', 'human:admin')
        aid = started['id']
        audit.save_snapshot(aid, snapshot)
        audit.finish_audit(aid, 'complete', report={
            'summary': '목표 자료 확인이 필요하다.', 'requirements': [], 'candidates': []})
        # 모듈을 다시 읽어도 완료 기록과 후보 0개는 DB에 남는다.
        import importlib
        importlib.reload(audit)
        self.assertEqual(audit.get_audit(self.pid, aid)['report']['candidates'], [])
        cancelling = audit.create_audit(self.pid, 'cancel', 'codex', 'human:admin')
        audit.save_snapshot(cancelling['id'], snapshot)
        with db.connect() as c:
            c.execute("UPDATE project_audits SET status='cancelling' WHERE id=?", (cancelling['id'],))
        self.denied(lambda: audit.finish_audit(cancelling['id'], 'complete', report={
            'summary': '늦은 결과', 'requirements': [], 'candidates': []}), 409)
        self.denied(lambda: audit.create_audit(self.pid, 'busy', 'codex', 'human:admin'), 409)
        audit.finish_audit(cancelling['id'], 'cancelled')
        with db.connect() as c:
            for table in ('issues', 'plans', 'decisions', 'jobs', 'runs', 'project_auto_settings_events'):
                self.assertEqual(c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)

    def test_wrong_repo_missing_branch_and_invalid_limits(self):
        self.denied(lambda: audit.collect_snapshot('MISSING'), 404)
        self.denied(lambda: audit.collect_snapshot('AUD', base_branch='absent'), 409)
        self.denied(lambda: audit.collect_snapshot('AUD', max_input_bytes=0))
        self.denied(lambda: audit.collect_snapshot('AUD', max_file_bytes=audit.MAX_FILE_BYTES + 1))
        self.denied(lambda: audit.collect_snapshot('AUD', max_input_bytes=100), 413)
        with db.connect() as c:
            sub = self.repo / 'nested'
            sub.mkdir()
            c.execute('UPDATE projects SET local_path=? WHERE id=?', (str(sub), self.pid))
        self.denied(lambda: audit.collect_snapshot('AUD'), 409)


if __name__ == '__main__':
    unittest.main()
