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
