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
        location = schema['$defs']['evidence']['items']
        self.assertNotIn('dependentRequired', location)
        self.assertEqual(set(location['required']), set(location['properties']))
        nullable = copy.deepcopy(report)
        for row in nullable['requirements'] + nullable['candidates']:
            for evidence in row['evidence']:
                for field in ('line_start', 'line_end', 'section'):
                    evidence.setdefault(field, None)
        self.assertEqual(audit.validate_report(nullable, snapshot), nullable)
        mutations = [
            lambda r: r['requirements'][0]['evidence'].append({'source_id': 'git:uncollected.py'}),
            lambda r: r['requirements'][0]['evidence'][0].update(section='없는 절'),
            lambda r: r['requirements'][0]['evidence'][1].update(line_end=999),
            lambda r: r['requirements'][0]['evidence'][1].update(line_start=1, line_end=None),
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


class PublicationTests(unittest.TestCase):
    setUp = AuditTests.setUp
    git = AuditTests.git
    write = AuditTests.write
    commit = AuditTests.commit
    denied = AuditTests.denied
    report = AuditTests.report
    actor = {'kind': 'human', 'id': 1, 'name': 'admin'}

    def completed_reuse(self):
        issue = issues.create_issue(self.actor, 'AUD', '기존 검색', '')
        issues.post_plan(self.actor, issue['ref'], '## Tasks\n1. 검색 구현 | 파일: app.py | 확인: 검색 검사 | 선행: 없음')
        issues.decide(self.actor, issue['ref'], 'approve', plan_version=1)
        report = self.report()
        report['candidates'][0].update(disposition='reuse_issue', related_issues=[issue['ref']], prerequisites=['환경 확인 필요'], plan_draft='Task 1: 저장·연결 공백을 보완한다.')
        snapshot = audit.collect_snapshot('AUD')
        row = audit.create_audit(self.pid, 'reuse', 'codex', 'human:admin')
        audit.save_snapshot(row['id'], snapshot)
        audit.finish_audit(row['id'], 'complete', report=report)
        return row['id'], issue

    def test_followup_publication_is_idempotent_and_preserves_existing_approval(self):
        aid, issue = self.completed_reuse()
        with db.connect() as c:
            before = {table: [tuple(r) for r in c.execute(f'SELECT * FROM {table}')] for table in ('plans', 'decisions', 'jobs', 'runs')}
            count = c.execute('SELECT COUNT(*) FROM issues').fetchone()[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: audit.publish(self.actor, 'AUD', aid, ['c1']), range(2)))
        self.assertEqual(results[0], results[1])
        with db.connect() as c:
            for table, rows in before.items():
                self.assertEqual([tuple(r) for r in c.execute(f'SELECT * FROM {table}')], rows)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM issues').fetchone()[0], count + 1)
            new_issue = issues.get_issue(results[0]['publications'][0]['issue_ref'])
            self.assertNotEqual(new_issue['ref'], issue['ref'])
            for value in (issue['ref'], '환경 확인 필요', 'Task 1: 저장·연결 공백을 보완한다.', f'점검 #{aid}'):
                self.assertIn(value, new_issue['body'])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM events WHERE issue_id=? AND kind='comment' AND json_extract(data_json,'$.audit_id')=?", (issue['id'], aid)).fetchone()[0], 0)

    def test_followup_rejects_invalid_candidates_and_stale_baseline(self):
        aid, issue = self.completed_reuse()
        self.denied(lambda: audit.publish(self.actor, 'AUD', aid, ['missing']), 404)
        self.denied(lambda: audit.publish({'kind': 'agent'}, 'AUD', aid, ['c1']), 403)
        self.write('app.py', 'print("changed")\n'); self.commit()
        self.denied(lambda: audit.publish(self.actor, 'AUD', aid, ['c1']), 409)
        self.assertEqual(audit.get_audit(self.pid, aid)['publications'], [])

    def test_old_comment_mapping_can_publish_a_new_issue(self):
        aid, issue = self.completed_reuse()
        with db.connect() as c:
            c.execute("INSERT INTO project_audit_publications VALUES(?,?,?,?,?,?,'comment')", (aid, 'c1', self.pid, issue['id'], issue['ref'], db.now_iso()))
            c.execute('ALTER TABLE project_audit_publications DROP COLUMN mode')
            c.execute(f'PRAGMA user_version={len(db.MIGRATIONS) - 1}')
        db.init()
        self.assertEqual(audit.get_audit(self.pid, aid)['publications'][0]['mode'], 'comment')
        publication = audit.publish(self.actor, 'AUD', aid, ['c1'])['publications'][0]
        self.assertNotEqual(publication['issue_ref'], issue['ref'])
        self.assertEqual(publication['mode'], 'issue')

    def completed(self, request='publish', count=1):
        report = self.report()
        report['candidates'][0]['plan_draft'] = '## Tasks\n1. 검색 구현 | 파일: app.py | 확인: 검색 결과 검사 | 선행: 없음'
        if count == 2:
            report['requirements'].append(dict(report['requirements'][0], id='r2', title='정렬'))
            report['candidates'].append(dict(report['candidates'][0], id='c2', title='정렬 구현', requirement_ids=['r2']))
        snapshot = audit.collect_snapshot('AUD')
        row = audit.create_audit(self.pid, request, 'codex', 'human:admin')
        audit.save_snapshot(row['id'], snapshot)
        audit.finish_audit(row['id'], 'complete', report=report)
        return row['id']

    def test_concurrent_multi_publish_is_atomic_and_has_no_automation(self):
        aid = self.completed(count=2)
        with ThreadPoolExecutor(max_workers=4) as pool:
            outputs = list(pool.map(lambda _: audit.publish(self.actor, 'AUD', aid, ['c1', 'c2']), range(4)))
        self.assertTrue(all(o == outputs[0] for o in outputs))
        with db.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM issues').fetchone()[0], 2)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM plans WHERE version=1').fetchone()[0], 2)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM issues WHERE status='backlog' AND parent_id IS NULL").fetchone()[0], 2)
            for table in ('decisions', 'runs', 'jobs', 'review_plan_runs', 'project_auto_settings_events'):
                self.assertEqual(c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)

    def test_publication_rollback_on_second_plan_failure(self):
        aid = self.completed(count=2)
        original = issues._post_plan
        calls = []
        def fail(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise issues.StoreError('injected', 409)
            return original(*args, **kwargs)
        with patch.object(issues, '_post_plan', side_effect=fail):
            self.denied(lambda: audit.publish(self.actor, 'AUD', aid, ['c1', 'c2']), 409)
        with db.connect() as c:
            for table in ('issues', 'plans', 'events', 'project_audit_publications'):
                self.assertEqual(c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)

    def test_closed_deleted_and_other_audit_reuse_mapping(self):
        aid = self.completed()
        first = audit.publish(self.actor, 'AUD', aid, ['c1'])['publications'][0]
        issues.set_status(self.actor, first['issue_ref'], 'closed')
        second = self.completed('second')
        self.assertEqual(audit.publish(self.actor, 'AUD', second, ['c1'])['publications'][0]['issue_ref'], first['issue_ref'])
        issues.delete_issue(self.actor, first['issue_ref'])
        third = self.completed('third')
        self.assertIsNone(audit.publish(self.actor, 'AUD', third, ['c1'])['publications'][0]['issue_id'])
        self.assertEqual(audit.publish(self.actor, 'AUD', aid, ['c1'])['publications'][0]['issue_ref'], first['issue_ref'])
        with db.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM issues').fetchone()[0], 0)

    def test_changed_history_baseline_and_invalid_selection(self):
        aid = self.completed()
        self.denied(lambda: audit.publish(self.actor, 'AUD', aid, ['foreign']), 404)
        self.denied(lambda: audit.publish(self.actor, 'AUD', aid, ['c1', 'foreign']), 404)
        self.denied(lambda: audit.publish(self.actor, 'AUD', aid, ['c1', 'c1']))
        issue = issues.create_issue(self.actor, 'AUD', 'unrelated', 'history changed')
        self.denied(lambda: audit.publish(self.actor, 'AUD', aid, ['c1']), 409)
        issues.delete_issue(self.actor, issue['ref'])
        self.write('app.py', 'changed baseline')
        self.commit()
        self.assertTrue(audit.detail(self.actor, 'AUD', aid)['stale'])
        self.denied(lambda: audit.publish(self.actor, 'AUD', aid, ['c1']), 409)

    def test_project_deletion_includes_audits_and_blocks_active(self):
        aid = self.completed()
        before = issues.check_project_deletion(self.actor, 'AUD')
        self.assertTrue(before['confirmation_required'])
        active = audit.create_audit(self.pid, 'active-delete', 'codex', 'human:admin')
        self.denied(lambda: issues.delete_project(self.actor, 'AUD', before['confirmation_token']), 409)
        audit.finish_audit(active['id'], 'cancelled')
        self.denied(lambda: issues.delete_project(self.actor, 'AUD', before['confirmation_token']), 409)
        audit.publish(self.actor, 'AUD', aid, ['c1'])
        state = issues.check_project_deletion(self.actor, 'AUD')
        issues.delete_project(self.actor, 'AUD', state['confirmation_token'])
        with db.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM project_audits').fetchone()[0], 0)

    def test_latest_issue_body_status_plan_and_dependencies_are_rechecked(self):
        issue = issues.create_issue(self.actor, 'AUD', '기존 작업', '다른 범위')
        dependency = issues.create_issue(self.actor, 'AUD', '선행 작업', '다른 범위')
        for field in ('body', 'status', 'plan', 'dependency'):
            with self.subTest(field=field):
                aid = self.completed('history-' + field)
                with db.connect() as c:
                    if field == 'body':
                        c.execute('UPDATE issues SET body=? WHERE id=?', ('검색 변경', issue['id']))
                    elif field == 'status':
                        c.execute("UPDATE issues SET status='closed' WHERE id=?", (issue['id'],))
                    elif field == 'plan':
                        c.execute('INSERT INTO plans(issue_id,version,body,author,created_at) VALUES(?,1,?,?,?)',
                                  (issue['id'], '검색 계획 변경', 'human:admin', db.now_iso()))
                    else:
                        c.execute('INSERT INTO issue_deps VALUES(?,?)', (issue['id'], dependency['id']))
                self.denied(lambda: audit.publish(self.actor, 'AUD', aid, ['c1']), 409)
        with db.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM project_audit_publications').fetchone()[0], 0)

    def test_invalid_drafts_dispositions_and_existing_duplicate_block_publication(self):
        aid = self.completed()
        original = audit.get_audit(self.pid, aid)['report']['candidates'][0]
        for draft in ('조사만 한다.', '## Tasks\n1. 조사 | 파일: app.py | 확인: 검사',
                      '## Tasks\n1. 구현 | 파일: ../external.py | 확인: 검사',
                      '## Tasks\n1. 구현 | 파일: app.py | 확인: 검사\n## 검사 선택\n```json\n{}invalid\n```'):
            with self.subTest(draft=draft):
                data = dict(original, plan_draft=draft)
                with db.connect() as c:
                    c.execute('UPDATE project_audit_candidates SET data_json=? WHERE audit_id=?', (audit._json(data), aid))
                self.denied(lambda: audit.publish(self.actor, 'AUD', aid, ['c1']), 409 if 'invalid' not in draft else 400)
        with db.connect() as c:
            c.execute("UPDATE project_audit_candidates SET disposition='human_confirmation' WHERE audit_id=?", (aid,))
        self.denied(lambda: audit.publish(self.actor, 'AUD', aid, ['c1']), 409)
        issues.create_issue(self.actor, 'AUD', '검색 결과 구현', 'app.py 구현')
        newer = self.completed('duplicate-before-snapshot')
        self.denied(lambda: audit.publish(self.actor, 'AUD', newer, ['c1']), 409)

    def test_replay_survives_missing_repo_and_snapshot_collection_races(self):
        aid = self.completed()
        result = audit.publish(self.actor, 'AUD', aid, ['c1'])
        with patch.object(audit, 'collect_snapshot', side_effect=AssertionError('replay must not read Git')):
            self.assertEqual(audit.publish(self.actor, 'AUD', aid, ['c1']), result)
        newer = self.completed('race')
        original = audit.collect_snapshot
        def moved(*args, **kwargs):
            snapshot = original(*args, **kwargs)
            self.write('app.py', 'new commit after collection')
            self.commit()
            return snapshot
        with patch.object(audit, 'collect_snapshot', side_effect=moved):
            self.denied(lambda: audit.publish(self.actor, 'AUD', newer, ['c1']), 409)

    def test_stored_initial_spec_and_project_path_change_block_publication(self):
        aid = self.completed()
        with db.connect() as c:
            c.execute('UPDATE projects SET local_path=? WHERE id=?', (str(self.root / 'absent'), self.pid))
        self.assertTrue(audit.detail(self.actor, 'AUD', aid)['stale'])
        self.denied(lambda: audit.publish(self.actor, 'AUD', aid, ['c1']), 409)
        with db.connect() as c:
            c.execute('UPDATE projects SET local_path=? WHERE id=?', (str(self.repo), self.pid))
        # 기존 초기 생성 저장 계약으로 기획서를 추가하면 Git SHA가 같아도 오래된 결과다.
        with db.connect() as c:
            c.execute('''INSERT INTO project_initializations(project_id,request_id,owner,spec,provider,status,created_at,updated_at)
                VALUES(?,'changed-spec','human:admin',?,?,'failed',?,?)''',
                (self.pid, '새 목표 기획서', 'codex', db.now_iso(), db.now_iso()))
        self.assertTrue(audit.detail(self.actor, 'AUD', aid)['stale'])
        self.denied(lambda: audit.publish(self.actor, 'AUD', aid, ['c1']), 409)

    def test_new_repository_file_is_a_valid_implementation_draft(self):
        aid = self.completed()
        with db.connect() as c:
            data = json.loads(c.execute('SELECT data_json FROM project_audit_candidates WHERE audit_id=?', (aid,)).fetchone()[0])
            data['plan_draft'] = '## Tasks\n1. 검색 구현 | 파일: search.py | 확인: 검색 결과 검사 | 선행: 없음'
            c.execute('UPDATE project_audit_candidates SET data_json=? WHERE audit_id=?', (audit._json(data), aid))
        self.assertEqual(len(audit.publish(self.actor, 'AUD', aid, ['c1'])['publications']), 1)

    def test_actual_app_all_routes_auth_csrf_and_project_boundaries(self):
        import app
        import auth
        import project_audit_runner as runner
        from fastapi.testclient import TestClient
        aid = self.completed()
        base = '/api/projects/AUD/audits'
        routes = [('GET', base), ('GET', base + '/preflight'), ('GET', base + f'/{aid}'),
                  ('POST', base), ('POST', base + f'/{aid}/cancel'), ('POST', base + f'/{aid}/publish')]
        async def user(cookie):
            return {'id': 1, 'username': 'admin', 'role': 'admin' if cookie == 'admin' else 'user'} if cookie else None
        with patch.object(auth, 'nightshift_user', side_effect=user), TestClient(app.app) as client:
            for method, path in routes:
                self.assertEqual(client.request(method, path, headers={'X-Requested-With': 'dev'}).status_code, 401)
            client.cookies.set('ns_session', 'member')
            self.assertEqual(client.get(base).status_code, 403)
            client.cookies.set('ns_session', 'admin')
            for method, path in routes:
                if method == 'POST':
                    self.assertEqual(client.post(path, json={}).status_code, 403)
            with patch.object(auth, 'agent_for_key', return_value={'id': 1, 'name': 'agent', 'model': ''}):
                for method, path in routes:
                    self.assertEqual(client.request(method, path, headers={'Authorization': 'Bearer fake'}).status_code, 403)
            self.assertEqual(client.get(base).status_code, 200)
            self.assertEqual(client.get(base + '/preflight?provider=codex').status_code, 200)
            self.assertEqual(client.get(base + '/preflight?provider=unknown').status_code, 400)
            self.assertEqual(client.get(base + f'/{aid}').status_code, 200)
            issues.create_project(self.actor, 'OTHER', 'other')
            for suffix, method in [(f'/{aid}', 'GET'), (f'/{aid}/cancel', 'POST'), (f'/{aid}/publish', 'POST')]:
                self.assertEqual(client.request(method, '/api/projects/OTHER/audits' + suffix,
                    headers={'X-Requested-With': 'dev'}, json={'candidate_ids': ['c1'], 'candidate_id': 'c1', 'issue_ref': 'AUD-1'}).status_code, 404)
            def fake_run(project, row):
                with runner._lock:
                    runner._workers.discard(row['id'])
            with patch.object(runner, '_run', side_effect=fake_run):
                response = client.post(base, headers={'X-Requested-With': 'dev'},
                    json={'request_id': 'api-start', 'provider': 'codex'})
                self.assertEqual(response.status_code, 200, response.text)
                running = response.json()['id']
                repeated = client.post(base, headers={'X-Requested-With': 'dev'},
                    json={'request_id': 'api-start', 'provider': 'codex'})
                self.assertEqual(repeated.status_code, 200, repeated.text)
                self.assertEqual(repeated.json()['id'], running)
            self.assertEqual(client.post(base + f'/{running}/cancel', headers={'X-Requested-With': 'dev'}).status_code, 200)
            response = client.post(base + f'/{aid}/publish', headers={'X-Requested-With': 'dev'}, json={'candidate_ids': ['c1']})
            self.assertEqual(response.status_code, 200, response.text)


if __name__ == '__main__':
    unittest.main()
