"""복사된 관리 앱의 실제 HTTP 응답과 선택 기능을 검사한다."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from fastapi.testclient import TestClient
from app import app


class AppTest(unittest.TestCase):
    def test_http(self):
        with tempfile.TemporaryDirectory() as folder:
            os.environ['DATA_DIR'] = folder
            with TestClient(app) as client:
                self.assertEqual(client.get('/api/health').json()['stage'], 'starter')
                self.assertIn('<html', client.get('/').text)
                for path in ('/.env', '/.git/config', '/PROJECT_SPEC.md'):
                    self.assertEqual(client.get(path).status_code, 404)
                # [option:login]
                self.assertEqual(client.get('/api/me').status_code, 401)
                # [/option:login]
                # [option:db]
                import db
                db.initialize()
                connection = db.connect()
                try:
                    self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], len(db.MIGRATIONS))
                finally:
                    connection.close()
                # [/option:db]


if __name__ == '__main__':
    unittest.main()
