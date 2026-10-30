"""Task 실행 진행 단계(DEV-206) — 실행 스레드가 구현·전체 검사·재작업·다시 검사로 넘어갈 때마다 남기고, 화면은 짧은 표시와 ⓘ 상세로 본다.
메모리에만 둔다 — dev가 재시작되면 도는 실행은 어차피 orphaned가 된다. In Progress 경과 시간(DEV-208)은 DB에서 읽는다."""
import re
import threading
import time
from datetime import datetime

import db
import test_runner

_lock = threading.Lock()
_runs = {}     # ref → {'phase', 'since', 'attempt', 'provider', 'model', 'limit', 'failed'}
_checks = {}   # ref → {'total', 'done', 'failed', 'running': {검사 이름: 시작 시각}}

SHORT = {'implement': '구현 중', 'checks': '전체 검사 중', 'rework': '재작업 중', 'recheck': '다시 검사 중', 'recovery': '복구 실행 중'}
STALL_GRACE_SEC = 120          # 모델 호출이 시간 제한을 이만큼 넘겨도 끝나지 않으면 멈춘 것으로 본다
CHECKS_SILENT_SEC = 30 * 60    # 검사 목록도 못 받은 채 이만큼 지나면 멈춘 것으로 본다


def set_phase(ref, phase, **info):
    """단계를 바꾼다. 구현·재작업으로 들어가면 앞 검사 진행은 지운다."""
    with _lock:
        _runs[ref] = {'phase': phase, 'since': time.time(), **info}
        if phase in ('implement', 'rework', 'recovery'):
            _checks.pop(ref, None)


def clear(ref):
    with _lock:
        _runs.pop(ref, None)
        _checks.pop(ref, None)


def checks_begin(ref, total):
    with _lock:
        if ref in _runs:
            _checks[ref] = {'total': total, 'done': 0, 'failed': 0, 'running': {}}


def check_start(ref, name):
    with _lock:
        if ref in _checks:
            _checks[ref]['running'][name] = time.time()


def check_end(ref, name, ok):
    with _lock:
        if ref in _checks:
            c = _checks[ref]
            c['running'].pop(name, None)
            c['done'] += 1
            c['failed'] += 0 if ok else 1


def failed_names(failure_text: str) -> list[str]:
    """orchestrate.run_tests의 실패 글에서 검사 파일 이름만 뽑는다."""
    return list(dict.fromkeys(re.findall(r'(test_[\w.-]+\.(?:py|js)): 종료 코드', failure_text or '')))


def _clock(ts):
    return datetime.fromtimestamp(ts).strftime('%H:%M')


def _span(sec):
    sec = int(max(0, sec))
    return f'{sec // 60}분' if sec >= 60 else f'{sec}초'


def view(ref, now=None) -> dict | None:
    """화면용 {phase, short, long, stalled}. 도는 실행이 없으면 None."""
    now = now or time.time()
    with _lock:
        run = dict(_runs[ref]) if ref in _runs else None
        checks = {**_checks[ref], 'running': dict(_checks[ref]['running'])} if ref in _checks else None
    if not run:
        return None
    phase, elapsed = run['phase'], now - run['since']
    who = ' '.join(x for x in (run.get('provider'), run.get('model')) if x)
    lines, stalled = [], False
    if phase in ('implement', 'rework', 'recovery'):
        limit = run.get('limit') or 0
        head = {'implement': f"구현 중 ({run.get('attempt', 1)}차)", 'rework': f"재작업 중 ({run.get('attempt', 2)}차 · 마지막 시도)",
                'recovery': '복구 실행 중'}[phase]
        lines += [head, f"{who + ' · ' if who else ''}시작 {_clock(run['since'])} · 경과 {_span(elapsed)}" + (f' / 시간 제한 {_span(limit)}' if limit else '')]
        if phase == 'rework' and run.get('failed'):
            lines.append('앞선 전체 검사에서 실패: ' + ', '.join(run['failed']))
        stalled = bool(limit) and elapsed > limit + STALL_GRACE_SEC
    else:
        head = '전체 검사 중' if phase == 'checks' else '다시 검사 중 (재작업 뒤)'
        lines += [head, f"완료 등록 전에 서버가 Task worktree에서 전체 검사를 돌려요 · 시작 {_clock(run['since'])} · 경과 {_span(elapsed)}"]
        if checks:
            lines.append(f"{checks['done']}/{checks['total']}개 끝" + (f" (실패 {checks['failed']}개)" if checks['failed'] else ''))
            per = test_runner.TIMEOUT_SEC + test_runner.PIPE_GRACE_SEC * 2 + 60
            for name, since in sorted(checks['running'].items(), key=lambda kv: kv[1]):
                lines.append(f"지금: {name} ({_span(now - since)}째)")
                stalled = stalled or now - since > per
            lines.append(f'검사 하나의 시간 제한은 {_span(test_runner.TIMEOUT_SEC)}이에요.')
        else:
            stalled = elapsed > CHECKS_SILENT_SEC
    if phase in ('implement', 'rework', 'checks', 'recheck'):
        lines.append('시간 제한은 시도마다 걸려요 — 검사가 실패하면 한 번 재작업하므로 전체는 제한의 두 배를 넘을 수 있어요.')
    short = SHORT[phase] if phase != 'rework' else '재작업 중 (2차)'
    if stalled:
        short = '⚠️ 멈춘 것 같아요'
        lines.insert(0, f'⚠️ {SHORT[phase]} 단계가 예상 시간을 넘겼는데 끝나지 않았어요 — 실행 로그와 남은 프로세스를 확인해 주세요.')
    return {'phase': phase, 'short': short, 'long': '\n'.join(lines), 'stalled': stalled}


def elapsed(items) -> None:
    """In Progress 이슈마다 `in_progress_seconds`를 채운다(DEV-208) — 도는 실행이 있으면 그 실행 전체의 시작부터(단계가 바뀌어도
    이어서), 없으면 마지막으로 In Progress가 된 때부터. 화면은 받은 뒤 흐른 시간만 더해 브라우저 시계와 어긋나지 않는다."""
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
