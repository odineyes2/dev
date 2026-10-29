"""Claude Code 기록의 새 사용량만 dev로 보내고 성공한 읽기 위치를 기억한다."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.request import Request, urlopen


def result_ref(value):
    """도구 결과의 JSON과 MCP text 블록에서 ref만 읽는다."""
    if isinstance(value, str):
        try:
            return result_ref(json.loads(value))
        except ValueError:
            return None
    if isinstance(value, list):
        return next((ref for part in value if (ref := result_ref(part))), None)
    if isinstance(value, dict):
        ref = value.get('ref')
        if isinstance(ref, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9]*-\d+(?:-\d+)*', ref):
            return ref
        for key in ('content', 'text', 'structuredContent'):
            if ref := result_ref(value.get(key)):
                return ref
    return None


def collect(event, state, deadline):
    session = event['session_id']
    transcript = Path(event['transcript_path']).resolve()
    current = state.setdefault(session, {'files': {}, 'seen': [], 'ref': None, 'calls': {}})
    paths = [transcript]
    folder = transcript.parent / transcript.stem / 'subagents'
    if folder.is_dir():
        paths.extend(sorted(folder.rglob('*.jsonl')))
    records = []
    for path in paths:
        if time.monotonic() >= deadline:
            raise TimeoutError()
        name = str(path)
        offset = current['files'].get(name, 0)
        with path.open('rb') as stream:
            if path.stat().st_size < offset:
                offset = 0
            stream.seek(offset)
            while line := stream.readline():
                if time.monotonic() >= deadline:
                    raise TimeoutError()
                if not line.endswith(b'\n'):
                    break
                offset = stream.tell()
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(record, dict):
                    records.append(record)
        current['files'][name] = offset
    records.sort(key=lambda row: str(row.get('timestamp', '')))
    seen = set(current['seen'])
    items = {}
    for row in records:
        message = row.get('message', {})
        if not isinstance(message, dict):
            continue
        content = message.get('content', [])
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get('type') == 'tool_use':
                name = block.get('name', '')
                if name.startswith(('mcp__dev__', 'mcp__claude_ai_dev__')):
                    if ref := result_ref(block.get('input')):
                        current['ref'] = ref
                    if name.endswith('__create_issue'):
                        current['calls'][block['id']] = True
            elif block.get('type') == 'tool_result' and block.get('tool_use_id') in current['calls']:
                if not block.get('is_error') and (ref := result_ref(block.get('content'))):
                    current['ref'] = ref
                current['calls'].pop(block['tool_use_id'], None)
        if row.get('type') != 'assistant':
            continue
        usage = message.get('usage')
        identity = message.get('id') or row.get('requestId')
        if not identity or identity in seen or not isinstance(usage, dict):
            continue
        item = {'message_id': identity, 'model': message.get('model'),
                'ts': row.get('timestamp'), 'ref': current['ref']}
        for target, source in (('input_tokens', 'input_tokens'), ('output_tokens', 'output_tokens'),
                               ('cache_creation_tokens', 'cache_creation_input_tokens'),
                               ('cache_read_tokens', 'cache_read_input_tokens')):
            item[target] = usage.get(source, 0)
        # 분할 블록은 마지막 usage와 그 시점까지 다룬 ref를 쓴다.
        items[identity] = item
    current['seen'] = sorted(seen | items.keys())
    return list(items.values())


def run(event, url, key_file, state_path):
    deadline = time.monotonic() + 1.5
    state_path = Path(state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock = state_path.with_suffix('.lock')
    # Stop과 SessionEnd가 겹치면 한쪽만 처리하고 다음 호출에서 이어 읽는다.
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if time.time() - lock.stat().st_mtime > 30:
            lock.unlink(missing_ok=True)
        return
    temporary = None
    try:
        os.close(descriptor)
        state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
        items = collect(event, state, deadline)
        if items:
            key = Path(key_file).read_text(encoding='utf-8').strip()
            for start in range(0, len(items), 500):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                body = json.dumps({'session_id': event['session_id'], 'items': items[start:start + 500]}).encode()
                request = Request(url, data=body, headers={'Authorization': 'Bearer ' + key,
                                                         'Content-Type': 'application/json'})
                with urlopen(request, timeout=remaining) as response:
                    if not 200 <= response.status < 300:
                        raise OSError('수신 실패')
        fd, temporary = tempfile.mkstemp(dir=state_path.parent, prefix='.dev-usage-')
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(state, stream)
        os.replace(temporary, state_path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
        lock.unlink(missing_ok=True)


def main():
    try:
        class QuietParser(argparse.ArgumentParser):
            def error(self, message):
                raise ValueError(message)

        parser = QuietParser(add_help=False)
        parser.add_argument('--url', required=True)
        parser.add_argument('--key-file', required=True)
        parser.add_argument('--state-file', default=str(Path.home() / '.claude' / 'dev-usage-state.json'))
        args = parser.parse_args()
        run(json.load(sys.stdin), args.url, args.key_file, args.state_file)
    except (Exception, SystemExit):
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
