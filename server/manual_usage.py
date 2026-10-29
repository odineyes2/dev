"""대화형 Claude Code의 수신 전용 키와 메시지별 사용량을 저장한다."""
import hashlib
import secrets
from datetime import datetime

import config
import db
from issues import StoreError, _find

KEY_SETTING = 'manual_usage_key_hash'


def rotate_key():
    """키는 파일에만 쓰고 DB에는 해시만 남긴다. 원문을 반환하지 않는다."""
    key = 'mu_' + secrets.token_urlsafe(32)
    path = config.DATA_DIR / 'manual_usage.key'
    path.parent.mkdir(parents=True, exist_ok=True)
    # 임시 파일을 제한된 권한으로 만든 뒤 교체하여 부분 기록을 피한다.
    import os
    import tempfile
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.manual-usage-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(key)
        os.replace(temporary, path)
        with db.connect() as c:
            c.execute('''INSERT INTO app_settings(key,value,updated_at,updated_by)
                         VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                         value=excluded.value,updated_at=excluded.updated_at,updated_by=excluded.updated_by''',
                      (KEY_SETTING, hashlib.sha256(key.encode()).hexdigest(), db.now_iso(), 'system:manual_usage'))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def authorized(header):
    if not header.lower().startswith('bearer '):
        return False
    key = header[7:].strip()
    if not key.startswith('mu_'):
        return False
    with db.connect() as c:
        row = c.execute('SELECT value FROM app_settings WHERE key=?', (KEY_SETTING,)).fetchone()
    return bool(row and secrets.compare_digest(row['value'], hashlib.sha256(key.encode()).hexdigest()))


def _text(value, name, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise StoreError(f'{name} 값이 맞지 않아요.', 400)
    return value


def ingest(body):
    session = _text(body.get('session_id'), 'session_id', 256)
    items = body.get('items')
    if not isinstance(items, list) or not 1 <= len(items) <= 500:
        raise StoreError('items는 1~500건 목록이어야 해요.', 400)
    rows = []
    for item in items:
        if not isinstance(item, dict):
            raise StoreError('사용량은 객체여야 해요.', 400)
        message = _text(item.get('message_id'), 'message_id', 512)
        model = _text(item.get('model'), 'model', 256)
        ts = _text(item.get('ts'), 'ts', 64)
        try:
            if datetime.fromisoformat(ts.replace('Z', '+00:00')).utcoffset() is None:
                raise ValueError()
        except ValueError:
            raise StoreError('ts는 시간대가 있는 시각이어야 해요.', 400)
        tokens = [item.get(k) for k in ('input_tokens', 'output_tokens', 'cache_creation_tokens', 'cache_read_tokens')]
        if any(type(n) is not int or not 0 <= n <= 9223372036854775807 for n in tokens):
            raise StoreError('토큰은 음수가 아닌 정수여야 해요.', 400)
        ref = item.get('ref')
        if ref is not None:
            ref = _text(ref, 'ref', 128)
        rows.append((message, model, ts, tokens, ref))
    inserted = 0
    with db.connect() as c:
        for message, model, ts, tokens, ref in rows:
            issue_id = None
            if ref:
                try:
                    issue_id = _find(c, ref)['id']
                except StoreError as exc:
                    if exc.status != 404:
                        raise
            inserted += c.execute('''INSERT OR IGNORE INTO manual_usage
                (message_id,session_id,model,input_tokens,output_tokens,cache_creation_tokens,
                 cache_read_tokens,ts,issue_id,ref_text,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (message, session, model, *tokens, ts, issue_id, ref, db.now_iso())).rowcount
    return {'inserted': inserted}


# 스크립트 경로를 소유 표식으로 써서 다른 명령 훅은 건드리지 않는다.
def _settings_path(home=None):
    from pathlib import Path
    import os
    return Path(home or os.environ.get('DEV_MANUAL_USAGE_HOME') or Path.home()) / '.claude' / 'settings.json'


def _read_settings(path):
    import json
    try:
        data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        if not isinstance(data, dict) or not isinstance(data.get('hooks', {}), dict):
            raise ValueError()
        for event in ('Stop', 'SessionEnd'):
            groups = data.get('hooks', {}).get(event, [])
            if not isinstance(groups, list) or any(
                not isinstance(g, dict) or not isinstance(g.get('hooks'), list)
                or any(not isinstance(h, dict) for h in g['hooks']) for g in groups
            ):
                raise ValueError()
        return data
    except (ValueError, UnicodeError):
        raise StoreError('Claude Code 설정 JSON을 확인해 주세요. 파일은 바꾸지 않았어요.', 409)


def _ours(hook):
    script = str(config.REPO_ROOT / 'scripts' / 'claude_usage_hook.py')
    command = hook.get('command')
    return hook.get('type') == 'command' and isinstance(command, str) and script in command


def connection_status(home=None):
    data = _read_settings(_settings_path(home))
    connected = all(any(_ours(h) for g in data.get('hooks', {}).get(event, [])
                        for h in g['hooks']) for event in ('Stop', 'SessionEnd'))
    with db.connect() as c:
        last = c.execute('SELECT MAX(created_at) FROM manual_usage').fetchone()[0]
        key = c.execute('SELECT 1 FROM app_settings WHERE key=?', (KEY_SETTING,)).fetchone()
    return {'connected': bool(connected and key and (config.DATA_DIR / 'manual_usage.key').exists()),
            'last_received_at': last}


def set_connection(enabled, home=None):
    import json
    import os
    import shutil
    import subprocess
    import sys
    import tempfile
    path = _settings_path(home)
    data = _read_settings(path)
    hooks = data.setdefault('hooks', {})
    command = subprocess.list2cmdline([sys.executable, str(config.REPO_ROOT / 'scripts' / 'claude_usage_hook.py'),
                                      '--url', f'http://127.0.0.1:{config.PORT}/api/manual-usage/ingest',
                                      '--key-file', str(config.DATA_DIR.resolve() / 'manual_usage.key')])
    if os.name != 'nt':
        import shlex
        command = shlex.join([sys.executable, str(config.REPO_ROOT / 'scripts' / 'claude_usage_hook.py'),
                              '--url', f'http://127.0.0.1:{config.PORT}/api/manual-usage/ingest',
                              '--key-file', str(config.DATA_DIR.resolve() / 'manual_usage.key')])
    for event in ('Stop', 'SessionEnd'):
        groups = []
        for group in hooks.get(event, []):
            remaining = [h for h in group['hooks'] if not _ours(h)]
            if remaining or not group['hooks']:
                groups.append({**group, 'hooks': remaining})
        if enabled:
            groups.append({'hooks': [{'type': 'command', 'command': command, 'timeout': 2}]})
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + '.bak'))
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.dev-usage-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        if enabled:
            rotate_key()
        os.replace(temporary, path)
        if not enabled:
            with db.connect() as c:
                c.execute('DELETE FROM app_settings WHERE key=?', (KEY_SETTING,))
            (config.DATA_DIR / 'manual_usage.key').unlink(missing_ok=True)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return connection_status(home)
