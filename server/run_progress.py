"""Task 실행 진행 단계(DEV-206) — 실행 스레드가 구현·전체 검사·재작업·다시 검사로 넘어갈 때마다 남기고, 화면은 짧은 표시와 ⓘ 상세로 본다.
병합 검사(server_merge)도 같은 n/N·ⓘ로 보인다(DEV-211) — 실행 단계가 없으므로 따로 `_merges`에 시작 시각을 둔다.
검사 전 준비(임시 DB·웹 빌드)는 `_prep`에 두어 "준비 중: …"으로 보이고, 검사 목록이 정해지면 지운다(DEV-212).
메모리에만 둔다 — dev가 재시작되면 도는 실행은 어차피 orphaned가 된다. In Progress 경과 시간(DEV-208)은 DB에서 읽는다."""
import re
import threading
import time
from datetime import datetime

import db
import test_runner

_lock = threading.Lock()
_runs = {}     # ref → {'phase', 'since', 'attempt', 'provider', 'model', 'limit', 'failed'}
_checks = {}   # ref → {'total', 'done', 'failed', 'failed_names', 'reused', 'skipped', 'running': {검사 이름: (시작 시각, 제한)}}
_merges = {}   # ref → 병합 검사 시작 시각(DEV-211)
_prep = {}     # ref → (준비 이름, 시작 시각)(DEV-212)

SHORT = {'implement': '구현 중', 'checks': '전체 검사 중', 'rework': '재작업 중', 'recheck': '다시 검사 중', 'recovery': '복구 실행 중'}
STALL_GRACE_SEC = 120          # 모델 호출이 시간 제한을 이만큼 넘겨도 끝나지 않으면 멈춘 것으로 본다
CHECKS_SILENT_SEC = 30 * 60    # 검사 목록도 못 받은 채 이만큼 지나면 멈춘 것으로 본다


def set_phase(ref, phase, **info):
    """단계를 바꾼다. 구현·재작업으로 들어가면 앞 검사 진행은 지운다."""
    with _lock:
        _runs[ref] = {'phase': phase, 'since': time.time(), **info}
        _prep.pop(ref, None)
        if phase in ('implement', 'rework', 'recovery'):
            _checks.pop(ref, None)


def clear(ref):
    with _lock:
        _runs.pop(ref, None)
        _checks.pop(ref, None)
        _prep.pop(ref, None)


def prepare(ref, label, merge=False):
    """검사 전 준비를 시작한다(임시 DB·웹 빌드). 도는 실행이나 병합 검사가 있을 때만 남긴다."""
    with _lock:
        if not ref:
            return
        if merge:
            _merges.setdefault(ref, time.time())
        if ref in _runs or merge:
            _prep[ref] = (label, time.time())


def prepare_end(ref):
    with _lock:
        _prep.pop(ref, None)


def checks_begin(ref, total, reused=0, skipped=0, merge=False):
    """검사 목록이 정해졌을 때 부른다. total은 실제로 돌릴 수, reused·skipped는 이전 결과 재사용·꺼서 건너뛴 수.
    merge면 실행 단계가 없어도 병합 검사 진행으로 남긴다."""
    with _lock:
        _prep.pop(ref, None)
        if merge and ref:
            _merges.setdefault(ref, time.time())   # 준비부터 이어서 센다
        if ref in _runs or merge and ref:
            _checks[ref] = {'total': total, 'done': 0, 'failed': 0, 'failed_names': [], 'reused': reused, 'skipped': skipped, 'running': {}}


def merge_end(ref):
    """병합 검사가 끝나면 진행을 지운다 — 다음 표시는 병합 단계 문구(운영 반영 중 등)로 넘어간다."""
    with _lock:
        if _merges.pop(ref, None) is not None and ref not in _runs:
            _checks.pop(ref, None)
            _prep.pop(ref, None)


def check_start(ref, name, limit=None):
    """limit은 그 검사의 시간 제한(초) — 브라우저 검사는 더 길다(DEV-209)."""
    with _lock:
        if ref in _checks:
            _checks[ref]['running'][name] = (time.time(), limit or test_runner.TIMEOUT_SEC)


def check_end(ref, name, ok):
    with _lock:
        if ref in _checks:
            c = _checks[ref]
            c['running'].pop(name, None)
            c['done'] += 1
            c['failed'] += 0 if ok else 1
            if not ok:
                c['failed_names'].append(name)


def failed_names(failure_text: str) -> list[str]:
    """orchestrate.run_tests의 실패 글에서 검사 파일 이름만 뽑는다."""
    return list(dict.fromkeys(re.findall(r'(test_[\w.-]+\.(?:py|js)): 종료 코드', failure_text or '')))


def _clock(ts):
    return datetime.fromtimestamp(ts).strftime('%H:%M')


def _span(sec):
    sec = int(max(0, sec))
    return f'{sec // 60}분' if sec >= 60 else f'{sec}초'


def _count(checks):
    """짧은 표시 뒤에 붙는 ` (6/67)` · ` (6/67 · 실패 1)`."""
    if not checks:
        return ''
    return f" ({checks['done']}/{checks['total']}" + (f" · 실패 {checks['failed']}" if checks['failed'] else '') + ')'


def _check_lines(checks, now):
    """ⓘ의 검사 진행 줄과 멈춤 여부."""
    lines, stalled = [], False
    extra = [f'{label} {n}개' for label, n in (('재사용', checks.get('reused')), ('건너뜀', checks.get('skipped'))) if n]
    lines.append(f"{checks['done']}/{checks['total']}개 끝" + (f" (실패 {checks['failed']}개)" if checks['failed'] else '')
                 + (f" · 그 밖에 {', '.join(extra)}" if extra else ''))
    if checks.get('failed_names'):
        lines.append('실패: ' + ', '.join(checks['failed_names']))
    for name, (since, limit) in sorted(checks['running'].items(), key=lambda kv: kv[1][0]):
        lines.append(f"지금: {name} ({_span(now - since)}째 / 제한 {_span(limit)})")
        stalled = stalled or now - since > limit + test_runner.PIPE_GRACE_SEC * 2 + 60
    lines.append(f'검사 하나의 시간 제한은 {_span(test_runner.TIMEOUT_SEC)}, 브라우저 검사는 {_span(test_runner.BROWSER_TIMEOUT_SEC)}이에요.')
    return lines, stalled


def _prep_line(ref, now):
    """(짧은 표시, ⓘ 줄) — 준비 중이 아니면 None."""
    with _lock:
        prep = _prep.get(ref)
    if not prep:
        return None
    return f'준비 중: {prep[0]}', f'준비 중: {prep[0]} ({_span(now - prep[1])}째) — 끝나면 검사를 시작해요.'


def _snapshot(ref):
    with _lock:
        return {**_checks[ref], 'running': dict(_checks[ref]['running']), 'failed_names': list(_checks[ref]['failed_names'])} if ref in _checks else None


def merge_view(ref, now=None) -> dict | None:
    """병합 검사 화면용 {phase:'merge', short, long, stalled}(DEV-211). 병합 검사가 돌지 않으면 None."""
    now = now or time.time()
    with _lock:
        since = _merges.get(ref)
    checks, prep = _snapshot(ref), _prep_line(ref, now)
    if since is None or not checks and not prep:
        return None
    lines = ['병합 검사 중', f'시험 병합 사본에서 선택된 검사를 돌려요 — 운영 코드는 통과한 뒤에만 바뀌어요 · 시작 {_clock(since)} · 경과 {_span(now - since)}']
    if checks:
        more, stalled = _check_lines(checks, now)
        lines += more + ['병합 검사에서 실패한 검사는 섞임을 가리려고 2번 더 돌려요.']
        short = '병합 검사 중' + _count(checks)
    else:
        lines.append(prep[1])
        short, stalled = prep[0], now - since > CHECKS_SILENT_SEC
    if stalled:
        short = '⚠️ 멈춘 것 같아요'
        lines.insert(0, '⚠️ 병합 검사가 예상 시간을 넘겼는데 끝나지 않았어요 — 실행 로그와 남은 프로세스를 확인해 주세요.')
    return {'phase': 'merge', 'short': short, 'long': '\n'.join(lines), 'stalled': stalled}


def for_issue(it) -> dict | None:
    """이슈 API에 싣는 진행 — 병합 검사 중이면 병합 검사 진행, In Progress면 실행 단계."""
    if it.get('merge_state') == '병합 검사 중':
        return merge_view(it['ref'])
    return view(it['ref']) if it.get('status') == 'in_progress' else None


def view(ref, now=None) -> dict | None:
    """화면용 {phase, short, long, stalled}. 도는 실행이 없으면 None."""
    now = now or time.time()
    with _lock:
        run = dict(_runs[ref]) if ref in _runs else None
    checks = _snapshot(ref)
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
        prep = _prep_line(ref, now)
        if checks:
            more, stalled = _check_lines(checks, now)
            lines += more
        else:
            lines += [prep[1]] if prep else []
            stalled = elapsed > CHECKS_SILENT_SEC
    if phase in ('implement', 'rework', 'checks', 'recheck'):
        lines.append('시간 제한은 시도마다 걸려요 — 검사가 실패하면 한 번 재작업하므로 전체는 제한의 두 배를 넘을 수 있어요.')
    short = SHORT[phase] + (_count(checks) if phase in ('checks', 'recheck') else '') if phase != 'rework' else '재작업 중 (2차)'
    if phase in ('checks', 'recheck') and not checks and prep:
        short = prep[0]
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
