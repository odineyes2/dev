"""DB 스키마 — 새 파일에 마이그레이션이 걸리고, 두 번 불러도 그대로이며, CHECK/UNIQUE 제약이 막는다."""
import os, sqlite3, sys, tempfile
from pathlib import Path

os.environ["DEV_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import db  # noqa: E402

assert db.init() == len(db.MIGRATIONS)
assert db.init() == len(db.MIGRATIONS)   # 다시 불러도 그대로
with db.connect() as c:
    assert [r['name'] for r in c.execute('SELECT * FROM issue_types ORDER BY id')] == ['버그 수정', '문서 생성', 'UI/UX', '기능 추가']

now = db.now_iso()
with db.connect() as c:
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"projects", "issues", "issue_deps", "plans", "events", "agents"} <= tables, tables
    pid = c.execute("INSERT INTO projects(key, name, created_at) VALUES('NS', 'nightshift', ?)", (now,)).lastrowid
    iid = c.execute("INSERT INTO issues(project_id, number, title, reporter, created_at, updated_at) VALUES(?, 1, 't', 'human:admin', ?, ?)",
                    (pid, now, now)).lastrowid


def rejected(sql, args=()):
    try:
        with db.connect() as c:
            c.execute(sql, args)
    except sqlite3.IntegrityError:
        return True
    return False


assert rejected("UPDATE issues SET status='whatever' WHERE id=?", (iid,))
assert rejected("UPDATE issues SET priority='p0' WHERE id=?", (iid,))
assert rejected("INSERT INTO projects(key, name, created_at) VALUES('ns', 'x', ?)", (now,))          # 소문자 키
assert rejected("INSERT INTO issues(project_id, number, title, reporter, created_at, updated_at) VALUES(?, 1, 'dup', 'h', ?, ?)",
                (pid, now, now))                                                                   # 같은 번호
assert rejected("INSERT INTO issues(project_id, number, title, reporter, created_at, updated_at) VALUES(999, 2, 'x', 'h', ?, ?)",
                (now, now))                                                                        # 없는 프로젝트(FK)
assert rejected("INSERT INTO issue_deps VALUES(?, ?)", (iid, iid))
assert rejected("INSERT INTO events(issue_id, actor, kind, created_at) VALUES(?, 'h', 'nope', ?)", (iid, now))
with db.connect() as c:
    c.execute("INSERT INTO events(issue_id, actor, kind, created_at) VALUES(?, 'h', 'comment', ?)", (iid, now))
    c.execute("DELETE FROM issues WHERE id=?", (iid,))
    assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0   # 이슈를 지우면 타임라인도
# runs(DEV-22) — mode·status 제약, 서버가 다시 뜰 때 running은 orphaned로
with db.connect() as c:
    iid = c.execute("INSERT INTO issues(project_id, number, title, reporter, created_at, updated_at) VALUES(?, 2, 't', 'human:admin', ?, ?)",
                    (pid, now, now)).lastrowid
assert rejected("INSERT INTO runs(issue_id, mode, status, actor, started_at) VALUES(?, 'x', 'running', 'h', ?)", (iid, now))
assert rejected("INSERT INTO runs(issue_id, mode, status, actor, started_at) VALUES(?, 'review', 'nope', 'h', ?)", (iid, now))
with db.connect() as c:
    c.execute("INSERT INTO runs(issue_id, mode, status, actor, started_at) VALUES(?, 'review', 'running', 'h', ?)", (iid, now))
    c.execute("INSERT INTO runs(issue_id, mode, status, actor, started_at) VALUES(?, 'review', 'ok', 'h', ?)", (iid, now))
db.init()
with db.connect() as c:
    assert [r[0] for r in c.execute("SELECT status FROM runs ORDER BY id")] == ["orphaned", "ok"]
    c.execute("DELETE FROM issues WHERE id=?", (iid,))
    assert c.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0   # 이슈를 지우면 기록도
# 이전 스키마를 기록이 있는 상태로 확장해 참조·번호·인덱스 보존을 검증한다.
original_path = db.config.DB_PATH
db.config.DB_PATH = Path(tempfile.mkdtemp()) / 'legacy.db'
with sqlite3.connect(db.config.DB_PATH) as c:
    # Waiting·프로젝트 설명 도입 전 스키마를 고정한다(새 마이그레이션 추가와 독립적).
    legacy_version = next(i for i, sql in enumerate(db.MIGRATIONS) if 'CREATE TABLE issues_new' in sql)
    for version, sql in enumerate(db.MIGRATIONS[:legacy_version], 1):
        c.executescript(sql + f'PRAGMA user_version={version};')
    c.execute("INSERT INTO projects VALUES(1,'OLD','old','','',3,0,?)", (now,))
    c.execute("INSERT INTO agents VALUES(1,'a','','','hash','prefix',1,?,NULL)", (now,))
    c.execute("INSERT INTO issues(id,project_id,number,title,reporter,created_at,updated_at,assignee_agent_id) VALUES(1,1,1,'parent','human:admin',?,?,1)", (now, now))
    c.execute("INSERT INTO issues(id,project_id,number,parent_id,title,reporter,created_at,updated_at,sub_of,sub_number) VALUES(2,1,-2,1,'task','human:admin',?,?,1,1)", (now, now))
    c.execute("INSERT INTO issue_deps VALUES(2,1)")
    c.execute("INSERT INTO plans VALUES(1,1,1,'plan','human:admin',?)", (now,))
    c.execute("INSERT INTO decisions VALUES(1,1,'plan',1,'approve','','human:admin',?)", (now,))
    c.execute("INSERT INTO events(issue_id,actor,kind,created_at) VALUES(2,'human:admin','comment',?)", (now,))
    c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at) VALUES(2,'execute','ok','human:admin',?)", (now,))
    c.execute("INSERT INTO jobs(issue_id,mode,status,actor,created_at) VALUES(2,'execute','queued','human:admin',?)", (now,))
    tables = ('projects','agents','issues','issue_deps','plans','decisions','events','runs','jobs')
    before = {t: c.execute(f'SELECT * FROM {t}').fetchall() for t in tables}
assert db.init() == len(db.MIGRATIONS)
assert db.init() == len(db.MIGRATIONS)
with db.connect() as c:
    for table in tables:
        after = [tuple(r) for r in c.execute(f'SELECT * FROM {table}')]
        added = {'jobs': (None, 'manual', None, None), 'projects': ('',)}.get(table, ())
        assert after == [r + added for r in before[table]], table
    job = c.execute('SELECT * FROM jobs WHERE issue_id=2').fetchone()
    assert job['source'] == 'manual'
    assert job['delegation_id'] is None and job['approval_version'] is None
    assert c.execute('PRAGMA foreign_key_check').fetchall() == []
    assert c.execute('SELECT COUNT(*) FROM issue_types').fetchone()[0] == 4
    assert c.execute('SELECT COUNT(*) FROM issue_type_links').fetchone()[0] == 0
    assert {'issues_status','issues_parent','issues_sub'} <= {r[1] for r in c.execute('PRAGMA index_list(issues)')}
    c.execute("UPDATE issues SET status='waiting' WHERE id=2")
    assert c.execute("SELECT id FROM issues WHERE status='waiting'").fetchone()[0] == 2
assert rejected("UPDATE issues SET status='invalid' WHERE id=2")
with db.connect() as c:
    c.execute('DELETE FROM issues WHERE id=1')
    assert c.execute('SELECT parent_id FROM issues WHERE id=2').fetchone()[0] is None
    assert not c.execute('SELECT * FROM issue_deps').fetchall()
db.config.DB_PATH = original_path

# Auto 계획 승인 직전 판에서 실제 설정·위임·참조와 실행 게이트를 보존한다.
db.config.DB_PATH = Path(tempfile.mkdtemp()) / 'auto-legacy.db'
with sqlite3.connect(db.config.DB_PATH) as c:
    # Auto 계획 승인 도입 직전으로 고정하여 이후 마이그레이션 추가에도 보존 검사를 유지한다.
    auto_legacy_version = next(i for i, sql in enumerate(db.MIGRATIONS)
                               if 'CREATE TABLE review_plan_runs' in sql)
    for version, sql in enumerate(db.MIGRATIONS[:auto_legacy_version], 1):
        c.executescript(sql + f'PRAGMA user_version={version};')
    c.execute("INSERT INTO projects(key,name,created_at) VALUES('AUTO','자동',?)", (now,))
    c.execute("INSERT INTO issues(project_id,number,title,reporter,created_at,updated_at) VALUES(1,1,'이슈','human:admin',?,?)", (now, now))
    c.execute("INSERT INTO project_auto_settings VALUES(1,1,1,0,'[\"codex\",\"claude\"]','human:admin',?)", (now,))
    c.execute("INSERT INTO project_auto_settings_events VALUES(7,1,'human:admin','{}','{}',?)", (now,))
    c.execute("INSERT INTO jobs(issue_id,mode,status,actor,created_at,source,delegation_id) VALUES(1,'review','cancelled','human:auto/delegation/7',?,'auto',7)", (now,))
    settings_before = c.execute('SELECT * FROM project_auto_settings').fetchall()
    event_before = c.execute('SELECT * FROM project_auto_settings_events').fetchall()
    job_before = c.execute('SELECT * FROM jobs').fetchall()
    trigger_before = c.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='auto_run_gate'").fetchone()[0]
assert db.init() == len(db.MIGRATIONS)
assert db.init() == len(db.MIGRATIONS)
with db.connect() as c:
    assert [tuple(r) for r in c.execute('SELECT * FROM project_auto_settings')] == settings_before
    assert [tuple(r) for r in c.execute('SELECT * FROM project_auto_settings_events')] == [r + (0,) for r in event_before]
    assert [tuple(r) for r in c.execute('SELECT * FROM jobs')] == job_before
    assert c.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='auto_run_gate'").fetchone()[0] == trigger_before
    assert c.execute('PRAGMA foreign_key_check').fetchall() == []
    c.execute('UPDATE project_auto_settings SET auto_approve=1 WHERE project_id=1')
assert rejected('UPDATE project_auto_settings SET auto_approve=2 WHERE project_id=1')
with db.connect() as c:
    c.execute('DELETE FROM jobs WHERE issue_id=1')
    c.execute('DELETE FROM issues WHERE id=1')
    c.execute('DELETE FROM projects WHERE id=1')
    assert not c.execute('SELECT * FROM project_auto_settings_events').fetchall()
db.config.DB_PATH = original_path
print("OK")
