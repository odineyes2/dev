"""복사된 분리형 앱의 HTTP 응답과 선택적 DB 마이그레이션을 검사한다."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fastapi.testclient import TestClient
from backend.serve import app


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
                from backend import db
                db.initialize()
                self.assertEqual(client.get('/api/ready').json()['db'], db.status())
                if os.environ.get('DATABASE_URL'):
                    with db.connect() as connection:
                        self.assertEqual(connection.execute("SELECT name FROM schema_migrations WHERE name='001_core.sql'").fetchone()[0], '001_core.sql')
                        connection.execute('SELECT key, value FROM app_settings LIMIT 0')
                else:
                    self.assertEqual(db.status(), 'not_configured')
                # [/option:db]


if __name__ == '__main__':
    unittest.main()
