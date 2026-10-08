"""현재 보존된 직접 실행 기록을 프로젝트별로 집계한다."""
from datetime import datetime, timezone

import db
import issues
from review import token_count


def _time(value):
    try:
        value = datetime.fromisoformat(value)
        return value if value.tzinfo else None
    except (TypeError, ValueError):
        return None


def _seconds(start, end):
    start, end = _time(start), _time(end)
    return (end - start).total_seconds() if start and end and end >= start else None


def _empty():
    return dict(input_tokens=0, output_tokens=0, total_tokens=0, run_count=0,
                unmeasured_run_count=0, running_run_count=0, agent_seconds=0,
                unmeasured_duration_run_count=0, orphaned_run_count=0)


def project_usage(key, limit=50, offset=0, *, as_of=None):
    """같은 조회 시각·DB 스냅샷으로 요약과 페이지를 반환한다."""
    if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
        raise issues.StoreError("limit은 1~200, offset은 0 이상이어야 해요.")
    as_of = as_of or datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db.connect() as c:
        c.execute("BEGIN")
        project = c.execute("SELECT id,key,name FROM projects WHERE key=?", (key.upper(),)).fetchone()
        if project is None:
            raise issues.StoreError("없는 프로젝트예요.", 404)
        rows = [dict(r) for r in c.execute(
            f"SELECT i.id,i.title,i.status,i.first_started_at,i.last_done_at,{issues.ref_sql('i', 'p')} AS ref "
            "FROM issues i JOIN projects p ON p.id=i.project_id WHERE p.id=? ORDER BY i.id DESC", (project['id'],))]
        by_id = {r['id']: r for r in rows}
        for row in rows:
            row.update(_empty())
            row['elapsed_seconds'] = _seconds(row['first_started_at'], row['last_done_at'])
            row['ongoing_elapsed_seconds'] = None if row['status'] in ('done', 'closed') else _seconds(row['first_started_at'], as_of)
        for run in c.execute("SELECT r.* FROM runs r JOIN issues i ON i.id=r.issue_id WHERE i.project_id=?", (project['id'],)):
            row = by_id[run['issue_id']]
            row['run_count'] += 1
            row['running_run_count'] += run['status'] == 'running'
            row['orphaned_run_count'] += run['status'] == 'orphaned'
            counts = [token_count(run[k]) for k in ('input_tokens', 'output_tokens')]
            row['unmeasured_run_count'] += any(v is None for v in counts)
            for k, v in zip(('input_tokens', 'output_tokens'), counts):
                row[k] += v if v is not None else 0
            duration = _seconds(run['started_at'], as_of if run['status'] == 'running' else run['ended_at'])
            row['unmeasured_duration_run_count'] += duration is None
            row['agent_seconds'] += duration if duration is not None else 0
        summary = _empty()
        summary.update(issue_count=len(rows), completed_issue_count=0, completed_elapsed_seconds=0,
                       unmeasured_completed_issue_count=0)
        for row in rows:
            row['total_tokens'] = row['input_tokens'] + row['output_tokens']
            row['partial'] = bool(row['unmeasured_run_count'] or row['running_run_count'])
            for k in _empty():
                summary[k] += row[k]
            if row['status'] == 'done':
                summary['completed_issue_count'] += 1
                summary['unmeasured_completed_issue_count'] += row['elapsed_seconds'] is None
                summary['completed_elapsed_seconds'] += row['elapsed_seconds'] or 0
        summary['partial'] = bool(summary['unmeasured_run_count'] or summary['running_run_count'])
        return dict(project=dict(project), as_of=as_of, summary=summary, issues=rows[offset:offset + limit],
                    limit=limit, offset=offset, total=len(rows), has_more=offset + limit < len(rows),
                    notes=['실행 시간은 병렬 실행도 각각 합산해요.', 'orphaned는 서버가 기록한 종료 시각을 사용해요.',
                           '삭제된 이슈와 수집되지 않은 외부 실행은 포함하지 않아요.'])
