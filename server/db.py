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
    # 헤드리스 실행 기록(DEV-22) — 'running'인 행이 곧 "지금 도는 것". 서버가 뜰 때 남아 있는 running은 orphaned로 바꾼다.
    """
    CREATE TABLE runs (
        id INTEGER PRIMARY KEY,
        issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
        mode TEXT NOT NULL CHECK (mode IN ('review','execute')),
        status TEXT NOT NULL CHECK (status IN ('running','ok','failed','timeout','orphaned')),
        actor TEXT NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        exit_code INTEGER,
        log_file TEXT NOT NULL DEFAULT '',
        input_tokens INTEGER,
        output_tokens INTEGER,
        cost_usd REAL,
        note TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX runs_issue ON runs(issue_id, id);
    """,
    # Task 번호를 부모 아래에서 센다(NS-17-1)
    """
    -- Task 번호는 만들 때의 부모 번호에 묶인다(NS-17-1): 나중에 부모를 바꾸거나 지워도 ref는 그대로.
    -- number는 UNIQUE라 이 Task들은 음수(-id)로 채워 두고 화면에는 sub_of-sub_number를 쓴다.
    ALTER TABLE issues ADD COLUMN sub_of INTEGER;
    ALTER TABLE issues ADD COLUMN sub_number INTEGER;
    CREATE UNIQUE INDEX issues_sub ON issues(project_id, sub_of, sub_number) WHERE sub_number IS NOT NULL;
    """,
    # Claude 맡기기 대기열(DEV-43) — 바쁠 때 거절하지 않고 줄에 세운다. runs의 CHECK를 바꾸지 않으려고 따로 둔다.
    """
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY,
        issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
        mode TEXT NOT NULL CHECK (mode IN ('review','execute')),
        actor TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('queued','started','cancelled','skipped')),
        note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        started_at TEXT,
        run_id INTEGER
    );
    CREATE INDEX jobs_status ON jobs(status, id);
    """,
    # 검토 제공자를 대기열과 실행 기록에 보존한다. 기존 항목은 Claude다.
    """
    ALTER TABLE jobs ADD COLUMN provider TEXT NOT NULL DEFAULT 'claude';
    ALTER TABLE runs ADD COLUMN provider TEXT NOT NULL DEFAULT 'claude';
    """,
    # Waiting CHECK를 확장하고 기존 행·참조·번호를 보존한다.
    f"""
    CREATE TABLE issues_new (
        id INTEGER PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES projects(id),
        number INTEGER NOT NULL,
        parent_id INTEGER REFERENCES issues(id) ON DELETE SET NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'backlog' CHECK (status IN {_in(STATUSES + ('waiting',))}),
        priority TEXT NOT NULL DEFAULT 'none' CHECK (priority IN {_in(PRIORITIES)}),
        labels_json TEXT NOT NULL DEFAULT '[]',
        reporter TEXT NOT NULL,
        assignee_agent_id INTEGER REFERENCES agents(id) ON DELETE SET NULL,
        claimed_by TEXT,
        lease_until TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        closed_at TEXT,
        sub_of INTEGER,
        sub_number INTEGER,
        UNIQUE (project_id, number)
    );
    INSERT INTO issues_new SELECT * FROM issues;
    DROP TABLE issues;
    ALTER TABLE issues_new RENAME TO issues;
    CREATE INDEX issues_status ON issues(project_id, status);
    CREATE INDEX issues_parent ON issues(parent_id);
    CREATE UNIQUE INDEX issues_sub ON issues(project_id, sub_of, sub_number) WHERE sub_number IS NOT NULL;
    ALTER TABLE jobs ADD COLUMN previous_status TEXT;
    """,
    # 프로젝트 설명은 기존 프로젝트와 생성 호출에서 빈 문자열로 시작한다.
    """
    ALTER TABLE projects ADD COLUMN description TEXT NOT NULL DEFAULT '';
    """,
    # 공식 문서 요청의 설명과 도구를 이슈 본문 변경과 독립적으로 보존한다.
    """
    CREATE TABLE project_doc_requests (
        issue_id INTEGER PRIMARY KEY REFERENCES issues(id) ON DELETE CASCADE,
        project_id INTEGER NOT NULL REFERENCES projects(id),
        provider TEXT NOT NULL CHECK (provider IN ('claude','codex')),
        description TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX project_doc_requests_project ON project_doc_requests(project_id, provider);
    """,
    # 종류는 Labels와 독립된 안정 ID로 관리하고 비활성화해도 연결을 보존한다.
    """
    CREATE TABLE issue_types (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE CHECK(length(trim(name)) BETWEEN 1 AND 100),
        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
    );
    INSERT INTO issue_types(id,name) VALUES
        (1,'버그 수정'),(2,'문서 생성'),(3,'UI/UX'),(4,'기능 추가');
    CREATE TABLE issue_type_links (
        issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
        type_id INTEGER NOT NULL REFERENCES issue_types(id),
        source TEXT NOT NULL CHECK(source IN ('human','agent')),
        actor TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(issue_id,type_id)
    );
    CREATE INDEX issue_type_links_type ON issue_type_links(type_id,issue_id);
    """,

    # 프로젝트별 자동화 위임을 저장하고 변경 이력을 별도로 보존한다.
    """
    CREATE TABLE project_auto_settings (
        project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
        auto_review INTEGER NOT NULL DEFAULT 0 CHECK(auto_review IN (0,1)),
        auto_execute INTEGER NOT NULL DEFAULT 0 CHECK(auto_execute IN (0,1)),
        auto_approve INTEGER NOT NULL DEFAULT 0 CHECK(auto_approve=0),
        provider_order_json TEXT NOT NULL DEFAULT '["claude","codex"]'
            CHECK(provider_order_json IN ('["claude","codex"]','["codex","claude"]')),
        updated_by TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE project_auto_settings_events (
        id INTEGER PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        actor TEXT NOT NULL,
        before_json TEXT NOT NULL,
        after_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """,
    # 기존 공식 문서 요청만 문서 생성 종류에 연결한다. 기존 선택과 스냅샷은 보존한다.
    """
    INSERT OR IGNORE INTO issue_type_links(issue_id,type_id,source,actor,created_at)
        SELECT r.issue_id,2,
            CASE WHEN i.reporter LIKE 'human:%' THEN 'human' ELSE 'agent' END,
            i.reporter,r.created_at
        FROM project_doc_requests r JOIN issues i ON i.id=r.issue_id;
    """,
    # 자동 등록 출처와 위임 설정 판을 수동 대기와 구분한다.
    """
    ALTER TABLE jobs ADD COLUMN source TEXT NOT NULL DEFAULT 'manual' CHECK(source IN ('manual','auto'));
    ALTER TABLE jobs ADD COLUMN delegation_id INTEGER REFERENCES project_auto_settings_events(id);
    ALTER TABLE jobs ADD COLUMN approval_version INTEGER;
    -- 준비 중 OFF·새 계획·승인 철회가 발생해도 유료 실행 기록을 만들기 직전에 차단한다.
    CREATE TRIGGER auto_run_gate BEFORE INSERT ON runs
    WHEN EXISTS(SELECT 1 FROM jobs WHERE issue_id=NEW.issue_id AND mode=NEW.mode AND status='queued' AND source='auto')
    BEGIN
        SELECT RAISE(ABORT, '자동 위임 조건이 바뀌었어요') WHERE NOT EXISTS(
            SELECT 1 FROM jobs j JOIN issues i ON i.id=j.issue_id
            JOIN projects p ON p.id=i.project_id
            JOIN project_auto_settings s ON s.project_id=p.id
            WHERE j.issue_id=NEW.issue_id AND j.mode=NEW.mode AND j.status='queued' AND j.source='auto'
            AND p.archived=0 AND i.status='waiting' AND i.claimed_by IS NULL
            AND NOT EXISTS(SELECT 1 FROM json_each(i.labels_json) WHERE value='goal')
            AND ((NEW.mode='review' AND s.auto_review=1 AND i.parent_id IS NULL
                  AND i.reporter LIKE 'human:%'
                  AND NOT EXISTS(SELECT 1 FROM plans WHERE issue_id=i.id))
              OR (NEW.mode='execute' AND s.auto_execute=1
                  AND j.approval_version=(SELECT MAX(version) FROM plans WHERE issue_id=i.parent_id)
                  AND EXISTS(SELECT 1 FROM decisions d WHERE d.id=(SELECT MAX(id) FROM decisions WHERE issue_id=i.parent_id AND gate='plan')
                      AND d.plan_version=j.approval_version AND d.verdict IN ('approve','approve_notes') AND d.actor LIKE 'human:%')
                  AND NOT EXISTS(SELECT 1 FROM issue_deps dep JOIN issues b ON b.id=dep.blocked_by_id WHERE dep.issue_id=i.id AND b.status<>'done')))
        );
    END;
    """,
    # 승인 위임을 허용하되 기존 이력과 실행 게이트를 보존한다.
    """
    DROP TRIGGER auto_run_gate;
    CREATE TABLE project_auto_settings_new (
        project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
        auto_review INTEGER NOT NULL DEFAULT 0 CHECK(auto_review IN (0,1)),
        auto_execute INTEGER NOT NULL DEFAULT 0 CHECK(auto_execute IN (0,1)),
        auto_approve INTEGER NOT NULL DEFAULT 0 CHECK(auto_approve IN (0,1)),
        provider_order_json TEXT NOT NULL DEFAULT '["claude","codex"]'
            CHECK(provider_order_json IN ('["claude","codex"]','["codex","claude"]')),
        updated_by TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    INSERT INTO project_auto_settings_new SELECT * FROM project_auto_settings;
    DROP TABLE project_auto_settings;
    ALTER TABLE project_auto_settings_new RENAME TO project_auto_settings;
    ALTER TABLE project_auto_settings_events ADD COLUMN plan_id_floor INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE review_plan_runs (
        plan_id INTEGER PRIMARY KEY REFERENCES plans(id) ON DELETE CASCADE,
        run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE
    );
    CREATE TRIGGER auto_run_gate BEFORE INSERT ON runs
    WHEN EXISTS(SELECT 1 FROM jobs WHERE issue_id=NEW.issue_id AND mode=NEW.mode AND status='queued' AND source='auto')
    BEGIN
        SELECT RAISE(ABORT, '자동 위임 조건이 바뀌었어요') WHERE NOT EXISTS(
            SELECT 1 FROM jobs j JOIN issues i ON i.id=j.issue_id
            JOIN projects p ON p.id=i.project_id
            JOIN project_auto_settings s ON s.project_id=p.id
            WHERE j.issue_id=NEW.issue_id AND j.mode=NEW.mode AND j.status='queued' AND j.source='auto'
            AND p.archived=0 AND i.status='waiting' AND i.claimed_by IS NULL
            AND NOT EXISTS(SELECT 1 FROM json_each(i.labels_json) WHERE value='goal')
            AND ((NEW.mode='review' AND s.auto_review=1 AND i.parent_id IS NULL
                  AND i.reporter LIKE 'human:%'
                  AND NOT EXISTS(SELECT 1 FROM plans WHERE issue_id=i.id))
              OR (NEW.mode='execute' AND s.auto_execute=1
                  AND j.approval_version=(SELECT MAX(version) FROM plans WHERE issue_id=i.parent_id)
                  AND EXISTS(SELECT 1 FROM decisions d WHERE d.id=(SELECT MAX(id) FROM decisions WHERE issue_id=i.parent_id AND gate='plan')
                      AND d.plan_version=j.approval_version AND d.verdict IN ('approve','approve_notes') AND d.actor LIKE 'human:%')
                  AND NOT EXISTS(SELECT 1 FROM issue_deps dep JOIN issues b ON b.id=dep.blocked_by_id WHERE dep.issue_id=i.id AND b.status<>'done')))
        );
    END;
    """,
    # 취소 이력은 보존하고 명확한 실행 OFF만 재등록 대상으로 구분한다.
    """
    ALTER TABLE jobs ADD COLUMN cancellation_reason TEXT;
    ALTER TABLE jobs ADD COLUMN cancellation_event_id INTEGER REFERENCES project_auto_settings_events(id);
    -- 착수 준비 도중 sync가 대기를 취소해도 기존 auto_run_gate를 우회하지 않는다.
    CREATE TRIGGER auto_job_owner_gate BEFORE INSERT ON runs
    WHEN NEW.actor LIKE 'human:auto/delegation/%'
    BEGIN
        SELECT RAISE(ABORT, '자동 위임 조건이 바뀌었어요') WHERE NOT EXISTS(
            SELECT 1 FROM jobs WHERE issue_id=NEW.issue_id AND mode=NEW.mode
            AND actor=NEW.actor AND source='auto' AND status='queued'
        );
    END;
    """,

]

# 앞의 마이그레이션 SQL은 기존 상태 목록으로 평가해 과거 결과를 유지한다.
STATUSES += ("waiting",)


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
        conn.execute("UPDATE runs SET status='orphaned', ended_at=?, note='서버가 다시 떠서 끊겼어요' WHERE status='running'", (now_iso(),))
        conn.commit()
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
