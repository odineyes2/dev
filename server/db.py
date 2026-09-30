"""
SQLite 저장소 — 프로젝트·이슈·계획서·이벤트·에이전트.

## 스키마 결정

- 이슈 번호는 프로젝트마다 따로 센다(projects.key + issues.number → "NS-27"). id는 내부용.
- 상태·우선순위는 CHECK 제약으로 막는다 — 앱 코드가 틀려도 이상한 값이 들어가지 않게.
- 누가 했는지(reporter/author/actor)는 문자열 하나로 적는다: 사람은 "human:<이름>", 에이전트는 "agent:<id>".
  그때의 모델명은 events.data_json에 남긴다(에이전트의 모델은 나중에 바뀔 수 있다).
- 상태 변경·댓글·계획서·커밋 연결은 전부 events에 한 줄씩 — 이슈의 타임라인이자 감사 기록.

## 마이그레이션

PRAGMA user_version을 판 번호로 쓴다. MIGRATIONS에 새 SQL을 뒤에 덧붙이기만 하고, 앞의 것은 고치지 않는다.

## 동시성

쓰는 프로세스는 app 하나. WAL + busy_timeout, 호출마다 짧게 연결을 열고 닫는다.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import config

STATUSES = ("backlog", "triage", "in_progress", "in_review", "changes_requested", "on_hold", "done", "closed")
PRIORITIES = ("urgent", "high", "medium", "low", "none")


def _in(values):
    return "(" + ",".join(f"'{v}'" for v in values) + ")"


MIGRATIONS = [
    f"""
    CREATE TABLE projects (
        id INTEGER PRIMARY KEY,
        key TEXT NOT NULL UNIQUE CHECK (key GLOB '[A-Z][A-Z0-9]*' AND length(key) <= 10),
        name TEXT NOT NULL,
        repo_url TEXT NOT NULL DEFAULT '',
        local_path TEXT NOT NULL DEFAULT '',
        next_number INTEGER NOT NULL DEFAULT 1,
        archived INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE TABLE agents (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        vendor TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        key_hash TEXT NOT NULL UNIQUE,
        key_prefix TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        last_seen_at TEXT
    );
    CREATE TABLE issues (
        id INTEGER PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES projects(id),
        number INTEGER NOT NULL,
        parent_id INTEGER REFERENCES issues(id) ON DELETE SET NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'backlog' CHECK (status IN {_in(STATUSES)}),
        priority TEXT NOT NULL DEFAULT 'none' CHECK (priority IN {_in(PRIORITIES)}),
        labels_json TEXT NOT NULL DEFAULT '[]',
        reporter TEXT NOT NULL,
        assignee_agent_id INTEGER REFERENCES agents(id) ON DELETE SET NULL,
        claimed_by TEXT,
        lease_until TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        closed_at TEXT,
        UNIQUE (project_id, number)
    );
    CREATE INDEX issues_status ON issues(project_id, status);
    CREATE INDEX issues_parent ON issues(parent_id);
    CREATE TABLE issue_deps (
        issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
        blocked_by_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
        PRIMARY KEY (issue_id, blocked_by_id),
        CHECK (issue_id <> blocked_by_id)
    );
    CREATE TABLE plans (
        id INTEGER PRIMARY KEY,
        issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
        version INTEGER NOT NULL,
        body TEXT NOT NULL,
        author TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (issue_id, version)
    );
    CREATE TABLE events (
        id INTEGER PRIMARY KEY,
        issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
        actor TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('comment','status','plan','commit','assign','claim','edit')),
        body TEXT NOT NULL DEFAULT '',
        data_json TEXT NOT NULL DEFAULT '{{}}',
        created_at TEXT NOT NULL
    );
    CREATE INDEX events_issue ON events(issue_id, id);
    """,
    # 사람의 결정(DEV-14) — gate 'plan' = 계획서 승인, 'result' = 결과 승인(나중에). 마지막 결정이 유효.
    """
    CREATE TABLE decisions (
        id INTEGER PRIMARY KEY,
        issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
        gate TEXT NOT NULL CHECK (gate IN ('plan','result')),
        plan_version INTEGER NOT NULL,
        verdict TEXT NOT NULL CHECK (verdict IN ('approve','approve_notes','reject')),
        note TEXT NOT NULL DEFAULT '',
        actor TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX decisions_issue ON decisions(issue_id, id);
    """,
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        with conn:   # 성공하면 커밋, 예외면 롤백
            yield conn
    finally:
        conn.close()


def init() -> int:
    """DB 파일을 만들고 밀린 마이그레이션을 건다. 지금 판 번호를 돌려준다."""
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            conn.executescript("BEGIN;" + sql + f"PRAGMA user_version={i};COMMIT;")
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
