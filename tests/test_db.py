"""DB 스키마 — 새 파일에 마이그레이션이 걸리고, 두 번 불러도 그대로이며, CHECK/UNIQUE 제약이 막는다."""
import os, sqlite3, sys, tempfile
from pathlib import Path

os.environ["DEV_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import db  # noqa: E402

assert db.init() == len(db.MIGRATIONS)
assert db.init() == len(db.MIGRATIONS)   # 다시 불러도 그대로

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
print("OK")
