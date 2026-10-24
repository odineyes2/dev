"""현재 보존된 직접 실행 기록을 프로젝트별로 집계한다."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
import json
import math
import re

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


def task_token_usage(c, parent_ids):
    """지정 Issue의 현재 하위 Task 직접 실행분과 Task 수를 묶음 집계한다."""
    result = {iid: None for iid in parent_ids}
    ids = list(result)
    for start in range(0, len(ids), 900):
        batch = ids[start:start + 900]
        seen = set()
        for run in c.execute(
                f"SELECT t.parent_id,t.id AS task_id,r.status,r.input_tokens,r.output_tokens "
                f"FROM issues t LEFT JOIN runs r ON r.issue_id=t.id "
                f"WHERE t.parent_id IN ({','.join('?' * len(batch))}) "
                "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.issue_id=t.id "
                "AND json_extract(e.data_json,'$.superseded_by_plan') IS NOT NULL)", batch):
            parent_id = run['parent_id']
            if result[parent_id] is None:
                result[parent_id] = dict(input_tokens=0, output_tokens=0, total_tokens=0,
                                         run_count=0, unmeasured_run_count=0, running_run_count=0,
                                         partial=False, task_count=0)
            if run['task_id'] not in seen:
                result[parent_id]['task_count'] += 1
                seen.add(run['task_id'])
            if run['status'] is not None:
                _add_tokens(result[parent_id], run)
    return result


def _now():
    return datetime.now(timezone.utc)


def issue_summary(period, project=None, *, as_of=None):
    """보존된 Issue와 실행 기록의 기간별 게시판 합계."""
    if period not in ('today', 'yesterday', 'week', 'last_week', 'month', 'last_month', 'year', 'all'):
        raise issues.StoreError('period는 today/yesterday/week/last_week/month/last_month/year/all 중 하나여야 해요.')
    now = _time(as_of) if as_of is not None else _now()
    if now is None:
        raise ValueError('as_of must be an ISO timestamp with a timezone')
    local = now.astimezone(ZoneInfo('Asia/Seoul'))
    today = local.replace(hour=0, minute=0, second=0, microsecond=0)
    month = today.replace(day=1)
    week = today - timedelta(days=today.weekday())
    bounds = {'today': (today, local), 'yesterday': (today - timedelta(days=1), today),
              'week': (week, local), 'last_week': (week - timedelta(days=7), week),
              'month': (month, local),
              'last_month': ((month - timedelta(days=1)).replace(day=1), month),
              'year': (month.replace(month=1), local), 'all': (None, local)}
    start_local, end_local = bounds[period]
    since = start_local.astimezone(timezone.utc) if start_local else None
    until = min(now, end_local.astimezone(timezone.utc))
    historical = until < now
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
        if historical:
            date_scope += ' AND julianday(created_at)<julianday(?)'
            date_args += (until.isoformat(),)
        for row in c.execute('SELECT created_at,last_done_at,status FROM issues WHERE 1=1' + scope + date_scope,
                             args + date_args):
            created, done = _time(row['created_at']), _time(row['last_done_at'])
            summary['created_issue_count'] += bool(created and (since is None or created >= since) and
                                                   (created < until if historical else created <= until))
            summary['completed_issue_count'] += bool(row['status'] == 'done' and done and
                                                     (since is None or done >= since) and
                                                     (done < until if historical else done <= until))

        def collect(run, active, counts):
            started = _time(run['started_at'])
            if started is None or (started >= until if historical else started > until):
                return
            ended = until if active else _time(run['ended_at'])
            started_here = since is None or started >= since
            overlaps = ended is not None and ended >= started and (since is None or ended > since)
            unknown_duration = ended is None and started_here
            if not (started_here or overlaps):
                return
            summary['run_count'] += 1
            summary['running_run_count'] += active and not historical
            if started_here:
                summary['unmeasured_run_count'] += any(v is None for v in counts)
                for key, value in zip(('input_tokens', 'output_tokens'), counts):
                    summary[key] += value or 0
            if overlaps:
                summary['agent_seconds'] += max(0, (min(ended, until) - max(started, since or started)).total_seconds())
            summary['unmeasured_duration_run_count'] += unknown_duration

        run_scope = (" AND (julianday(r.started_at)>=julianday(?) OR "
                     "julianday(r.ended_at)>julianday(?) OR r.status='running')") if since else ''
        audit_scope = (" AND (julianday(started_at)>=julianday(?) OR "
                       "julianday(ended_at)>julianday(?) OR status IN ('preparing','running','cancelling'))") if since else ''
        run_args = args + ((since.isoformat(), since.isoformat()) if since else ())
        audit_args = run_args
        if historical:
            run_scope += ' AND julianday(r.started_at)<julianday(?)'
            audit_scope += ' AND julianday(started_at)<julianday(?)'
            run_args += (until.isoformat(),)
            audit_args += (until.isoformat(),)
        for run in c.execute('SELECT r.* FROM runs r JOIN issues i ON i.id=r.issue_id WHERE 1=1' +
                             (' AND i.project_id=?' if project_id is not None else '') + run_scope,
                             run_args):
            collect(run, run['status'] == 'running',
                    [token_count(run[k]) for k in ('input_tokens', 'output_tokens')])
        for run in c.execute('SELECT started_at,ended_at,status,usage_json FROM project_audits WHERE 1=1' +
                             scope + audit_scope, audit_args):
            usage = json.loads(run['usage_json'])
            collect(run, run['status'] in ('preparing', 'running', 'cancelling'),
                    [token_count(usage.get(k)) for k in ('input_tokens', 'output_tokens')])
    summary['total_tokens'] = summary['input_tokens'] + summary['output_tokens']
    summary['partial'] = bool(summary['unmeasured_run_count'] or summary['running_run_count'] or
                              summary['unmeasured_duration_run_count'])
    return dict(period=period, project=project.upper() if project else None,
                as_of=now.isoformat(), since=since.isoformat() if since else None, until=until.isoformat(),
                summary=summary, note='현재 보존된 기록 기준이에요. 병렬 실행 시간은 각각 합산하고 토큰은 시작 시각 기준이에요.')


def trends(from_date=None, to_date=None, project=None, *, as_of=None):
    """서울 날짜별 Issue와 직접 실행·프로젝트 점검 사용량을 조회한다."""
    now = _time(as_of) if as_of is not None else _now()
    if now is None:
        raise ValueError('as_of must be an ISO timestamp with a timezone')
    seoul = ZoneInfo('Asia/Seoul')

    def parse(value, default):
        if value is None:
            return default
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise issues.StoreError('날짜는 YYYY-MM-DD 형식이어야 해요.')
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise issues.StoreError('유효한 날짜를 입력해 주세요.')

    today = now.astimezone(seoul).date()
    end_day = parse(to_date, today)
    start_day = parse(from_date, end_day - timedelta(days=min(29, end_day.toordinal() - 1)))
    days = (end_day - start_day).days + 1
    if not 1 <= days <= 366:
        raise issues.StoreError('조회 기간은 시작일부터 종료일까지 1~366일이어야 해요.')
    if end_day == date.max:
        raise issues.StoreError('조회 가능한 종료일을 입력해 주세요.')
    begin = datetime.combine(start_day, time.min, seoul).astimezone(timezone.utc)
    finish = datetime.combine(end_day + timedelta(days=1), time.min, seoul).astimezone(timezone.utc)
    rows = {}
    for offset in range(days):
        day = start_day + timedelta(days=offset)
        rows[day] = {'date': day.isoformat(), 'created_issue_count': 0,
                     'completed_issue_count': 0, **_empty(), 'partial': False}
    ranking = {}

    def day_of(value):
        parsed = _time(value)
        return parsed.astimezone(seoul).date() if parsed and parsed <= now else None

    def collect(run, counts, active, issue_id=None):
        started = _time(run['started_at'])
        if started is None or started > now or started >= finish:
            return
        day = started.astimezone(seoul).date()
        if day in rows:
            bucket = rows[day]
            bucket['run_count'] += 1
            bucket['unmeasured_run_count'] += any(value is None for value in counts)
            bucket['running_run_count'] += active
            bucket['orphaned_run_count'] += run['status'] == 'orphaned'
            for key, value in zip(('input_tokens', 'output_tokens'), counts):
                bucket[key] += value or 0
            if issue_id is not None:
                ranking.setdefault(issue_id, [0, 0])
                for index, value in enumerate(counts):
                    ranking[issue_id][index] += value or 0
        ended = now if active else _time(run['ended_at'])
        if ended is None or ended < started:
            if day in rows:
                rows[day]['unmeasured_duration_run_count'] += 1
            return
        clipped_end = min(ended, now, finish)
        if clipped_end <= begin:
            return
        cursor = max(started, begin)
        while cursor < clipped_end:
            local_day = cursor.astimezone(seoul).date()
            next_day = datetime.combine(local_day + timedelta(days=1), time.min, seoul).astimezone(timezone.utc)
            segment_end = min(clipped_end, next_day)
            bucket = rows[local_day]
            bucket['agent_seconds'] += (segment_end - cursor).total_seconds()
            if local_day != day:
                bucket['running_run_count'] += active
            cursor = segment_end

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
        # 날짜 조건은 보존된 기록 중 조회 범위와 겹치는 행만 읽는다.
        for item in c.execute('SELECT created_at,last_done_at,status FROM issues WHERE 1=1' + scope +
                              ' AND (julianday(created_at)<julianday(?) AND julianday(created_at)>=julianday(?)'
                              " OR (status='done' AND julianday(last_done_at)>=julianday(?) AND julianday(last_done_at)<julianday(?)))",
                              args + (finish.isoformat(), begin.isoformat(), begin.isoformat(), finish.isoformat())):
            created = day_of(item['created_at'])
            done = day_of(item['last_done_at']) if item['status'] == 'done' else None
            if created in rows:
                rows[created]['created_issue_count'] += 1
            if done in rows:
                rows[done]['completed_issue_count'] += 1
        overlap = (' AND julianday(started_at)<julianday(?) AND '
                   '(julianday(started_at)>=julianday(?) OR julianday(ended_at)>julianday(?) '
                   "OR status IN ('preparing','running','cancelling'))")
        run_overlap = (' AND julianday(r.started_at)<julianday(?) AND '
                       '(julianday(r.started_at)>=julianday(?) OR julianday(r.ended_at)>julianday(?) OR r.status=?)')
        bounds = (finish.isoformat(), begin.isoformat(), begin.isoformat())
        for run in c.execute('SELECT r.issue_id,r.started_at,r.ended_at,r.status,r.input_tokens,r.output_tokens '
                             'FROM runs r JOIN issues i ON i.id=r.issue_id WHERE 1=1' +
                             (' AND i.project_id=?' if project_id is not None else '') + run_overlap,
                             args + bounds + ('running',)):
            collect(run, [token_count(run[k]) for k in ('input_tokens', 'output_tokens')],
                    run['status'] == 'running', run['issue_id'])
        for run in c.execute('SELECT started_at,ended_at,status,usage_json FROM project_audits WHERE 1=1' +
                             scope + overlap, args + bounds):
            usage = json.loads(run['usage_json'])
            collect(run, [token_count(usage.get(k)) for k in ('input_tokens', 'output_tokens')],
                    run['status'] in ('preparing', 'running', 'cancelling'))
        top = []
        if ranking:
            for start in range(0, len(ranking), 900):
                batch = list(ranking)[start:start + 900]
                for item in c.execute(
                        f"SELECT i.id,i.title,{issues.ref_sql('i', 'p')} AS ref FROM issues i "
                        f"JOIN projects p ON p.id=i.project_id WHERE i.id IN ({','.join('?' * len(batch))})", batch):
                    inp, out = ranking[item['id']]
                    top.append(dict(ref=item['ref'], title=item['title'], input_tokens=inp,
                                    output_tokens=out, total_tokens=inp + out))
    top.sort(key=lambda item: (-item['total_tokens'], item['ref']))
    daily = list(rows.values())
    for row in daily:
        row['total_tokens'] = row['input_tokens'] + row['output_tokens']
        row['partial'] = bool(row['unmeasured_run_count'] or row['running_run_count'] or
                              row['unmeasured_duration_run_count'])
    summary = {key: sum(row[key] for row in daily) for key in
               ('created_issue_count', 'completed_issue_count', *_empty())}
    summary['partial'] = any(row['partial'] for row in daily)
    return dict(project=project.upper() if project else None, from_date=start_day.isoformat(),
                to_date=end_day.isoformat(), as_of=now.isoformat(), days=daily, summary=summary,
                top_issues=top[:10], note='현재 보존된 기록 기준이에요. 토큰과 실행 수는 시작일, 시간은 서울 날짜별로 나눠요. 프로젝트 점검은 일별 합계에만 포함하고 작업 순위에서 제외해요. 병렬 실행 시간은 각각 합산해요.')


_STAGES = (
    ('queue', '대기열 대기', 'machine'), ('review', '검토·계획', 'machine'),
    ('execute', '구현', 'machine'), ('qa', 'QA', 'machine'),
    ('completion', '완료 처리', 'machine'), ('plan_wait', '계획 결정 대기', 'human'),
    ('done_wait', '완료 확인 대기', 'human'),
)


def _stage_stats(values, *, percentile=False):
    values = sorted(values)
    if not values:
        return {'mean': None, 'median': None, **({'p90': None} if percentile else {}), 'n': 0}
    size = len(values)
    median = (values[(size - 1) // 2] + values[size // 2]) / 2
    return {'mean': sum(values) / size, 'median': median,
            **({'p90': values[math.ceil(size * .9) - 1]} if percentile else {}), 'n': size}


def _idle_seconds(start, end, intervals):
    """대기 구간에서 다른 실행과 겹친 시간의 합집합을 뺀다."""
    spans = sorted((max(start, a), min(end, b)) for a, b in intervals if a < end and b > start)
    occupied = 0
    cursor = start
    for a, b in spans:
        occupied += max(0, (b - max(a, cursor)).total_seconds())
        cursor = max(cursor, b)
    return max(0, (end - start).total_seconds() - occupied)


def _distribution(values, *, kind, bins=8):
    """값 범위에 맞춘 고정 구간 히스토그램. 시간은 1·2·5 배수 경계, 토큰은 같은 방식의 정수 경계."""
    values = sorted(values)
    if not values:
        return {'unit': kind, 'bins': [], 'n': 0}
    top = values[-1]
    raw = max(top, 1) / bins
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next((m * magnitude for m in (1, 2, 5, 10) if m * magnitude >= raw), 10 * magnitude)
    step = max(1, math.ceil(step))
    rows = [{'from': i * step, 'to': (i + 1) * step, 'count': 0} for i in range(bins)]
    for value in values:
        rows[min(int(value // step), bins - 1)]['count'] += 1
    return {'unit': kind, 'bins': rows, 'n': len(values)}


def stage_summary(from_date=None, to_date=None, project=None, include_human=False, *, as_of=None, agent=None):
    """완료된 부모 Issue의 단계별 시간·토큰과 같은 길이의 직전 기간을 집계한다.

    agent(claude|codex)를 주면 그 provider의 실행·대기열·완료 처리만 세고, 그 실행이 없는 Issue는 뺀다."""
    if agent is not None and agent not in ('claude', 'codex'):
        raise issues.StoreError('agent는 all, claude, codex 중 하나여야 해요.')
    now = _time(as_of) if as_of is not None else _now()
    if now is None:
        raise ValueError('as_of must be an ISO timestamp with a timezone')
    seoul = ZoneInfo('Asia/Seoul')

    def parse(value, default):
        if value is None:
            return default
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise issues.StoreError('날짜는 YYYY-MM-DD 형식이어야 해요.')
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise issues.StoreError('유효한 날짜를 입력해 주세요.')

    end_day = parse(to_date, now.astimezone(seoul).date())
    start_day = parse(from_date, end_day - timedelta(days=min(6, end_day.toordinal() - 1)))
    days = (end_day - start_day).days + 1
    if not 1 <= days <= 366 or end_day == date.max or start_day.toordinal() <= days:
        raise issues.StoreError('조회 기간은 시작일부터 종료일까지 1~366일이어야 해요.')
    previous_start = start_day - timedelta(days=days)
    previous_end = start_day - timedelta(days=1)
    begin = datetime.combine(start_day, time.min, seoul).astimezone(timezone.utc)
    finish = datetime.combine(end_day + timedelta(days=1), time.min, seoul).astimezone(timezone.utc)
    previous_begin = datetime.combine(previous_start, time.min, seoul).astimezone(timezone.utc)
    records = []
    dev_changes = []
    daily_task_durations = {}
    daily_task_tokens = {}
    daily_unmeasured = 0

    with db.connect() as c:
        c.execute('BEGIN')
        project_id = None
        if project:
            found = c.execute('SELECT id FROM projects WHERE key=?', (project.upper(),)).fetchone()
            if found is None:
                raise issues.StoreError('없는 프로젝트예요.', 404)
            project_id = found['id']
        scope = ' AND i.project_id=?' if project_id is not None else ''
        args = (project_id,) if project_id is not None else ()
        bounds = (previous_begin.isoformat(), finish.isoformat(), now.isoformat())
        # 완료 시점으로 후보를 고른다. 이슈의 오래된 실행도 후보에 속하면 모두 센다.
        candidates = c.execute(f'''SELECT i.id,i.title,p.key AS project,{issues.ref_sql('i', 'p')} AS ref,
            i.status,i.last_done_at,
            (SELECT MAX(e.updated_at) FROM issues t JOIN execution_completion e ON e.issue_id=t.id
             WHERE t.parent_id=i.id AND e.phase='complete' AND e.merge_sha<>''
             AND julianday(e.updated_at)<=julianday(?)) AS merged_at,
            (SELECT MAX(ev.created_at) FROM events ev WHERE ev.issue_id=i.id AND ev.kind='status'
             AND json_extract(ev.data_json,'$.to')='done' AND julianday(ev.created_at)<=julianday(?)) AS event_done
            FROM issues i JOIN projects p ON p.id=i.project_id WHERE i.parent_id IS NULL{scope}
            AND (EXISTS(SELECT 1 FROM issues t JOIN execution_completion e ON e.issue_id=t.id
                        WHERE t.parent_id=i.id AND e.phase='complete' AND e.merge_sha<>''
                        AND julianday(e.updated_at)>=julianday(?) AND julianday(e.updated_at)<julianday(?))
                 OR EXISTS(SELECT 1 FROM events ev WHERE ev.issue_id=i.id AND ev.kind='status'
                           AND json_extract(ev.data_json,'$.to')='done'
                           AND julianday(ev.created_at)>=julianday(?) AND julianday(ev.created_at)<julianday(?))
                 OR (i.status='done' AND julianday(i.last_done_at)>=julianday(?)
                     AND julianday(i.last_done_at)<julianday(?)))''',
            (now.isoformat(), now.isoformat(), *args, previous_begin.isoformat(), finish.isoformat(),
             previous_begin.isoformat(), finish.isoformat(), previous_begin.isoformat(), finish.isoformat())).fetchall()
        ids = [r['id'] for r in candidates]
        related = {}
        for offset in range(0, len(ids), 900):
            batch = ids[offset:offset + 900]
            marks = ','.join('?' * len(batch))
            for row in c.execute(f'''SELECT t.id,t.parent_id,t.title,{issues.ref_sql('t', 'p')} AS ref
                FROM issues t JOIN projects p ON p.id=t.project_id
                WHERE t.parent_id IN ({marks}) AND NOT EXISTS
                (SELECT 1 FROM events ev WHERE ev.issue_id=t.id
                 AND json_extract(ev.data_json,'$.superseded_by_plan') IS NOT NULL)''', batch):
                related.setdefault(row['parent_id'], []).append(dict(row))
        child_ids = [t['id'] for tasks in related.values() for t in tasks]
        all_ids = ids + child_ids
        runs = {iid: [] for iid in all_ids}
        jobs = {iid: [] for iid in all_ids}
        completions = {iid: [] for iid in child_ids}
        events = {iid: [] for iid in ids}
        decisions = {iid: [] for iid in ids}
        for offset in range(0, len(all_ids), 900):
            batch = all_ids[offset:offset + 900]
            marks = ','.join('?' * len(batch))
            for row in c.execute(f'SELECT id,issue_id,mode,status,started_at,ended_at,input_tokens,output_tokens,provider,model FROM runs WHERE issue_id IN ({marks}) AND julianday(started_at)<=julianday(?)', (*batch, now.isoformat())):
                if agent is None or row['provider'] == agent:
                    runs[row['issue_id']].append(row)
            for row in c.execute(f'SELECT issue_id,created_at,started_at,provider FROM jobs WHERE issue_id IN ({marks}) AND started_at IS NOT NULL AND julianday(started_at)<=julianday(?)', (*batch, now.isoformat())):
                if agent is None or row['provider'] == agent:
                    jobs[row['issue_id']].append(row)
        for offset in range(0, len(child_ids), 900):
            batch = child_ids[offset:offset + 900]
            marks = ','.join('?' * len(batch))
            for row in c.execute(f'''SELECT e.issue_id,e.run_id,e.updated_at,r.ended_at,e.phase,e.merge_sha,r.provider
                FROM execution_completion e JOIN runs r ON r.id=e.run_id
                WHERE e.issue_id IN ({marks}) AND julianday(e.updated_at)<=julianday(?)''', (*batch, now.isoformat())):
                completions[row['issue_id']].append(row)   # 병합 여부 판단에는 모두 쓰고, 시간 합산만 agent로 거른다.
        for offset in range(0, len(ids), 900):
            batch = ids[offset:offset + 900]
            marks = ','.join('?' * len(batch))
            for row in c.execute(f"SELECT issue_id,created_at,data_json FROM events WHERE issue_id IN ({marks}) AND kind='status' AND julianday(created_at)<=julianday(?)", (*batch, now.isoformat())):
                events[row['issue_id']].append(row)
            for row in c.execute(f"SELECT issue_id,plan_version,verdict,created_at FROM decisions WHERE issue_id IN ({marks}) AND gate='plan' AND julianday(created_at)<=julianday(?)", (*batch, now.isoformat())):
                decisions[row['issue_id']].append(row)
        # 빈 대기열은 모든 프로젝트의 실행과 비교해야 한다.
        oldest_job = min((_time(j['created_at']) for rows in jobs.values() for j in rows), default=previous_begin)
        busy = []
        for row in c.execute('''SELECT started_at,ended_at,status FROM runs
            WHERE julianday(started_at)<julianday(?) AND
            (julianday(ended_at)>julianday(?) OR status='running')''',
            (now.isoformat(), oldest_job.isoformat())):
            a = _time(row['started_at'])
            b = now if row['status'] == 'running' else _time(row['ended_at'])
            if a and b and b >= a:
                busy.append((a, b))

        for row in candidates:
            children = related.get(row['id'], [])
            completed = [e for task in children for e in completions[task['id']]
                         if e['phase'] == 'complete' and e['merge_sha']]
            merged = max((_time(e['updated_at']) for e in completed), default=None)
            done = _time(row['event_done']) or (_time(row['last_done_at']) if row['status'] == 'done' else None)
            done_at = max((t for t in (merged, done) if t), default=None)
            if not done_at or not previous_begin <= done_at < finish or done_at > now:
                continue
            if not done and children and any(not any(e['phase'] == 'complete' and e['merge_sha']
                    for e in completions[task['id']]) for task in children):
                continue
            all_issue_ids = [row['id']] + [t['id'] for t in children]
            if agent and not any(r['mode'] in ('review', 'execute', 'qa') for iid in all_issue_ids for r in runs[iid]):
                continue
            stage = {key: {'seconds': 0, 'tokens': 0} for key, _, _ in _STAGES}
            partial = False
            reasons = []
            execute_runs = failed_runs = 0
            providers = set()
            idle = 0
            for iid in all_issue_ids:
                for run in runs[iid]:
                    mode = run['mode']
                    if mode not in ('review', 'execute', 'qa'):
                        continue
                    if mode == 'execute':
                        execute_runs += 1
                        failed_runs += run['status'] != 'ok'
                    providers.add(f"{run['provider']}/{run['model'] or 'unknown'}")
                    ended = _time(run['ended_at'])
                    seconds = _seconds(run['started_at'], run['ended_at']) if ended and ended <= now else None
                    inp, out = token_count(run['input_tokens']), token_count(run['output_tokens'])
                    if seconds is None or inp is None or out is None or run['status'] == 'running':
                        partial = True
                        reasons.append('실행 시간 또는 토큰 미측정')
                    if seconds is not None:
                        stage[mode]['seconds'] += seconds
                    stage[mode]['tokens'] += (inp or 0) + (out or 0)
                for job in jobs[iid]:
                    seconds = _seconds(job['created_at'], job['started_at'])
                    if seconds is not None:
                        stage['queue']['seconds'] += seconds
                        idle += _idle_seconds(_time(job['created_at']), _time(job['started_at']), busy)
                if iid in completions:
                    for completion in completions[iid]:
                        if completion['phase'] == 'complete' and completion['merge_sha'] and (agent is None or completion['provider'] == agent):
                            seconds = _seconds(completion['ended_at'], completion['updated_at'])
                            if seconds is None:
                                partial = True
                                reasons.append('완료 처리 시간 미측정')
                            else:
                                stage['completion']['seconds'] += seconds
            status_events = [(ev, json.loads(ev['data_json'])) for ev in events[row['id']]]
            plans = [_time(ev['created_at']) for ev, data in status_events if data.get('to') == 'triage']
            approvals = [_time(d['created_at']) for d in decisions[row['id']]
                         if d['verdict'] in ('approve', 'approve_notes')]
            if plans and approvals:
                seconds = _seconds(max(plans).isoformat(), next((a.isoformat() for a in approvals if a >= max(plans)), None))
                stage['plan_wait']['seconds'] = seconds or 0
            if merged and done and done >= merged:
                stage['done_wait']['seconds'] = (done - merged).total_seconds()
            tasks = []
            for task in children:
                task_id = task['id']
                task_runs = runs[task_id]
                task_stages = {key: {'seconds': 0, 'tokens': 0} for key, _, _ in _STAGES}
                task_partial = False
                for run in task_runs:
                    mode = run['mode']
                    if mode not in ('review', 'execute', 'qa'):
                        continue
                    ended = _time(run['ended_at'])
                    seconds = _seconds(run['started_at'], run['ended_at']) if ended and ended <= now else None
                    inp, out = token_count(run['input_tokens']), token_count(run['output_tokens'])
                    task_partial |= seconds is None or inp is None or out is None or run['status'] == 'running'
                    task_stages[mode]['seconds'] += seconds or 0
                    task_stages[mode]['tokens'] += (inp or 0) + (out or 0)
                for job in jobs[task_id]:
                    task_stages['queue']['seconds'] += _seconds(job['created_at'], job['started_at']) or 0
                for completion in completions[task_id]:
                    if completion['phase'] == 'complete' and completion['merge_sha'] and (agent is None or completion['provider'] == agent):
                        task_stages['completion']['seconds'] += _seconds(completion['ended_at'], completion['updated_at']) or 0
                tasks.append({'ref': task['ref'], 'title': task['title'],
                              'execute_runs': sum(r['mode'] == 'execute' for r in task_runs),
                              'failed_runs': sum(r['mode'] == 'execute' and r['status'] != 'ok' for r in task_runs),
                              'stages': task_stages,
                              'total_machine_seconds': sum(task_stages[key]['seconds'] for key, _, kind in _STAGES if kind == 'machine'),
                              'total_tokens': sum(v['tokens'] for v in task_stages.values()),
                              'partial': task_partial})
            machine = sum(stage[key]['seconds'] for key, _, kind in _STAGES if kind == 'machine')
            human = stage['plan_wait']['seconds'] + stage['done_wait']['seconds']
            records.append({'ref': row['ref'], 'title': row['title'], 'project': row['project'],
                            'done_at': done_at.isoformat(), 'task_count': len(children),
                            'execute_runs': execute_runs, 'failed_runs': failed_runs,
                            'provider_models': sorted(providers), 'stages': stage, 'tasks': tasks,
                            'queue_idle_seconds': idle, 'total_machine_seconds': machine,
                            'total_seconds': machine + human, 'total_tokens': sum(v['tokens'] for v in stage.values()),
                            'partial': partial, 'partial_reason': ', '.join(sorted(set(reasons)))})
        for row in c.execute(f'''SELECT {issues.ref_sql('i', 'p')} AS ref,i.title,e.updated_at AS merged_at
            FROM execution_completion e JOIN issues i ON i.id=e.issue_id JOIN projects p ON p.id=i.project_id
            WHERE p.key='DEV' AND e.phase='complete' AND e.merge_sha<>''
             AND e.updated_at=(SELECT MAX(e2.updated_at) FROM execution_completion e2
                               WHERE e2.issue_id=i.id AND e2.phase='complete' AND e2.merge_sha<>'')
            AND julianday(e.updated_at)>=julianday(?) AND julianday(e.updated_at)<julianday(?)
            AND julianday(e.updated_at)<=julianday(?)''',
            (begin.isoformat(), finish.isoformat(), now.isoformat())):
            local_merge = _time(row['merged_at']).astimezone(seoul)
            dev_changes.append({**dict(row), 'date': local_merge.date().isoformat(),
                                'merged_at_local': local_merge.strftime('%Y-%m-%d %H:%M')})
        # 병합일별 Task 전체 시간은 첫 구현 대기열 등록부터 병합 완료까지 잰다.
        measured_tasks = {}
        for row in c.execute('''SELECT i.id,e.updated_at AS merged_at,
            (SELECT MIN(j.created_at) FROM jobs j WHERE j.issue_id=i.id AND j.mode='execute'
             AND julianday(j.created_at)<=julianday(e.updated_at)) AS queued_at
            FROM execution_completion e JOIN issues i ON i.id=e.issue_id
            JOIN projects p ON p.id=i.project_id
            WHERE i.parent_id IS NOT NULL AND e.phase='complete' AND e.merge_sha<>''
            AND e.updated_at=(SELECT MAX(e2.updated_at) FROM execution_completion e2
                              WHERE e2.issue_id=i.id AND e2.phase='complete' AND e2.merge_sha<>'')
            AND julianday(e.updated_at)>=julianday(?) AND julianday(e.updated_at)<julianday(?)
            AND julianday(e.updated_at)<=julianday(?)
            AND (? IS NULL OR i.project_id=?)
            AND (? IS NULL OR EXISTS(SELECT 1 FROM runs rr WHERE rr.issue_id=i.id AND rr.provider=?))''',
            (begin.isoformat(), finish.isoformat(), now.isoformat(), project_id, project_id, agent, agent)):
            merged_at = _time(row['merged_at'])
            day = merged_at.astimezone(seoul).date().isoformat()
            seconds = _seconds(row['queued_at'], row['merged_at']) if row['queued_at'] else None
            if seconds is None:
                daily_unmeasured += 1
            else:
                daily_task_durations.setdefault(day, []).append(seconds)
                measured_tasks[row['id']] = {'day': day, 'merged_at': merged_at, 'tokens': 0}
        task_ids = list(measured_tasks)
        for offset in range(0, len(task_ids), 900):
            batch = task_ids[offset:offset + 900]
            marks = ','.join('?' * len(batch))
            for run in c.execute(f'''SELECT issue_id,started_at,input_tokens,output_tokens FROM runs
                WHERE issue_id IN ({marks}) AND julianday(started_at)<=julianday(?)
                AND (? IS NULL OR provider=?)''',
                (*batch, now.isoformat(), agent, agent)):
                task = measured_tasks[run['issue_id']]
                started = _time(run['started_at'])
                if started and started <= task['merged_at']:
                    task['tokens'] += sum(token_count(run[key]) or 0
                                          for key in ('input_tokens', 'output_tokens'))
        for task in measured_tasks.values():
            daily_task_tokens.setdefault(task['day'], []).append(task['tokens'])

    current = [r for r in records if _time(r['done_at']) >= begin]
    previous = [r for r in records if _time(r['done_at']) < begin]
    current.sort(key=lambda r: (-r['total_machine_seconds'], r['ref']))
    stages = []
    for key, name, kind in _STAGES:
        def stats(rows, field):
            return _stage_stats([r['stages'][key][field] for r in rows if not r['partial']], percentile=field == 'seconds')
        stages.append({'key': key, 'name': name, 'kind': kind,
                       'seconds': stats(current, 'seconds'),
                       'tokens': stats(current, 'tokens') if key in ('review', 'execute', 'qa') else None,
                       'previous': {'seconds': stats(previous, 'seconds'),
                                    'tokens': stats(previous, 'tokens') if key in ('review', 'execute', 'qa') else None}})
    complete = [r for r in current if not r['partial']]
    distribution = {'seconds': _distribution([r['total_machine_seconds'] for r in complete], kind='seconds'),
                    'tokens': _distribution([r['total_tokens'] for r in complete], kind='tokens')}
    daily_task_median = []
    daily_task_tokens_median = []
    for offset in range(days):
        day = (start_day + timedelta(days=offset)).isoformat()
        values = daily_task_durations.get(day, [])
        daily_task_median.append({'date': day, 'seconds': _stage_stats(values)['median'] if values else None,
                                  'n': len(values)})
        tokens = daily_task_tokens.get(day, [])
        daily_task_tokens_median.append({'date': day, 'tokens': _stage_stats(tokens)['median'] if tokens else None,
                                         'n': len(tokens)})
    dev_changes.sort(key=lambda row: (row['merged_at'], row['ref']))
    return {'from_date': start_day.isoformat(), 'to_date': end_day.isoformat(),
            'previous': {'from_date': previous_start.isoformat(), 'to_date': previous_end.isoformat()},
            'project': project.upper() if project else None, 'include_human': bool(include_human),
            'agent': agent or 'all', 'distribution': distribution,
            'stages': stages, 'queue_idle_seconds': _stage_stats([r['queue_idle_seconds'] for r in complete]),
            'issues': current, 'dev_changes': dev_changes, 'daily_task_median': daily_task_median,
            'daily_task_tokens_median': daily_task_tokens_median,
            'daily_task_unmeasured': daily_unmeasured,
            'partial_count': sum(r['partial'] for r in current),
            'note': '현재 보존된 완료 기록 기준이에요. 부분 측정 이슈는 평균에서 제외해요. 병렬 실행 시간은 각각 합산해요.'}


def trend_issues(period='7', project=None, limit=50, offset=0, *, as_of=None):
    """서울 날짜 기간의 Issue·Task 직접 실행과 부모 합계를 한 스냅샷에서 조회한다."""
    if period not in ('7', '30', 'all'):
        raise issues.StoreError('period는 7/30/all 중 하나여야 해요.')
    if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
        raise issues.StoreError('limit는 1~200, offset은 0 이상이어야 해요.')
    now = _time(as_of) if as_of is not None else _now()
    if now is None:
        raise ValueError('as_of must be an ISO timestamp with a timezone')
    local = now.astimezone(ZoneInfo('Asia/Seoul'))
    since = (datetime.combine(local.date() - timedelta(days=int(period) - 1), time.min,
                              tzinfo=ZoneInfo('Asia/Seoul')).astimezone(timezone.utc)
             if period != 'all' else None)
    with db.connect() as c:
        c.execute('BEGIN')
        if project:
            found = c.execute('SELECT id FROM projects WHERE key=?', (project.upper(),)).fetchone()
            if not found:
                raise issues.StoreError('없는 프로젝트예요.', 404)
        rows = [dict(r) for r in c.execute(
            f"SELECT i.id,i.parent_id,i.title,i.status,i.created_at,p.key AS project,{issues.ref_sql('i', 'p')} AS ref "
            "FROM issues i JOIN projects p ON p.id=i.project_id WHERE (? IS NULL OR p.key=?)",
            (project.upper() if project else None, project.upper() if project else None))]
        by_id = {r['id']: r for r in rows}
        for row in rows:
            row.update(_empty())
            row['partial'] = False
            row['last_activity_at'] = row['created_at'] if (stamp := _time(row['created_at'])) and stamp <= now and (since is None or stamp >= since) else None
        for run in c.execute(
                "SELECT r.issue_id,r.status,r.started_at,r.ended_at,r.input_tokens,r.output_tokens "
                "FROM runs r JOIN issues i ON i.id=r.issue_id JOIN projects p ON p.id=i.project_id "
                "WHERE (? IS NULL OR p.key=?) AND (? IS NULL OR datetime(r.started_at)>=datetime(?)) "
                "AND datetime(r.started_at)<=datetime(?)",
                (project.upper() if project else None, project.upper() if project else None,
                 since.isoformat() if since else None, since.isoformat() if since else None, now.isoformat())):
            started = _time(run['started_at'])
            if not started or started > now or (since is not None and started < since):
                continue
            row = by_id[run['issue_id']]
            _add_tokens(row, run)
            row['last_activity_at'] = max(row['last_activity_at'] or run['started_at'], run['started_at'])
            duration = _seconds(run['started_at'], now.isoformat() if run['status'] == 'running' else run['ended_at'])
            row['agent_seconds'] += duration or 0
            row['unmeasured_duration_run_count'] += duration is None
            row['partial'] = bool(row['partial'] or duration is None)
        # 부모에는 현재 자식 Task의 선택 기간 실행분을 한 번씩만 더한다.
        for row in rows:
            parent = by_id.get(row['parent_id'])
            if not parent or not row['run_count']:
                continue
            for key in _empty():
                parent[key] += row[key]
            parent['partial'] = bool(parent['partial'] or row['partial'])
            parent['last_activity_at'] = max(parent['last_activity_at'] or row['last_activity_at'], row['last_activity_at'])
        selected = [r for r in rows if r['last_activity_at']]
        selected.sort(key=lambda r: (-_time(r['last_activity_at']).timestamp(), r['ref']))
        for row in selected:
            row['partial'] = bool(row['partial'] or row['unmeasured_run_count'] or row['running_run_count'] or row['unmeasured_duration_run_count'])
            row.pop('id'); row.pop('parent_id')
        return {'as_of': now.isoformat(), 'period': period, 'project': project.upper() if project else None,
                'total': len(selected), 'limit': limit, 'offset': offset, 'has_more': offset + limit < len(selected),
                'issues': selected[offset:offset + limit]}


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
