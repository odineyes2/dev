"""첨부 저장·마이그레이션·권한·실패 복구를 임시 DATA_DIR에서 검사한다."""
import os
import json
import struct
import zlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))
os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
import config, db, issues, attachments

H = {'kind': 'human', 'name': 'admin'}
OTHER = {'kind': 'human', 'name': 'other'}
AGENT = {'kind': 'agent', 'id': 1}


class AttachmentsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config.DATA_DIR = Path(self.tmp.name)
        config.DB_PATH = config.DATA_DIR / 'dev.db'
        db.init()
        issues.create_project(H, 'DEV', 'dev')
        self.issue = issues.create_issue(H, 'DEV', 'issue')

    def upload(self, name='note.md', data=b'# hello'):
        return attachments.upload(H, name, iter([data[:2], data[2:]]))

    def capture(self, directory='capture-new', paths=None):
        root = config.DATA_DIR / 'worktree'
        folder = root / '.ui-captures' / directory
        folder.mkdir(parents=True, exist_ok=True)
        def chunk(kind, body):
            return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body))
        png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
               + chunk(b'IDAT', zlib.compress(b'\0\xff\0\0')) + chunk(b'IEND', b''))
        (folder / 'screen.png').write_bytes(png)
        (folder / 'manifest.json').write_text(json.dumps({'version': 1, 'captures': [
            {'path': p} for p in (paths or [f'.ui-captures/{directory}/screen.png'])], 'errors': []}))
        return root, folder, png

    def capture_run(self):
        with db.connect() as c:
            return c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at,log_file) VALUES(?,'execute','running','human:admin',?,'test.log')",
                             (self.issue['id'], db.now_iso())).lastrowid

    def test_internal_capture_registration_and_retry(self):
        root, old, png = self.capture('capture-old')
        baseline = attachments.capture_baseline(root)
        self.capture()
        run = self.capture_run()
        self.assertEqual(attachments.collect_captures(self.issue['ref'], run, root, baseline), (1, []))
        self.assertEqual(attachments.collect_captures(self.issue['ref'], run, root, baseline), (0, []))
        a = issues.get_issue(self.issue['ref'])['attachments'][0]
        self.assertEqual(a['media_type'], 'image/png')
        with attachments.open_content(attachments.get(AGENT, a['id'], self.issue['ref'])) as f:
            self.assertEqual(f.read(), png)
        self.capture_run()
        self.assertTrue(attachments.collect_captures(self.issue['ref'], run, root, baseline)[1])
        self.assertEqual(len(attachments.list_for_issue(self.issue['id'])), 1)
        attachments.delete(H, a['id'])
        self.assertEqual(attachments.list_for_issue(self.issue['id']), [])

    def test_internal_capture_rejects_paths_format_and_limits(self):
        run = self.capture_run()
        for source in ('../screen.png', '/screen.png', 'C:/screen.png',
                       '.ui-captures/capture-new/../screen.png', '.ui-captures/capture-old/screen.png',
                       '.ui-captures/capture-new/note.md'):
            root, folder, _ = self.capture(paths=[source])
            self.assertTrue(attachments.collect_captures(self.issue['ref'], run, root, set())[1])
        root, folder, _ = self.capture()
        with patch.object(Path, 'is_symlink', return_value=True):
            with self.assertRaises(issues.StoreError):
                attachments.collect_captures(self.issue['ref'], run, root, set())
        with patch.object(config, 'ATTACHMENT_MAX_BYTES', 8):
            self.assertTrue(attachments.collect_captures(self.issue['ref'], run, root, set())[1])
        for limit in ('ATTACHMENT_MAX_COUNT', 'ATTACHMENT_TOTAL_BYTES'):
            with patch.object(config, limit, 0):
                self.assertTrue(attachments.collect_captures(self.issue['ref'], run, root, set())[1])
        (folder / 'screen.png').write_bytes(b'\x89PNG\r\n\x1a\n')
        self.assertTrue(attachments.collect_captures(self.issue['ref'], run, root, set())[1])
        self.assertEqual(attachments.list_for_issue(self.issue['id']), [])

    def test_internal_capture_malformed_errors_preserves_valid_partial_results(self):
        root, folder, _ = self.capture()
        manifest_path = folder / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['errors'] = None
        manifest['captures'].insert(0, None)
        manifest_path.write_text(json.dumps(manifest))
        added, warnings = attachments.collect_captures(self.issue['ref'], self.capture_run(), root, set())
        self.assertEqual(added, 1)
        self.assertEqual(len(warnings), 2)
        self.assertEqual(len(attachments.list_for_issue(self.issue['id'])), 1)

    def test_internal_capture_database_failure_removes_copy(self):
        root, folder, _ = self.capture()
        run = self.capture_run()
        with patch.object(attachments, '_insert', side_effect=sqlite3.OperationalError('DB failed')):
            with self.assertRaises(sqlite3.Error):
                attachments.collect_captures(self.issue['ref'], run, root, set())
        self.assertEqual(attachments.list_for_issue(self.issue['id']), [])
        self.assertEqual(list((config.DATA_DIR / 'attachments').iterdir()), [])

    def test_internal_capture_partial_limit_and_wrong_task(self):
        root, folder, png = self.capture(paths=['.ui-captures/capture-new/screen.png',
                                               '.ui-captures/capture-new/second.png'])
        (folder / 'second.png').write_bytes(png)
        run = self.capture_run()
        other = issues.create_issue(H, 'DEV', 'other')
        self.assertTrue(attachments.collect_captures(other['ref'], run, root, set())[1])
        self.assertEqual(attachments.list_for_issue(other['id']), [])
        with patch.object(config, 'ATTACHMENT_MAX_COUNT', 1):
            added, warnings = attachments.collect_captures(self.issue['ref'], run, root, set())
        self.assertEqual(added, 1)
        self.assertTrue(warnings)
        self.assertEqual(len(attachments.list_for_issue(self.issue['id'])), 1)
        # 루트가 아니라 PNG 자체가 링크인 경우에도 읽지 않는다.
        original = Path.is_symlink
        with patch.object(Path, 'is_symlink', lambda p: p.name == 'second.png' or original(p)):
            self.assertTrue(attachments.collect_captures(self.issue['ref'], run, root, set())[1])

    def link(self, ids, actor=H, iid=None):
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            attachments.link(c, actor, iid or self.issue['id'], ids)

    def test_existing_database_upgrade(self):
        old = config.DATA_DIR / 'old.db'
        with sqlite3.connect(old) as c:
            for n, sql in enumerate(db.MIGRATIONS[:-1], 1):
                c.executescript(sql + f'PRAGMA user_version={n};')
        c.close()
        config.DB_PATH = old
        issues.create_project(H, 'OLD', 'preserved')
        item = issues.create_issue(H, 'OLD', 'old body', body='keep')
        self.assertEqual(db.init(), len(db.MIGRATIONS))
        self.assertEqual(db.init(), len(db.MIGRATIONS))
        self.assertEqual(issues.get_issue(item['ref'])['body'], 'keep')
        self.upload()

    def test_formats_and_names(self):
        fixtures = {
            'x.PNG': b'\x89PNG\r\n\x1a\n', 'x.jpg': b'\xff\xd8\xff',
            'x.gif': b'GIF89a', 'x.webp': b'RIFF0000WEBP',
            'x.mp4': b'0000ftypisom', 'x.webm': b'\x1aE\xdf\xa3webm',
            'x.mp3': b'ID3', 'x.wav': b'RIFF0000WAVE', 'x.ogg': b'OggS',
            'x.m4a': b'0000ftypM4A ', 'x.md': '한글'.encode(), 'x.json': b'{"ok":true}',
        }
        for name, data in fixtures.items():
            with self.subTest(name=name):
                a = self.upload(name, data)
                self.assertEqual(attachments.file_path(a['storage_key']).read_bytes(), data)
        for name, data in [('x.svg', b'<svg>'), ('x.html', b'<html>'), ('x.png', b'<html>'),
                           ('x.json', b'{'), ('x.json', b'NaN'), ('x.md', b'\xff'),
                           ('x.md', b'\x00'), ('x.md', b''), ('../x.md', b'x'),
                           ('x\\a.md', b'x'), ('C:x.md', b'x'), ('x\n.md', b'x')]:
            with self.subTest(name=name), self.assertRaises(issues.StoreError):
                self.upload(name, data)

    def test_upload_failures_and_limit(self):
        with patch.object(config, 'ATTACHMENT_MAX_BYTES', 4):
            with self.assertRaises(issues.StoreError):
                self.upload(data=b'12345')
        def interrupted():
            yield b'abc'
            raise OSError('stream interrupted')
        with self.assertRaises(OSError):
            attachments.upload(H, 'x.md', interrupted())
        with patch.object(attachments, '_insert', side_effect=sqlite3.OperationalError('DB failed')):
            with self.assertRaises(sqlite3.Error):
                self.upload()
        with patch.object(Path, 'open', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.upload()
        with db.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM attachments').fetchone()[0], 0)
        self.assertEqual(list((config.DATA_DIR / 'attachments').iterdir()), [])

    def test_ownership_atomic_link_and_retry(self):
        a, b = self.upload(), self.upload()
        for actor in (OTHER, AGENT):
            with self.assertRaises(issues.StoreError):
                self.link([a['id']], actor)
            with self.assertRaises(issues.StoreError):
                attachments.delete(actor, a['id'])
        with self.assertRaises(issues.StoreError):
            attachments.upload(AGENT, 'x.md', [b'x'])
        for ids in ([a['id'], a['id']], [a['id'], 'missing']):
            with self.assertRaises(issues.StoreError):
                self.link(ids)
        with self.assertRaises(issues.StoreError):
            self.link('not-a-list')
        self.assertEqual(attachments.list_for_issue(self.issue['id']), [])
        self.link([a['id']])
        self.link([a['id']])
        self.assertEqual(len(attachments.list_for_issue(self.issue['id'])), 1)
        another = issues.create_issue(H, 'DEV', 'other')
        with self.assertRaises(issues.StoreError):
            self.link([a['id']], iid=another['id'])
        with patch.object(config, 'ATTACHMENT_MAX_COUNT', 1):
            with self.assertRaises(issues.StoreError):
                self.link([b['id']])
        with patch.object(config, 'ATTACHMENT_TOTAL_BYTES', a['size']):
            with self.assertRaises(issues.StoreError):
                self.link([b['id']])

    def test_create_rollback_and_success(self):
        a = self.upload()
        with self.assertRaises(issues.StoreError):
            issues.create_issue(H, 'DEV', 'failed', attachment_ids=[a['id'], 'missing'])
        created = issues.create_issue(H, 'DEV', 'success', attachment_ids=[a['id']])
        self.assertEqual(created['number'], self.issue['number'] + 1)
        self.assertEqual(len(attachments.list_for_issue(created['id'])), 1)

    def test_expiry_cancel_and_issue_deletion(self):
        a = self.upload()
        with db.connect() as c:
            c.execute("UPDATE attachments SET expires_at='2000-01-01' WHERE id=?", (a['id'],))
        with self.assertRaises(issues.StoreError):
            self.link([a['id']])
        attachments.cleanup()
        self.assertFalse(attachments.file_path(a['storage_key']).exists())
        a = self.upload()
        attachments.delete(H, a['id'])
        self.assertFalse(attachments.file_path(a['storage_key']).exists())
        a = self.upload()
        self.link([a['id']])
        attachments.cleanup()
        self.assertTrue(attachments.file_path(a['storage_key']).exists())
        issues.delete_issue(H, self.issue['ref'])
        self.assertFalse(attachments.file_path(a['storage_key']).exists())

    def test_durable_delete_queue_and_orphans(self):
        a = self.upload()
        with patch.object(Path, 'unlink', side_effect=OSError('busy')):
            attachments.delete(H, a['id'])
        with db.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM attachment_gc').fetchone()[0], 1)
        self.assertTrue(attachments.file_path(a['storage_key']).exists())
        attachments.cleanup()
        self.assertFalse(attachments.file_path(a['storage_key']).exists())
        orphan = attachments.file_path('a' * 32)
        orphan.write_bytes(b'abandoned')
        os.utime(orphan, (0, 0))
        fresh = attachments.file_path('b' * 32)
        fresh.write_bytes(b'fresh')
        attachments.cleanup()
        self.assertFalse(orphan.exists())
        self.assertTrue(fresh.exists())

    def test_database_failure_preserves_files(self):
        a = self.upload()
        with patch.object(db, 'connect', side_effect=sqlite3.OperationalError('locked')):
            with self.assertRaises(sqlite3.Error):
                attachments.delete(H, a['id'])
        self.assertTrue(attachments.file_path(a['storage_key']).exists())
        self.link([a['id']])
        with patch.object(attachments, 'cleanup', side_effect=sqlite3.OperationalError('locked')):
            issues.delete_issue(H, self.issue['ref'])
        attachments.cleanup()
        self.assertFalse(attachments.file_path(a['storage_key']).exists())

    def test_project_deletion_cascades(self):
        a = self.upload()
        self.link([a['id']])
        state = issues.check_project_deletion(H, 'DEV')
        issues.delete_project(H, 'DEV', state['confirmation_token'])
        self.assertFalse(attachments.file_path(a['storage_key']).exists())
        with db.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM attachments').fetchone()[0], 0)

    def test_url_and_path_escape(self):
        a = attachments.add_url(H, 'https://example.com/path?q=1')
        self.link([a['id']])
        self.assertEqual(a['kind'], 'url')
        for url in ('javascript:alert(1)', 'file:///tmp/x', 'https://u:p@example.com',
                    'https://example.com:bad', 'https://example.com/\n', '//example.com'):
            with self.assertRaises(issues.StoreError):
                attachments.add_url(H, url)
        for key in ('../x', 'C:\\x', 'a' * 31, 'A' * 32):
            with self.assertRaises(issues.StoreError):
                attachments.file_path(key)
        a = self.upload()
        with patch.object(Path, 'is_symlink', return_value=True):
            with self.assertRaises(issues.StoreError):
                attachments.file_path(a['storage_key'])


class AttachmentAPITest(unittest.TestCase):
    setUp = AttachmentsTest.setUp

    def client(self):
        import app, auth
        from fastapi.testclient import TestClient
        from unittest.mock import AsyncMock
        self.addCleanup(patch.stopall)
        patch.object(auth, 'nightshift_user', AsyncMock(return_value={'id': 1, 'username': 'admin', 'role': 'admin'})).start()
        return TestClient(app.app)

    def test_api_lifecycle_and_atomic_publish(self):
        import app, auth
        client = self.client()
        headers = {'X-Requested-With': 'dev'}
        self.assertEqual(client.post('/api/attachments', files={'file': ('x.md', b'hello')}).status_code, 403)
        r = client.post('/api/attachments', headers=headers, files={'file': ('한글.md', b'hello world')})
        self.assertEqual(r.status_code, 200, r.text)
        a = r.json()
        self.assertNotIn('storage_key', a)
        url = client.post('/api/attachments/url', headers=headers, json={'url': 'https://example.com'}).json()
        body = {'project': 'DEV', 'title': 'with files', 'attachment_ids': [a['id'], 'missing']}
        self.assertEqual(client.post('/api/issues', headers=headers, json=body).status_code, 404)
        self.assertIsNone(attachments.get(H, a['id'])['issue_id'])
        original = attachments.link
        def inspect(c, actor, iid, ids):
            # Auto가 쓰는 별도 연결에서는 미완성 이슈를 볼 수 없다.
            with db.connect() as observer:
                self.assertIsNone(observer.execute('SELECT id FROM issues WHERE id=?', (iid,)).fetchone())
            return original(c, actor, iid, ids)
        body['attachment_ids'] = [a['id'], url['id']]
        with patch.object(attachments, 'link', side_effect=inspect):
            r = client.post('/api/issues', headers=headers, json=body)
        self.assertEqual(r.status_code, 200, r.text)
        full = issues.get_issue(r.json()['ref'])
        self.assertEqual(len(full['attachments']), 2)
        body['title'] = 'duplicate connection'
        self.assertEqual(client.post('/api/issues', headers=headers, json=body).status_code, 403)
        r = client.get(a['download_url'])
        self.assertEqual(r.content, b'hello world')
        self.assertEqual(r.headers['x-content-type-options'], 'nosniff')
        self.assertIn('filename*=UTF-8', r.headers['content-disposition'])
        for value, expected in [('bytes=1-3', b'ell'), ('bytes=-5', b'world'), ('bytes=6-', b'world'), ('bytes=0-999', b'hello world')]:
            r = client.get(a['download_url'], headers={'Range': value})
            self.assertEqual(r.status_code, 206)
            self.assertEqual(r.content, expected)
            self.assertEqual(int(r.headers['content-length']), len(expected))
        for value in ('bytes=99-', 'bytes=3-1', 'bytes=-0', 'bytes=0-1,3-4', 'nonsense'):
            self.assertEqual(client.get(a['download_url'], headers={'Range': value}).status_code, 416)
        agent, key = auth.create_agent('reader', '', '')
        bearer = {'Authorization': 'Bearer ' + key}
        self.assertEqual(client.get(a['download_url'], headers=bearer).status_code, 200)
        self.assertEqual(client.post('/api/attachments', headers=bearer, files={'file': ('x.md', b'x')}).status_code, 403)
        self.assertEqual(client.delete('/api/attachments/' + a['id'], headers=bearer).status_code, 403)
        with patch.object(auth, 'nightshift_user', return_value=None):
            self.assertEqual(client.get(a['download_url']).status_code, 401)
        self.assertEqual(client.delete('/api/attachments/' + a['id'], headers=headers).status_code, 204)
        self.assertEqual(client.get(a['download_url']).status_code, 404)

    def test_auto_registration_sees_all_attachments(self):
        import auto_settings, automation
        client = self.client()
        headers = {'X-Requested-With': 'dev'}
        auto_settings.update_settings(H, 'DEV', {'auto_review': True})
        with patch.object(automation, 'provider_available', return_value=True):
            automation.sync()
            with db.connect() as c:
                before = c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
            a = client.post('/api/attachments', headers=headers, files={'file': ('x.md', b'data')}).json()
            automation.sync()
            with db.connect() as c:
                self.assertEqual(c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], before)
            result = client.post('/api/issues', headers=headers, json={'project': 'DEV', 'title': 'auto', 'attachment_ids': [a['id']]}).json()
            original = automation.eligible
            def inspect(c, issue, mode, job=None):
                if issue['id'] == result['id']:
                    self.assertEqual([x['id'] for x in issue['attachments']], [a['id']])
                return original(c, issue, mode, job)
            with patch.object(automation, 'eligible', side_effect=inspect):
                automation.sync()
            automation.sync()
            with db.connect() as c:
                rows = c.execute('SELECT * FROM jobs WHERE issue_id=?', (result['id'],)).fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['source'], 'auto')

    def test_limits_formats_and_temporary_visibility(self):
        import auth
        client = self.client()
        headers = {'X-Requested-With': 'dev'}
        for name, data in [('x.svg', b'<svg/>'), ('x.json', b'{bad'), ('x.png', b'fake')]:
            self.assertEqual(client.post('/api/attachments', headers=headers, files={'file': (name, data)}).status_code, 400)
        with patch.object(config, 'ATTACHMENT_MAX_BYTES', 3):
            self.assertEqual(client.post('/api/attachments', headers=headers, files={'file': ('x.md', b'abcd')}).status_code, 413)
            self.assertEqual(client.post('/api/attachments', headers=headers, files={'file': ('x.md', b'a' * 70000)}).status_code, 413)
            # 길이 헤더 없는 스트림도 실제 수신 바이트를 기준으로 거부한다.
            stream_headers = {**headers, 'Content-Type': 'multipart/form-data; boundary=abc'}
            def stream():
                yield b'--abc\r\nContent-Disposition: form-data; name="file"; filename="x.md"\r\n\r\n'
                yield b'a' * 70000
                yield b'\r\n--abc--\r\n'
            self.assertEqual(client.post('/api/attachments', headers=stream_headers, content=stream()).status_code, 413)
        self.assertEqual(client.post('/api/attachments', headers=headers, files=[('file', ('x.md', b'x')), ('file', ('y.md', b'y'))]).status_code, 400)
        self.assertEqual(client.post('/api/attachments', headers=headers, content=b'bad').status_code, 400)
        self.assertEqual(client.post('/api/attachments', headers={**headers, 'Content-Type': 'multipart/form-data; boundary=abc'}, content=b'wrong').status_code, 400)
        a = client.post('/api/attachments', headers=headers, files={'file': ('x.md', b'x')}).json()
        _, key = auth.create_agent('reader', '', '')
        self.assertEqual(client.get(a['download_url'], headers={'Authorization': 'Bearer ' + key}).status_code, 403)
        with db.connect() as c:
            c.execute("UPDATE attachments SET expires_at='2000' WHERE id=?", (a['id'],))
        self.assertEqual(client.get(a['download_url']).status_code, 410)


if __name__ == '__main__':
    unittest.main()
