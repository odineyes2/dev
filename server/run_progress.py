"""Task 실행 진행 단계(DEV-206) — 실행 스레드가 구현·검사·재작업으로 넘어갈 때 남기고, 화면은 짧은 표시와 ⓘ 상세로 본다.
메모리에만 둔다 — dev가 재시작되면 도는 실행은 어차피 orphaned가 된다. In Progress 경과 시간(DEV-208)은 DB에서 읽는다."""
import threading
import time
from datetime import datetime

import db

_lock = threading.Lock()
_runs = {}     # ref → {'phase', 'since', 'attempt', 'provider', 'model', 'limit'}

STALL_GRACE_SEC = 120          # 모델 호출이 시간 제한을 이만큼 넘겨도 끝나지 않으면 멈춘 것으로 본다
SHORT = {'implement': '구현 중', 'checks': '검사 중', 'rework': '재작업 중',
         'check_failed': '검사 실패 · 수정 대기', 'check_passed': '검사 통과 · 결과 정리 중'}


def set_phase(ref, phase, **info):
    """단계를 바꾼다. `started`(실행 전체의 시작)는 같은 ref에서 처음 부를 때만 정하고 재작업 전환에도 이어진다(DEV-240)."""
    now = time.time()
    with _lock:
        previous = _runs.get(ref, {})
        started = previous.get('started', now) if previous.get('run_id') in (None, info.get('run_id')) else now
        _runs[ref] = {'phase': phase, 'since': now, **info, 'started': started}


def begin_checks(ref, run_id):
    """에이전트 요청·완료 검사를 같은 단계로 표시하고 기존 실행 정보를 보존한다."""
    now = time.time()
    with _lock:
        previous = _runs.get(ref, {})
        if previous.get('run_id') not in (None, run_id):
            return None
        ticket = {**previous, 'phase': 'checks', 'run_id': run_id, 'since': now,
                  'started': previous.get('started', now), 'failed': []}
        _runs[ref] = ticket
        return ticket


def finish_checks(ref, ticket, ok, report='', *, active=True):
    """종료·새 실행 뒤에 도착한 이전 검사 응답은 화면 상태를 되살리지 않는다."""
    with _lock:
        if ticket is None or _runs.get(ref) is not ticket:
            return
        if not active:
            _runs.pop(ref, None)
            return
        _runs[ref] = {**ticket, 'phase': 'check_passed' if ok else 'check_failed',
                      'since': time.time(), 'failed': [] if ok else failed_checks(report)}


def failed_checks(report) -> list[str]:
    """task_checks 보고의 `- ❌ {label} · …` 줄에서 실패한 검사 label만 뽑는다(DEV-240)."""
    out = []
    for line in (report or '').splitlines():
        if line.startswith('- ❌ '):
            out.append(line[4:].split(' · ', 1)[0].strip().strip('`'))
    return out


def clear(ref, run_id=None):
    with _lock:
        if run_id is None or _runs.get(ref, {}).get('run_id') in (None, run_id):
            _runs.pop(ref, None)


def _clock(ts):
    return datetime.fromtimestamp(ts).strftime('%H:%M')


def _span(sec):
    sec = int(max(0, sec))
    return f'{sec // 60}분' if sec >= 60 else f'{sec}초'


def for_issue(it) -> dict | None:
    """이슈 API에 싣는 진행 — In Progress면 실행 단계."""
    return view(it['ref']) if it.get('status') == 'in_progress' else None


def view(ref, now=None) -> dict | None:
    """화면용 {phase, short, long, stalled}. 도는 실행이 없으면 None."""
    now = now or time.time()
    with _lock:
        run = dict(_runs[ref]) if ref in _runs else None
    if not run:
        return None
    phase, elapsed = run.get('phase', 'implement'), now - run['since']
    who = ' '.join(x for x in (run.get('provider'), run.get('model')) if x)
    limit = run.get('limit') or 0 if phase != 'checks' else 0   # 검사는 명령마다 따로 시간 제한이 걸린다
    lines = [SHORT.get(phase, '구현 중'), f"{who + ' · ' if who else ''}시작 {_clock(run['since'])} · 경과 {_span(elapsed)}" + (f' / 시간 제한 {_span(limit)}' if limit else '')]
    stalled = bool(limit) and elapsed > limit + STALL_GRACE_SEC
    short = SHORT.get(phase, '구현 중')
    if stalled:
        short = '⚠️ 멈춘 것 같아요'
        lines.insert(0, f'⚠️ {SHORT.get(phase, "구현 중")} 단계가 예상 시간을 넘겼는데 끝나지 않았어요 — 실행 로그와 남은 프로세스를 확인해 주세요.')
    return {'phase': phase, 'short': short, 'long': '\n'.join(lines), 'stalled': stalled,
            'failed': list(run.get('failed') or []), 'seconds': int(max(0, now - run.get('started', run['since'])))}


def elapsed(items) -> None:
    """In Progress 이슈마다 `in_progress_seconds`를 채운다(DEV-208) — 도는 실행이 있으면 그 실행 전체의 시작부터,
    없으면 마지막으로 In Progress가 된 때부터. 화면은 받은 뒤 흐른 시간만 더해 브라우저 시계와 어긋나지 않는다."""
    ids = [it['id'] for it in items if it.get('status') == 'in_progress']
    for it in items:
        it['in_progress_seconds'] = None
    if not ids:
        return
    marks = ','.join('?' * len(ids))
    with db.connect() as c:
        runs = dict(c.execute(f"SELECT issue_id, MIN(started_at) FROM runs WHERE status='running' AND issue_id IN ({marks}) GROUP BY issue_id", ids).fetchall())
        moved = dict(c.execute(f"""SELECT issue_id, MAX(created_at) FROM events WHERE kind='status' AND issue_id IN ({marks})
            AND json_extract(data_json,'$.to')='in_progress' GROUP BY issue_id""", ids).fetchall())
    now = time.time()
    for it in items:
        since = runs.get(it['id']) or moved.get(it['id'])
        if it.get('status') == 'in_progress' and since:
            it['in_progress_seconds'] = max(0, int(now - datetime.fromisoformat(since).timestamp()))
