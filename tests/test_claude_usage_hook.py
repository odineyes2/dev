"""표본 기록과 로컬 수신기로 훅의 중복 제거·연결·재시도·읽기 위치를 검사한다."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'claude_usage_hook.py'
spec = importlib.util.spec_from_file_location('claude_usage_hook', SCRIPT)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


def assistant(identity, second, content=None, output=2):
    return {'type': 'assistant', 'timestamp': f'2026-10-29T12:00:{second:02d}Z',
            'requestId': 'request-' + identity,
            'message': {'id': identity, 'model': 'claude-opus-4-6',
                        'content': content or [{'type': 'text', 'text': 'PRIVATE BODY'}],
                        'usage': {'input_tokens': 10, 'output_tokens': output,
                                  'cache_creation_input_tokens': 3, 'cache_read_input_tokens': 4}}}


class HookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.transcript = self.folder / 'session.jsonl'
        self.state = self.folder / 'state.json'
        self.key = self.folder / 'receiver.key'
        self.key.write_text('test-receiver-credential', encoding='utf-8')
        self.event = {'session_id': 'session', 'transcript_path': str(self.transcript)}
        self.received = []
        received = self.received

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                received.append((self.headers.get('Authorization'), body))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"inserted":1}')

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.url = f'http://127.0.0.1:{self.server.server_port}/api/manual-usage/ingest'

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def append(self, *records, path=None):
        with (path or self.transcript).open('ab') as stream:
            for record in records:
                stream.write(json.dumps(record).encode() + b'\n')

    def run_hook(self, url=None):
        return subprocess.run([sys.executable, str(SCRIPT), '--url', url or self.url,
                               '--key-file', str(self.key), '--state-file', str(self.state)],
                              input=json.dumps(self.event), text=True, capture_output=True, timeout=5)

    def test_duplicates_refs_subagents_and_incremental(self):
        tool = {'type': 'tool_use', 'id': 'claim', 'name': 'mcp__dev__claim_issue',
                'input': {'ref': 'DEV-189-2'}}
        create = {'type': 'tool_use', 'id': 'create', 'name': 'mcp__claude_ai_dev__create_issue',
                  'input': {'title': 'PRIVATE TITLE'}}
        self.append(assistant('before', 0), assistant('claim-message', 1, [tool]),
                    assistant('claim-message', 1, [tool], output=7), assistant('create-message', 3, [create]),
                    {'type': 'user', 'timestamp': '2026-10-29T12:00:04Z', 'message': {'content': [
                        {'type': 'tool_result', 'tool_use_id': 'create', 'content': [
                            {'type': 'text', 'text': json.dumps({'ref': 'MO-89'})}]}]}},
                    assistant('after', 5))
        subfolder = self.folder / 'session' / 'subagents'
        subfolder.mkdir(parents=True)
        self.append(assistant('sub-message', 2), path=subfolder / 'agent-one.jsonl')
        result = self.run_hook()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        authorization, body = self.received[0]
        self.assertEqual(authorization, 'Bearer test-receiver-credential')
        items = {item['message_id']: item for item in body['items']}
        self.assertEqual(len(items), 5)
        self.assertIsNone(items['before']['ref'])
        self.assertEqual(items['claim-message']['output_tokens'], 7)
        self.assertEqual(items['sub-message']['ref'], 'DEV-189-2')
        self.assertEqual(items['after']['ref'], 'MO-89')
        self.assertEqual(sum(i['input_tokens'] + i['output_tokens'] for i in items.values()), 65)
        self.assertEqual(items['after']['cache_creation_tokens'], 3)
        self.assertEqual(items['after']['cache_read_tokens'], 4)
        self.assertNotIn('PRIVATE', json.dumps(body))
        self.run_hook()
        self.assertEqual(len(self.received), 1)
        self.append(assistant('after', 5), assistant('new', 6))
        self.run_hook()
        self.assertEqual([i['message_id'] for i in self.received[1][1]['items']], ['new'])
        self.assertEqual(self.received[1][1]['items'][0]['ref'], 'MO-89')

    def test_failure_exit_zero_and_state_unchanged(self):
        self.append(assistant('first', 0))
        self.run_hook()
        saved = self.state.read_bytes()
        self.append(assistant('second', 1))
        # 닫힌 로컬 포트만 사용하며 운영 서비스나 유료 호출을 하지 않는다.
        result = self.run_hook('http://127.0.0.1:0/api/manual-usage/ingest')
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        self.assertEqual(self.state.read_bytes(), saved)
        self.run_hook()
        self.assertEqual(self.received[-1][1]['items'][0]['message_id'], 'second')

    def test_partial_line_and_pending_create_result(self):
        create = {'type': 'tool_use', 'id': 'pending', 'name': 'mcp__dev__create_issue', 'input': {}}
        self.append(assistant('create', 0, [create]))
        self.run_hook()
        record = {'type': 'user', 'timestamp': '2026-10-29T12:00:01Z', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 'pending', 'content': '{"ref":"NS-17-1"}'}]}}
        raw = json.dumps(record).encode()
        with self.transcript.open('ab') as stream:
            stream.write(raw[:20])
        self.run_hook()
        state = json.loads(self.state.read_text())
        self.assertEqual(state['session']['files'][str(self.transcript.resolve())],
                         self.transcript.stat().st_size - 20)
        with self.transcript.open('ab') as stream:
            stream.write(raw[20:] + b'\n')
        self.append(assistant('linked', 2))
        self.run_hook()
        self.assertEqual(self.received[-1][1]['items'][0]['ref'], 'NS-17-1')

    def test_batches_retry_and_lock(self):
        self.append(*(assistant(str(n), 0) for n in range(501)))
        calls = []

        class Response:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        def send(request, timeout):
            calls.append(json.loads(request.data))
            self.assertLessEqual(timeout, 1.5)
            if len(calls) == 2:
                raise OSError('offline')
            return Response()

        with patch.object(hook, 'urlopen', side_effect=send):
            with self.assertRaises(OSError):
                hook.run(self.event, self.url, self.key, self.state)
        self.assertFalse(self.state.exists())
        self.assertEqual([len(c['items']) for c in calls], [500, 1])
        self.state.with_suffix('.lock').touch()
        hook.run(self.event, self.url, self.key, self.state)
        self.assertFalse(self.state.exists())

    def test_malformed_input_is_silent(self):
        with patch.object(sys, 'argv', ['hook', '--url', self.url, '--key-file', str(self.key)]), \
                patch.object(sys, 'stdin', __import__('io').StringIO('broken')):
            self.assertEqual(hook.main(), 0)

    def test_split_ref_non_dev_tools_and_deadline(self):
        self.append(assistant('split', 0), assistant('split', 1, [
            {'type': 'tool_use', 'id': 'dev', 'name': 'mcp__dev__get_issue',
             'input': {'ref': 'DEV-189-2'}}], output=8),
            assistant('other', 2, [{'type': 'tool_use', 'id': 'external',
                                    'name': 'mcp__other__get_issue', 'input': {'ref': 'MO-1'}}]))
        with self.transcript.open('ab') as stream:
            stream.write(b'not-json\n')
        with self.assertRaises(TimeoutError):
            hook.collect(self.event, {}, 0)
        self.run_hook()
        items = self.received[0][1]['items']
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]['output_tokens'], 8)
        self.assertTrue(all(item['ref'] == 'DEV-189-2' for item in items))
        # 기록 파일이 축소되어도 기존 message id는 다시 보내지 않는다.
        self.transcript.write_bytes(b'')
        self.append(assistant('split', 0), assistant('fresh', 3))
        self.run_hook()
        self.assertEqual([i['message_id'] for i in self.received[-1][1]['items']], ['fresh'])


if __name__ == '__main__':
    unittest.main()
