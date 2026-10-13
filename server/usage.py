"""현재 보존된 직접 실행 기록을 프로젝트별로 집계한다."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import json
import math

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


def _add_tokens(row, run):
    """유효한 측정값만 더하고 미측정과 실행 중 상태를 보존한다."""
    row['run_count'] += 1
    row['running_run_count'] += run['status'] == 'running'
    counts = [token_count(run[k]) for k in ('input_tokens', 'output_tokens')]
    row['unmeasured_run_count'] += any(v is None for v in counts)
    for k, v in zip(('input_tokens', 'output_tokens'), counts):
        row[k] += v if v is not None else 0
    row['total_tokens'] = row['input_tokens'] + row['output_tokens']
    row['partial'] = bool(row['unmeasured_run_count'] or row['running_run_count'])


def issue_token_usage(c, issue_ids):
    """호출자의 읽기 스냅샷에서 지정 Issue 직접 실행분을 묶음 조회한다."""
    result = {iid: dict(input_tokens=0, output_tokens=0, total_tokens=0,
                        run_count=0, unmeasured_run_count=0, running_run_count=0,
                        partial=False) for iid in issue_ids}
    ids = list(result)
    # SQLite의 바인딩 변수 제한 안에서 목록 최대 크기도 처리한다.
    for start in range(0, len(ids), 900):
        batch = ids[start:start + 900]
        for run in c.execute(
                f"SELECT issue_id,status,input_tokens,output_tokens FROM runs WHERE issue_id IN ({','.join('?' * len(batch))})",
                batch):
            _add_tokens(result[run['issue_id']], run)
    return result


def _now():
    return datetime.now(timezone.utc)


def issue_summary(period, project=None, *, as_of=None):
    """보존된 Issue와 실행 기록의 기간별 게시판 합계."""
    if period not in ('today', 'month', 'year', 'all'):
        raise issues.StoreError('period는 today/month/year/all 중 하나여야 해요.')
    now = _time(as_of) if as_of is not None else _now()
    if now is None:
        raise ValueError('as_of must be an ISO timestamp with a timezone')
    local = now.astimezone(ZoneInfo('Asia/Seoul'))
    begin = {'today': lambda: local.replace(hour=0, minute=0, second=0, microsecond=0),
             'month': lambda: local.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
             'year': lambda: local.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)}
    since = begin[period]().astimezone(timezone.utc) if period != 'all' else None
    summary = _empty()
    summary.update(created_issue_count=0, completed_issue_count=0, partial=False)
    with db.connect() as c:
        c.execute('BEGIN')
        project_id = None
        if project:
            found = c.execute('SELECT id FROM projects WHERE key=?', (project.upper(),)).fetchone()
            if found is None:
                raise issues.StoreError('없는 프로젝트예요.', 404)
            project_id = found['id']
        scope = ' AND project_id=?' if project_id is not None else ''
        args = (project_id,) if project_id is not None else ()
        date_scope = (" AND (julianday(created_at)>=julianday(?) OR "
                      "(status='done' AND julianday(last_done_at)>=julianday(?)))") if since else ''
        date_args = (since.isoformat(), since.isoformat()) if since else ()
        for row in c.execute('SELECT created_at,last_done_at,status FROM issues WHERE 1=1' + scope + date_scope,
                             args + date_args):
            created, done = _time(row['created_at']), _time(row['last_done_at'])
            summary['created_issue_count'] += bool(created and (since is None or created >= since) and created <= now)
            summary['completed_issue_count'] += bool(row['status'] == 'done' and done and
                                                     (since is None or done >= since) and done <= now)

        def collect(run, active, counts):
            started = _time(run['started_at'])
            if started is None or started > now:
                return
            ended = now if active else _time(run['ended_at'])
            started_here = since is None or started >= since
            overlaps = ended is not None and ended >= started and (since is None or ended > since)
            unknown_duration = ended is None and started_here
            if not (started_here or overlaps):
                return
            summary['run_count'] += 1
            summary['running_run_count'] += active
            if started_here:
                summary['unmeasured_run_count'] += any(v is None for v in counts)
                for key, value in zip(('input_tokens', 'output_tokens'), counts):
                    summary[key] += value or 0
            if overlaps:
                summary['agent_seconds'] += max(0, (min(ended, now) - max(started, since or started)).total_seconds())
            summary['unmeasured_duration_run_count'] += unknown_duration

        run_scope = (" AND (julianday(r.started_at)>=julianday(?) OR "
                     "julianday(r.ended_at)>julianday(?) OR r.status='running')") if since else ''
        audit_scope = (" AND (julianday(started_at)>=julianday(?) OR "
                       "julianday(ended_at)>julianday(?) OR status IN ('preparing','running','cancelling'))") if since else ''
        for run in c.execute('SELECT r.* FROM runs r JOIN issues i ON i.id=r.issue_id WHERE 1=1' +
                             (' AND i.project_id=?' if project_id is not None else '') + run_scope,
                             args + date_args):
            collect(run, run['status'] == 'running',
                    [token_count(run[k]) for k in ('input_tokens', 'output_tokens')])
        for run in c.execute('SELECT started_at,ended_at,status,usage_json FROM project_audits WHERE 1=1' +
                             scope + audit_scope, args + date_args):
            usage = json.loads(run['usage_json'])
            collect(run, run['status'] in ('preparing', 'running', 'cancelling'),
                    [token_count(usage.get(k)) for k in ('input_tokens', 'output_tokens')])
    summary['total_tokens'] = summary['input_tokens'] + summary['output_tokens']
    summary['partial'] = bool(summary['unmeasured_run_count'] or summary['running_run_count'] or
                              summary['unmeasured_duration_run_count'])
    return dict(period=period, project=project.upper() if project else None,
                as_of=now.isoformat(), since=since.isoformat() if since else None,
                summary=summary, note='현재 보존된 기록 기준이에요. 병렬 실행 시간은 각각 합산하고 토큰은 시작 시각 기준이에요.')


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
            _add_tokens(row, run)
            row['orphaned_run_count'] += run['status'] == 'orphaned'
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
        audits = []
        audit_summary = _empty()
        audit_summary.update(cost_usd=0, unmeasured_cost_run_count=0)
        for record in c.execute('SELECT * FROM project_audits WHERE project_id=? ORDER BY id DESC', (project['id'],)):
            run = dict(record)
            usage = json.loads(run['usage_json'])
            active = run['status'] in ('preparing', 'running', 'cancelling')
            counts = [token_count(usage.get(k)) for k in ('input_tokens', 'output_tokens')]
            cost = usage.get('cost_usd')
            measured = type(cost) in (int, float) and math.isfinite(cost) and cost >= 0
            duration = _seconds(run['started_at'], as_of if active else run['ended_at'])
            audit_summary['run_count'] += 1
            audit_summary['running_run_count'] += active
            audit_summary['unmeasured_run_count'] += any(v is None for v in counts)
            audit_summary['unmeasured_cost_run_count'] += not measured
            audit_summary['cost_usd'] += cost if measured else 0
            audit_summary['agent_seconds'] += duration or 0
            audit_summary['unmeasured_duration_run_count'] += duration is None
            for k, value in zip(('input_tokens', 'output_tokens'), counts):
                audit_summary[k] += value or 0
            audits.append({k: run[k] for k in ('id', 'provider', 'model', 'actual_model', 'status',
                'started_at', 'ended_at')} | {'input_tokens': counts[0], 'output_tokens': counts[1],
                'cost_usd': cost if measured else None, 'cost_measured': measured, 'agent_seconds': duration})
        audit_summary['total_tokens'] = audit_summary['input_tokens'] + audit_summary['output_tokens']
        audit_summary['partial'] = bool(audit_summary['unmeasured_run_count'] or audit_summary['running_run_count'] or
                                        audit_summary['unmeasured_cost_run_count'])
        for k in _empty():
            summary[k] += audit_summary[k]
        summary['partial'] = bool(summary['unmeasured_run_count'] or summary['running_run_count'] or audit_summary['partial'])
        return dict(project=dict(project), as_of=as_of, summary=summary, issues=rows[offset:offset + limit],
                    audits=audits[offset:offset + limit], audit_summary=audit_summary, audit_total=len(audits),
                    audit_has_more=offset + limit < len(audits),
                    limit=limit, offset=offset, total=len(rows), has_more=offset + limit < len(rows),
                    notes=['실행 시간은 병렬 실행도 각각 합산해요.', 'orphaned는 서버가 기록한 종료 시각을 사용해요.',
                           '삭제된 이슈와 수집되지 않은 외부 실행은 포함하지 않아요.'])
