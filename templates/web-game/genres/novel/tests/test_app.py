"""복사된 노벨의 실제 HTTP 화면·정적 파일·점수 API 부재를 검사한다."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from fastapi.testclient import TestClient
from app import app

class AppTest(unittest.TestCase):
    def test_http(self):
        with TestClient(app) as client:
            self.assertTrue(client.get('/api/health').json()['ok'])
            response = client.get('/')
            self.assertEqual(response.status_code, 200)
            for marker in ('id="game"', 'id="choices"', 'id="save"', 'id="load"'):
                self.assertIn(marker, response.text)
            for name in ('app.js', 'game.js', 'genre.css'):
                self.assertEqual(client.get('/static/' + name).status_code, 200)
            for path in ('/.env', '/.git/config', '/PROJECT_SPEC.md', '/server/db.py', '/api/me', '/api/scores'):
                self.assertEqual(client.get(path).status_code, 404)
            self.assertEqual(client.post('/api/scores', json={'name':'a', 'score':1}).status_code, 404)

if __name__ == '__main__':
    unittest.main()
