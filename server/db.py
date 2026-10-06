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
    # 기존 Plan 승인 위임을 Task 결과 승인으로 묵시 전환하지 않는다.
    """
    INSERT INTO project_auto_settings_events(project_id,actor,before_json,after_json,created_at)
    SELECT project_id,'system:migration/task-result-approval',
      json_object('auto_approve',json('true'),'auto_review',auto_review,'auto_execute',auto_execute,'provider_order',json(provider_order_json)),
      json_object('auto_approve',json('false'),'auto_review',auto_review,'auto_execute',auto_execute,'provider_order',json(provider_order_json),'reason','Plan approval delegation reset for Task result approval'),
      strftime('%Y-%m-%dT%H:%M:%SZ','now')
    FROM project_auto_settings WHERE auto_approve=1;
    UPDATE project_auto_settings SET auto_approve=0 WHERE auto_approve=1;
    CREATE TABLE task_execution_results (
      run_id INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
      plan_version INTEGER NOT NULL,
      state TEXT NOT NULL DEFAULT 'processing',
      commit_event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
      status_event_id INTEGER REFERENCES events(id) ON DELETE SET NULL
    );
    """,

    # 계획 승인과 Task 결과 승인은 각각 별도 관리자 위임으로 저장한다.
    """
    ALTER TABLE project_auto_settings ADD COLUMN auto_plan_approve INTEGER NOT NULL DEFAULT 0 CHECK(auto_plan_approve IN (0,1));
    """,

    # 구현 완료와 후처리의 경계를 영속화한다.
    """
    ALTER TABLE runs ADD COLUMN task_start_sha TEXT NOT NULL DEFAULT '';
    CREATE TABLE execution_completion (
        run_id INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
        issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
        ref TEXT NOT NULL,
        actor TEXT NOT NULL,
        task_sha TEXT NOT NULL,
        auto_merge INTEGER NOT NULL,
        phase TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        merge_sha TEXT NOT NULL DEFAULT '',
        process_id TEXT NOT NULL DEFAULT '',
        worker_id TEXT NOT NULL DEFAULT '',
        notified INTEGER NOT NULL DEFAULT 0,
        owner_event INTEGER NOT NULL,
        cfg_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX execution_completion_phase ON execution_completion(phase);
    """,
]

# 첨부 삭제 큐는 DB 커밋 뒤 파일 정리에 실패해도 재시도할 수 있게 보존한다.
MIGRATIONS.append("""
CREATE TABLE attachments (
    id TEXT PRIMARY KEY,
    issue_id INTEGER REFERENCES issues(id) ON DELETE CASCADE,
    owner TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('image','video','audio','markdown','json','url')),
    name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size INTEGER NOT NULL CHECK(size >= 0),
    storage_key TEXT UNIQUE,
    url TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    CHECK((kind='url' AND storage_key IS NULL AND url IS NOT NULL AND size=0)
       OR (kind<>'url' AND storage_key IS NOT NULL AND url IS NULL))
);
CREATE INDEX attachments_issue ON attachments(issue_id);
CREATE INDEX attachments_expiry ON attachments(expires_at) WHERE issue_id IS NULL;
CREATE TABLE attachment_gc (storage_key TEXT PRIMARY KEY);
CREATE TRIGGER attachment_delete AFTER DELETE ON attachments
WHEN OLD.storage_key IS NOT NULL BEGIN
    INSERT OR IGNORE INTO attachment_gc VALUES(OLD.storage_key);
END;
""")

# 앞의 마이그레이션 SQL은 기존 상태 목록으로 평가해 과거 결과를 유지한다.
# 기존 작업 순서와 감사 기록을 보존하고 이관은 별도 동의로 저장한다.
MIGRATIONS.append("""
CREATE TABLE project_auto_role_settings (
 project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
 orchestrator_provider_order_json TEXT NOT NULL DEFAULT '["claude","codex"]'
 CHECK(orchestrator_provider_order_json IN ('["claude","codex"]','["codex","claude"]')),
 troubleshooter_provider_order_json TEXT NOT NULL DEFAULT '["claude","codex"]'
 CHECK(troubleshooter_provider_order_json IN ('["claude","codex"]','["codex","claude"]')),
 token_exhaustion_fallback INTEGER NOT NULL DEFAULT 0 CHECK(token_exhaustion_fallback IN (0,1))
);
INSERT INTO project_auto_role_settings(project_id) SELECT project_id FROM project_auto_settings;
CREATE TABLE provider_run_failures (
 run_id INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
 failure_reason TEXT NOT NULL
);
CREATE TABLE provider_migration_chains (
 id INTEGER PRIMARY KEY,
 origin_job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
 delegation_id INTEGER NOT NULL REFERENCES project_auto_settings_events(id),
 created_at TEXT NOT NULL
);
CREATE TRIGGER provider_migration_auto_origin BEFORE INSERT ON provider_migration_chains
BEGIN
 SELECT RAISE(ABORT, 'Auto origin required') WHERE NOT EXISTS(
  SELECT 1 FROM jobs WHERE id=NEW.origin_job_id AND source='auto' AND delegation_id=NEW.delegation_id
 );
END;
CREATE TABLE provider_migration_attempts (
 id INTEGER PRIMARY KEY,
 chain_id INTEGER NOT NULL REFERENCES provider_migration_chains(id) ON DELETE CASCADE,
 provider TEXT NOT NULL CHECK(provider IN ('claude','codex')),
 previous_run_id INTEGER UNIQUE REFERENCES runs(id),
 job_id INTEGER UNIQUE REFERENCES jobs(id),
 run_id INTEGER UNIQUE REFERENCES runs(id),
 created_at TEXT NOT NULL,
 UNIQUE(chain_id,provider)
);
""")

# 대기열 순서(DEV-85) — 기존 항목은 ID 순서를 그대로 두고, 정렬 키 없이 들어온 새 항목은 맨 뒤에 붙인다.
MIGRATIONS.append("""
ALTER TABLE jobs ADD COLUMN sort_key INTEGER;
UPDATE jobs SET sort_key=id;
CREATE INDEX jobs_queue_order ON jobs(status, sort_key, id);
CREATE TRIGGER jobs_sort_key_last AFTER INSERT ON jobs WHEN NEW.sort_key IS NULL BEGIN
 UPDATE jobs SET sort_key=(SELECT COALESCE(MAX(sort_key), 0) + 1 FROM jobs) WHERE id=NEW.id;
END;
""")

# Description 자동 작성 에이전트 우선순위(DEV-86) — 다른 역할과 같은 두 순서만 허용한다.
MIGRATIONS.append("""
ALTER TABLE project_auto_role_settings ADD COLUMN description_provider_order_json TEXT NOT NULL DEFAULT '["claude","codex"]'
 CHECK(description_provider_order_json IN ('["claude","codex"]','["codex","claude"]'));
""")

# Description 자동 작성 요청(DEV-86) — 요청 Issue와 고른 provider, 에이전트가 바꾼 이전·새 설명을 남긴다.
MIGRATIONS.append("""
CREATE TABLE project_description_requests (
    issue_id INTEGER PRIMARY KEY REFERENCES issues(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (provider IN ('claude','codex')),
    before_description TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX project_description_requests_project ON project_description_requests(project_id);
CREATE TABLE project_description_events (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    issue_id INTEGER REFERENCES issues(id) ON DELETE SET NULL,
    actor TEXT NOT NULL,
    before TEXT NOT NULL,
    after TEXT NOT NULL,
    created_at TEXT NOT NULL
);
""")

# 역할별 Agent 우선순위(DEV-89) — 행이 없으면 provider 순서 → Agent id 순으로 만든다(기존 동작 그대로).
MIGRATIONS.append("""
CREATE TABLE project_agent_orders (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    agent_ids_json TEXT NOT NULL,
    PRIMARY KEY (project_id, role)
);
""")

# DEV-89-4: 우선순위 Agent의 모델을 대기열·실행 기록에 남긴다(NULL이면 CLI 기본 모델).
MIGRATIONS.append("""
ALTER TABLE jobs ADD COLUMN model TEXT;
ALTER TABLE runs ADD COLUMN model TEXT;
""")

# 계획서 판이 어느 base 커밋을 보고 쓰였는지 — Task 시작 때 그 뒤 다른 이슈가 같은 파일을 바꿨는지 본다(빈 값이면 보지 않음).
MIGRATIONS.append("""
ALTER TABLE plans ADD COLUMN base_sha TEXT NOT NULL DEFAULT '';
""")

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
