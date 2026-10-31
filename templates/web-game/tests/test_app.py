"""복사된 게임의 HTTP 응답·점수 검증·저장·정렬을 검사한다."""
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
            with TestClient(app) as c:
                self.assertTrue(c.get('/api/health').json()['ok'])
                self.assertIn('<canvas', c.get('/').text)
                for path in ('/.env', '/.git/config', '/PROJECT_SPEC.md', '/server/db.py', '/api/me'):
                    self.assertEqual(c.get(path).status_code, 404)
                # [option:scores]
                self.assertEqual(c.get('/api/scores').json(), [])
                for value in ({'name':'','score':0}, {'name':'  ','score':0}, {'name':'x'*21,'score':0}, {'name':'a','score':-1}, {'name':'a','score':1000001}, {'name':'a','score':1.5}, {'name':'a','score':True}, {'name':'a','score':'1'}):
                    self.assertEqual(c.post('/api/scores', json=value).status_code, 422)
                for score in range(12):
                    self.assertEqual(c.post('/api/scores', json={'name':'플레이어','score':score}).status_code, 200)
                self.assertEqual([r['score'] for r in c.get('/api/scores').json()], list(range(11,1,-1)))
                import db
                db.initialize()
                self.assertEqual(len(db.top_scores()), 10)
                self.assertEqual(c.post('/api/scores', json={'name':'경계','score':1000000}).status_code, 200)
                self.assertEqual(c.get('/api/scores').json()[0], {'name':'경계','score':1000000})
                # [/option:scores]
                if '/api/scores' not in [r.path for r in app.routes]:
                    self.assertEqual(c.get('/api/scores').status_code, 404)
                    self.assertEqual(c.post('/api/scores', json={'name':'a','score':1}).status_code, 404)

if __name__ == '__main__':
    unittest.main()
