"""DATABASE_URL이 있을 때 PostgreSQL 연결과 순차 마이그레이션을 관리한다."""
import os
from pathlib import Path


def connect():
    import psycopg
    return psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=5)


def initialize():
    if not os.environ.get('DATABASE_URL'):
        return
    with connect() as connection:
        # API·워커 동시 시작에서도 마이그레이션은 한 번만 적용한다.
        connection.execute('SELECT pg_advisory_xact_lock(210002)')
        connection.execute('CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)')
        for path in sorted((Path(__file__).parent / 'migrations').glob('*.sql')):
            if not connection.execute('SELECT 1 FROM schema_migrations WHERE name=%s', (path.name,)).fetchone():
                connection.execute(path.read_text(encoding='utf-8'))
                connection.execute('INSERT INTO schema_migrations (name) VALUES (%s)', (path.name,))


def status():
    if not os.environ.get('DATABASE_URL'):
        return 'not_configured'
    with connect() as connection:
        connection.execute('SELECT 1')
    return 'ready'
