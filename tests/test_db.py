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
        added = {'jobs': (None, 'manual', None, None, None, None, 1, None), 'projects': ('',), 'runs': ('', None), 'plans': ('',)}.get(table, ())   # 끝 None: DEV-89-4 model, plans ''는 base_sha
        if table == 'issues':
            assert after == [r + (now if r[0] == 2 else None, None) for r in before[table]], table
        else:
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
    approval_version = next(i for i, sql in enumerate(db.MIGRATIONS) if 'CREATE TABLE project_auto_settings_new' in sql)
    for version, sql in enumerate(db.MIGRATIONS[:approval_version], 1):
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
    assert [tuple(r) for r in c.execute('SELECT * FROM project_auto_settings')] == [r + (0,) for r in settings_before]
    assert [tuple(r) for r in c.execute('SELECT * FROM project_auto_settings_events')] == [r + (0,) for r in event_before]
    assert [tuple(r) for r in c.execute('SELECT * FROM jobs')] == [r + (None, None, r[0], None) for r in job_before]   # 끝 None: DEV-89-4 model
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

# 대기열 순서(DEV-85) 도입 직전 판 — 기존 순서는 ID 순으로 보존하고, 정렬 키 없이 넣은 새 항목은 맨 뒤다.
db.config.DB_PATH = Path(tempfile.mkdtemp()) / 'order-legacy.db'
with sqlite3.connect(db.config.DB_PATH) as c:
    order_version = next(i for i, sql in enumerate(db.MIGRATIONS) if 'jobs_sort_key_last' in sql)
    for version, sql in enumerate(db.MIGRATIONS[:order_version], 1):
        c.executescript(sql + f'PRAGMA user_version={version};')
    c.execute("INSERT INTO projects(key,name,created_at) VALUES('ORD','순서',?)", (now,))
    c.execute("INSERT INTO issues(project_id,number,title,reporter,created_at,updated_at) VALUES(1,1,'이슈','human:admin',?,?)", (now, now))
    for jid, status in ((3, 'queued'), (5, 'cancelled'), (9, 'queued')):
        c.execute("INSERT INTO jobs(id,issue_id,mode,status,actor,created_at) VALUES(?,1,'review',?,'human:admin',?)", (jid, status, now))
    jobs_before = c.execute('SELECT * FROM jobs ORDER BY id').fetchall()
assert db.init() == len(db.MIGRATIONS) and db.init() == len(db.MIGRATIONS)
with db.connect() as c:
    assert [tuple(r) for r in c.execute('SELECT * FROM jobs ORDER BY id')] == [r + (r[0], None) for r in jobs_before]   # 끝 None: DEV-89-4 model
    c.execute("UPDATE jobs SET sort_key=100 WHERE id=3")   # 사람이 옮긴 뒤에도 새 항목은 가장 큰 키 뒤로
    new = c.execute("INSERT INTO jobs(issue_id,mode,status,actor,created_at) VALUES(1,'execute','queued','human:admin',?)", (now,)).lastrowid
    assert c.execute('SELECT sort_key FROM jobs WHERE id=?', (new,)).fetchone()[0] == 101
    kept = c.execute("INSERT INTO jobs(issue_id,mode,status,actor,created_at,sort_key) VALUES(1,'review','cancelled','human:admin',?,7)", (now,)).lastrowid
    assert c.execute('SELECT sort_key FROM jobs WHERE id=?', (kept,)).fetchone()[0] == 7   # 명시한 키는 덮지 않는다
    assert 'jobs_queue_order' in {r[1] for r in c.execute('PRAGMA index_list(jobs)')}
db.config.DB_PATH = original_path
print("OK")

# 생애 시각 도입 직전 DB를 업그레이드하고 근거 없는 값은 비워 둔다.
db.config.DB_PATH = Path(tempfile.mkdtemp()) / 'lifecycle.db'
with sqlite3.connect(db.config.DB_PATH) as c:
    for version, sql in enumerate(db.MIGRATIONS[:-1], 1):
        c.executescript(sql + f'PRAGMA user_version={version};')
    c.execute("INSERT INTO projects(key,name,created_at) VALUES('LIFE','lifecycle',?)", (now,))
    for iid, status, closed in ((1,'done','2026-01-05T00:00:00Z'),(2,'closed','2026-01-06T00:00:00Z'),(3,'backlog',None),(4,'in_review',None)):
        c.execute("INSERT INTO issues(id,project_id,number,title,reporter,status,created_at,updated_at,closed_at) VALUES(?,1,?,'lifecycle','human:admin',?,?,?,?)", (iid,iid,status,now,now,closed))
    for iid, target, at in ((1,'in_progress','2026-01-02T00:00:00Z'),(1,'done','2026-01-04T00:00:00Z'),(2,'done','2026-01-03T00:00:00Z'),(2,'done','2026-01-04T00:00:00Z'),(4,'in_progress','2026-01-01T01:00:00+02:00')):
        c.execute("INSERT INTO events(issue_id,actor,kind,data_json,created_at) VALUES(?,'human:admin','status',json_object('to',?),?)", (iid,target,at))
    c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at) VALUES(1,'execute','ok','human:admin','2026-01-01T00:00:00Z')")
    c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at) VALUES(4,'review','ok','human:admin','2026-01-01T00:00:00Z')")
    c.execute("INSERT INTO events(issue_id,actor,kind,data_json,created_at) VALUES(3,'human:admin','status','broken',?)", (now,))
    c.execute("INSERT INTO issues(id,project_id,number,title,reporter,status,created_at,updated_at,closed_at) VALUES(8,1,8,'closed only','human:admin','closed',?,?,?)", (now,now,now))
    # 큰 이력에서도 이슈·이벤트를 한 번씩 모으고 인덱스로 후보를 찾는다.
    c.executemany("INSERT INTO issues(id,project_id,number,title,reporter,created_at,updated_at) VALUES(?,1,?,'bulk','human:admin',?,?)", [(i,i,now,now) for i in range(100,2100)])
    c.executemany("INSERT INTO events(issue_id,actor,kind,data_json,created_at) VALUES(?,'human:admin','status',?,?)", [(i,'{"to":"in_progress"}',f'2026-01-0{day}T00:00:00Z') for i in range(100,2100) for day in range(1,6)])
assert db.init() == len(db.MIGRATIONS)
with db.connect() as c:
    values = [tuple(r) for r in c.execute('SELECT first_started_at,last_done_at FROM issues WHERE id<8 ORDER BY id')]
    assert values == [('2026-01-01T00:00:00Z','2026-01-05T00:00:00Z'),(None,'2026-01-04T00:00:00Z'),(None,None),('2026-01-01T01:00:00+02:00',None)], values
    assert tuple(c.execute('SELECT first_started_at,last_done_at FROM issues WHERE id=8').fetchone()) == (None,None)
    assert c.execute("SELECT COUNT(*) FROM issues WHERE id>=100 AND first_started_at='2026-01-01T00:00:00Z' AND last_done_at IS NULL").fetchone()[0] == 2000
assert db.init() == len(db.MIGRATIONS)
with db.connect() as c:
    assert values == [tuple(r) for r in c.execute('SELECT first_started_at,last_done_at FROM issues WHERE id<8 ORDER BY id')]
    for iid, status in ((5,'in_progress'),(6,'done'),(7,'closed')):
        c.execute("INSERT INTO issues(id,project_id,number,title,reporter,status,created_at,updated_at) VALUES(?,1,?,'lifecycle','human:admin',?,?,?)", (iid,iid,status,now,now))
    assert [tuple(r) for r in c.execute('SELECT first_started_at,last_done_at FROM issues WHERE id BETWEEN 5 AND 7 ORDER BY id')] == [(now,None),(None,now),(None,None)]
    assert not c.execute('PRAGMA foreign_key_check').fetchall()
db.config.DB_PATH = original_path
print('OK lifecycle migration')
