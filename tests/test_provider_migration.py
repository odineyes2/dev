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
assert db.init() == len(db.MIGRATIONS)
with db.connect() as conn:
    for table in ('jobs', 'runs'):
        col = next(r for r in conn.execute('pragma table_info(' + table + ')') if r['name'] == 'provider')
        assert col['notnull'] and col['dflt_value'] == "'claude'"
assert db.init() == len(db.MIGRATIONS)
print('OK')

config.DB_PATH = Path(os.environ['DEV_DATA_DIR']) / 'legacy-role.db'
with sqlite3.connect(config.DB_PATH) as c:
    role_version = next(i for i, sql in enumerate(db.MIGRATIONS) if 'CREATE TABLE project_auto_role_settings' in sql)
    for i, sql in enumerate(db.MIGRATIONS[:role_version], 1):
        c.executescript('BEGIN;' + sql + f'PRAGMA user_version={i};COMMIT;')
    c.execute("INSERT INTO projects(id,key,name,created_at) VALUES(1,'DEV','dev','old')")
    c.execute("INSERT INTO issues(id,project_id,number,title,reporter,created_at,updated_at) VALUES(1,1,1,'old','human:admin','old','old')")
    c.execute("INSERT INTO project_auto_settings VALUES(1,1,1,0,'[\"codex\",\"claude\"]','human:admin','old',1)")
    c.execute("INSERT INTO project_auto_settings_events VALUES(1,1,'human:admin','{}','{\"provider_order\":[\"codex\",\"claude\"]}','old',0)")
    c.execute("INSERT INTO jobs(id,issue_id,mode,actor,status,created_at,source,delegation_id) VALUES(1,1,'review','human:admin','started','old','auto',1)")
    c.execute("INSERT INTO jobs(id,issue_id,mode,actor,status,created_at) VALUES(2,1,'review','human:admin','cancelled','old')")
    c.execute("INSERT INTO runs(id,issue_id,mode,status,actor,started_at) VALUES(1,1,'review','failed','human:admin','old')")
    tables = ('project_auto_settings', 'project_auto_settings_events', 'jobs', 'runs')
    old = {t: c.execute('SELECT * FROM ' + t).fetchall() for t in tables}
assert db.init() == len(db.MIGRATIONS)
with db.connect() as c:
    for t in tables:
        assert [tuple(r)[:len(old[t][0])] for r in c.execute('SELECT * FROM ' + t)] == old[t]
    settings = c.execute('SELECT * FROM project_auto_role_settings').fetchone()
    assert settings['token_exhaustion_fallback'] == 0
    assert settings['orchestrator_provider_order_json'] == '["claude","codex"]'
    assert settings['troubleshooter_provider_order_json'] == '["claude","codex"]'
    assert c.execute('SELECT * FROM provider_run_failures').fetchall() == []
    c.execute("INSERT INTO provider_run_failures VALUES(1,'token_exhaustion')")
    c.execute("INSERT INTO provider_migration_chains VALUES(1,1,1,'now')")
    c.execute("INSERT INTO provider_migration_attempts(chain_id,provider,job_id,run_id,created_at) VALUES(1,'claude',1,1,'now')")
    for sql in (
        "INSERT INTO provider_migration_chains VALUES(2,1,1,'now')",
        "INSERT INTO provider_migration_chains VALUES(2,2,1,'now')",
        "INSERT INTO provider_migration_attempts(chain_id,provider,created_at) VALUES(1,'claude','now')",
        "INSERT INTO provider_migration_attempts(chain_id,provider,created_at) VALUES(1,'other','now')",
        "UPDATE project_auto_role_settings SET token_exhaustion_fallback=2",
        "UPDATE project_auto_role_settings SET orchestrator_provider_order_json='[]'",
        "UPDATE project_auto_role_settings SET troubleshooter_provider_order_json='[]'",
    ):
        try:
            c.execute(sql)
            raise AssertionError(sql)
        except sqlite3.IntegrityError:
            pass
    c.execute("INSERT INTO provider_migration_attempts(chain_id,provider,previous_run_id,created_at) VALUES(1,'codex',1,'now')")
    assert c.execute('PRAGMA foreign_key_check').fetchall() == []
assert db.init() == len(db.MIGRATIONS)
print('OK — 기존 데이터 보존·이관 기본 OFF·Auto 출처·중복 제약')
