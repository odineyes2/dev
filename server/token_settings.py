"""전역 토큰 표시 단위 설정."""

import db
from issues import StoreError, actor_label

KEY = 'token_unit'
UNITS = {'auto', 'k', 'M', 'full'}


def get_settings():
    with db.connect() as conn:
        row = conn.execute('SELECT value FROM app_settings WHERE key=?', (KEY,)).fetchone()
    return {'unit': row['value'] if row and row['value'] in UNITS else 'auto'}


def update_settings(actor, body):
    if actor['kind'] != 'human':
        raise StoreError('관리자만 설정을 바꿀 수 있어요.', 403)
    if (not isinstance(body, dict) or set(body) != {'unit'}
            or not isinstance(body['unit'], str) or body['unit'] not in UNITS):
        raise StoreError('토큰 단위는 auto, k, M, full 중 하나여야 해요.', 400)
    with db.connect() as conn:
        conn.execute('''INSERT INTO app_settings(key,value,updated_at,updated_by)
                        VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                        value=excluded.value,updated_at=excluded.updated_at,updated_by=excluded.updated_by''',
                     (KEY, body['unit'], db.now_iso(), actor_label(actor)))
    return {'unit': body['unit']}
