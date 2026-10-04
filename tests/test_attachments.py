"""첨부 저장·마이그레이션·권한·실패 복구를 임시 DATA_DIR에서 검사한다."""
import os
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


if __name__ == '__main__':
    unittest.main()
