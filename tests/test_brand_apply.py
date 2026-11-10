"""임시 저장소에서 브랜드 적용·되돌리기·원본 보존과 기존 Task 병합 계약을 검사한다."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory(prefix='brand-apply-tests-')
ROOT = Path(TMP.name)
os.environ['DEV_DATA_DIR'] = str(ROOT/'data')
os.environ['DEV_DB_PATH'] = str(ROOT/'data/dev.db')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'server'))
import brand_apply as brand
import brand_assets as assets
import brand_render as render
import config
import db
import orchestrate
import app
from fastapi import HTTPException
from fastapi.testclient import TestClient

ACTOR = dict(kind='human', name='Brand Admin')


def git(repo, *args):
    result = subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@dev.local', *args],
                            cwd=repo, capture_output=True, text=True, encoding='utf-8')
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class BrandApplyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        git(self.repo, 'init', '-b', orchestrate.BASE_BRANCH)
        (self.repo/'static').mkdir()
        (self.repo/'static/icon.png').write_bytes(b'old icon')
        (self.repo/'static/index.html').write_text('old html')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'initial')
        self.before = git(self.repo, 'rev-parse', 'HEAD')
        self.patcher = patch.object(assets, 'project_root', return_value=str(self.repo))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.settings = patch.object(orchestrate, 'settings', return_value={'build': [], 'restart_when': []})
        self.settings.start()
        self.addCleanup(self.settings.stop)
        brand._states.clear()
        self.threads = []
        real_thread = threading.Thread
        def thread(*args, **kwargs):
            value = real_thread(*args, **kwargs)
            if kwargs.get('target') == brand._worker:
                self.threads.append(value)
            return value
        self.thread_patch = patch.object(brand, 'Thread', side_effect=thread)
        self.thread_patch.start()
        self.addCleanup(self.thread_patch.stop)

    def draft(self):
        import uuid
        identifier = uuid.uuid4().hex
        folder = render.draft_dir(identifier)
        (folder/'files/static').mkdir(parents=True)
        payload = {'static/icon.png': b'new icon', 'static/new.png': b'new resource'}
        for p, data in payload.items():
            (folder/'files'/p).write_bytes(data)
        import time
        metadata = dict(project='TT', created_at=time.time(), slots=['icon'],
                        changes=[dict(path=p) for p in payload],
                        blob_hashes={p: git(self.repo, 'hash-object', '--', p) if (self.repo/p).exists() else None for p in payload})
        (folder/'draft.json').write_text(json.dumps(metadata))
        return identifier

    def finish(self):
        for thread in self.threads:
            thread.join(15)
            self.assertFalse(thread.is_alive())
        return brand.status('TT')

    def clean(self):
        self.assertEqual(git(self.repo, 'status', '--porcelain'), '')
        self.assertEqual(git(self.repo, 'branch', '--list', 'relay/brand-*'), '')
        self.assertEqual(len(git(self.repo, 'worktree', 'list', '--porcelain').split('worktree '))-1, 1)
        for folder in ('brand-worktrees', 'merge-worktrees'):
            self.assertEqual(list((config.DATA_DIR/folder).glob('*')), [])

    def apply(self):
        brand.request('TT', ACTOR, draft_id=self.draft())
        result = self.finish()
        self.assertEqual(result['phase'], 'done', result)
        return result['sha']

    def test_apply_exactly_one_commit_and_history_and_revert(self):
        sha = self.apply()
        self.assertEqual(git(self.repo, 'rev-list', '--count', self.before+'..HEAD'), '1')
        self.assertEqual(git(self.repo, 'rev-parse', sha+'^'), self.before)
        self.assertEqual(len(git(self.repo, 'rev-list', '--parents', '-1', sha).split()), 2)
        self.assertEqual(git(self.repo, 'show', '-s', '--format=%an|%ae', sha), 'Brand Admin|Brand_Admin@dev.local')
        self.assertEqual((self.repo/'static/icon.png').read_bytes(), b'new icon')
        entry = brand.history('TT')[0]
        self.assertEqual(entry['sha'], sha)
        self.assertEqual(entry['slots'], ['icon'])
        self.assertEqual(entry['files'], ['static/icon.png', 'static/new.png'])
        self.assertIsNone(entry['reverted_by'])
        self.clean()
        brand.request('TT', ACTOR, sha=sha)
        result = self.finish()
        self.assertEqual(result['phase'], 'done', result)
        self.assertEqual(git(self.repo, 'rev-list', '--count', self.before+'..HEAD'), '2')
        self.assertEqual((self.repo/'static/icon.png').read_bytes(), b'old icon')
        self.assertFalse((self.repo/'static/new.png').exists())
        entries = brand.history('TT')
        self.assertEqual(entries[1]['reverted_by'], entries[0]['sha'])
        self.clean()

    def test_target_uncommitted_change_is_409_and_preserved(self):
        identifier = self.draft()
        for staged in (False, True):
            (self.repo/'static/icon.png').write_bytes(b'personal change')
            if staged:
                git(self.repo, 'add', 'static/icon.png')
            with self.assertRaises(HTTPException) as caught:
                brand.request('TT', ACTOR, draft_id=identifier)
            self.assertEqual(caught.exception.status_code, 409)
            self.assertEqual(caught.exception.detail['files'], ['static/icon.png'])
            self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), self.before)
            self.assertEqual((self.repo/'static/icon.png').read_bytes(), b'personal change')
        self.assertFalse(self.threads)

    def test_stale_committed_draft_is_rejected(self):
        identifier = self.draft()
        (self.repo/'static/icon.png').write_bytes(b'other commit')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'other change')
        with self.assertRaises(HTTPException) as caught:
            brand.request('TT', ACTOR, draft_id=identifier)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertFalse(self.threads)

    def test_waiting_duplicate_and_revalidation(self):
        identifier = self.draft()
        with orchestrate._lock:
            brand.request('TT', ACTOR, draft_id=identifier)
            self.assertEqual(brand.status('TT')['phase'], 'waiting')
            with self.assertRaises(HTTPException):
                brand.request('TT', ACTOR, draft_id=identifier)
            (self.repo/'static/icon.png').write_bytes(b'changed while waiting')
        result = self.finish()
        self.assertEqual(result['phase'], 'failed')
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), self.before)
        self.assertEqual((self.repo/'static/icon.png').read_bytes(), b'changed while waiting')

    def test_merge_failure_cleans_and_preserves_original(self):
        for outcome in ('on_hold', 'changes_requested'):
            with patch.object(orchestrate, '_merge', return_value=(outcome, 'test failure')):
                brand.request('TT', ACTOR, draft_id=self.draft())
                self.assertEqual(self.finish()['phase'], 'failed')
            self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), self.before)
            self.assertEqual((self.repo/'static/icon.png').read_bytes(), b'old icon')
            self.assertFalse((self.repo/'static/new.png').exists())
            self.clean()

    def test_commit_failure_cleans_and_preserves_original(self):
        original = brand._git
        def fail(repo, *args):
            if 'commit' in args:
                raise ValueError('commit failed')
            return original(repo, *args)
        with patch.object(brand, '_git', side_effect=fail):
            brand.request('TT', ACTOR, draft_id=self.draft())
            self.assertEqual(self.finish()['phase'], 'failed')
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), self.before)
        self.clean()

    def test_revert_conflict_preserves_original_and_cleans(self):
        sha = self.apply()
        (self.repo/'static/icon.png').write_bytes(b'newer conflicting content')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'later work')
        before = git(self.repo, 'rev-parse', 'HEAD')
        brand.request('TT', ACTOR, sha=sha)
        self.assertEqual(self.finish()['phase'], 'failed')
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), before)
        self.assertEqual((self.repo/'static/icon.png').read_bytes(), b'newer conflicting content')
        self.clean()

    def test_original_task_path_still_creates_merge_commit(self):
        branch = 'relay/TT-1'
        git(self.repo, 'checkout', '-b', branch)
        (self.repo/'task.txt').write_text('task')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'task')
        task = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, 'checkout', orchestrate.BASE_BRANCH)
        phase, sha = orchestrate._merge(str(self.repo), 'TT-1', [])
        self.assertEqual(phase, 'merged', sha)
        self.assertEqual(git(self.repo, 'rev-list', '--parents', '-1', sha).split(), [sha, self.before, task])
        self.clean()

    def test_fast_forward_rejects_divergence_without_touching_base(self):
        git(self.repo, 'checkout', '-b', 'relay/candidate')
        (self.repo/'task.txt').write_text('task')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'task')
        git(self.repo, 'checkout', orchestrate.BASE_BRANCH)
        (self.repo/'other.txt').write_text('other')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'other')
        before = git(self.repo, 'rev-parse', 'HEAD')
        phase, _ = orchestrate._merge(str(self.repo), 'TT-brand', [], branch='relay/candidate', fast_forward=True)
        self.assertEqual(phase, 'changes_requested')
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), before)
        self.clean()

    def test_invalid_draft_project_sha_and_no_change(self):
        identifier = self.draft()
        with self.assertRaises(HTTPException):
            brand.request('OTHER', ACTOR, draft_id=identifier)
        with self.assertRaises(HTTPException):
            brand.request('TT', ACTOR, sha='--all')
        folder, metadata = render.read_draft('TT', identifier)
        metadata['changes'] = []
        (folder/'draft.json').write_text(json.dumps(metadata))
        with self.assertRaises(HTTPException):
            brand.request('TT', ACTOR, draft_id=identifier)
        self.assertFalse(self.threads)

    def test_deploy_failure_reverts_single_parent_brand_commit(self):
        sha = self.apply()
        cfg = dict(pm2_app='fixture-only', restart_when=['static/*'])
        with patch.object(orchestrate, '_restart_guard', return_value=(None, [])), \
             patch.object(orchestrate, '_busy', return_value=False), \
             patch.object(orchestrate, '_restart', side_effect=['simulated failure', None]), \
             patch.object(orchestrate, '_healthy', return_value=True):
            phase, note = orchestrate.deploy(str(self.repo), cfg, sha)
        self.assertEqual(phase, 'changes_requested', note)
        self.assertEqual((self.repo/'static/icon.png').read_bytes(), b'old icon')
        self.assertFalse((self.repo/'static/new.png').exists())
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD^'), sha)
        self.clean()

    def test_new_url_checks_use_served_url_not_dev_preview_endpoint(self):
        html = '<link rel="manifest" href="/manifest.json"><meta property="og:image" content="https://public.example/new.png">'
        (self.repo/'static/index.html').write_text(html)
        (self.repo/'static/manifest.json').write_text('{"icons": [{"src": "/new.png"}]}')
        (self.repo/'static/new.png').write_bytes(b'new')
        info = dict(html=['static/index.html'], manifest={'path':'static/manifest.json'},
                    slots=[dict(files=[dict(path='static/new.png', url='/api/projects/TT/brand/file?path=static/new.png')])])
        with patch.object(assets, 'inspect_repo', return_value=info), \
             patch.object(orchestrate, '_get', return_value=(404, '')) as get:
            warnings = brand._check_urls(self.repo, 'TT', ['static/new.png'], {'health_url':'http://127.0.0.1:9999/api/health'})
        get.assert_called_once_with('http://127.0.0.1:9999/new.png')
        self.assertEqual(len(warnings), 1)

    def test_failed_build_preserves_base_and_cleans_temporary_worktrees(self):
        with patch.object(orchestrate, 'selected_builds', side_effect=ValueError('build rejected')):
            brand.request('TT', ACTOR, draft_id=self.draft())
            result = self.finish()
        self.assertEqual(result['phase'], 'failed')
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), self.before)
        self.clean()

    def test_api_human_only_and_history_status(self):
        import auth
        async def human(cookie):
            return {'username': 'admin', 'role': 'admin'}
        async def agent(cookie):
            return None
        client = TestClient(app.app, headers={app.CSRF_HEADER: app.CSRF_VALUE})
        client.cookies.set('ns_session', 'test')
        with patch.object(auth, 'nightshift_user', side_effect=human):
            identifier = self.draft()
            (self.repo/'static/icon.png').write_bytes(b'personal change')
            response = client.post('/api/projects/TT/brand/apply', json={'draft_id': identifier})
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()['files'], ['static/icon.png'])
            (self.repo/'static/icon.png').write_bytes(b'old icon')
            response = client.post('/api/projects/TT/brand/apply', json={'draft_id': identifier})
            self.assertEqual(response.status_code, 202, response.text)
            self.finish()
            response = client.get('/api/projects/TT/brand')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['apply']['phase'], 'done')
            self.assertEqual(len(response.json()['history']), 1)
        with patch.object(auth, 'agent_for_key', return_value={'id':1,'name':'agent','kind':'agent','model':''}):
            for path in ('apply', 'revert'):
                response = client.post('/api/projects/TT/brand/'+path, headers=dict([('Authorization', 'Bearer ' + 'dev_' + 'test')]), json={})
                self.assertEqual(response.status_code, 403, response.text)


if __name__ == '__main__':
    unittest.main()
