"""기존 v5 DB의 데이터와 대기 항목을 보존하며 provider 컬럼을 추가한다."""
import os, sys, tempfile, sqlite3
from pathlib import Path

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))
import db, config

conn = sqlite3.connect(config.DB_PATH)
for i, sql in enumerate(db.MIGRATIONS[:5], 1):
    conn.executescript('BEGIN;' + sql + f'PRAGMA user_version={i};COMMIT;')
assert 'provider' not in [r[1] for r in conn.execute('pragma table_info(jobs)')]
conn.close()
assert db.init() == 6
with db.connect() as conn:
    for table in ('jobs', 'runs'):
        col = next(r for r in conn.execute('pragma table_info(' + table + ')') if r['name'] == 'provider')
        assert col['notnull'] and col['dflt_value'] == "'claude'"
assert db.init() == 6
print('OK')
