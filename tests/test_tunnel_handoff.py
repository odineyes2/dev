"""공용 터널 무중단 전환(DEV-255) — 모의 커넥터로 순서(임시 준비→교체·재시작→서비스 준비→임시 정리), 준비 실패 시 무변경, 전환 실패 시 되돌린 뒤 정리, 정상 종료·강제 종료, 남은 임시 정리 조건, 연결 해제 단계 표시를 검사한다. 진짜 cloudflared·서비스는 부르지 않는다."""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix='dev-handoff-'))
os.environ['DEV_DATA_DIR'] = str(TMP / 'data')
os.environ['DEV_DB_PATH'] = str(TMP / 'data' / 'dev.db')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))

import db  # noqa: E402
import issues  # noqa: E402
import project_service  # noqa: E402
import tunnel_handoff as th  # noqa: E402

db.init()
th.READY_SEC = 1
th.time.sleep = lambda s: None  # 폴링 대기를 없앤다(시간 상한은 monotonic이 지킨다)


def check(cond, label):
    if not cond:
        raise AssertionError(label)
    print('ok', label)


OLD = b'tunnel: t1\ningress:\n- hostname: dev.lomebrote.com\n  service: http://127.0.0.1:8300\n- service: http_status:404\n'
NEW = b'tunnel: t1\nmetrics: 127.0.0.1:20241\ngrace-period: 1s\ningress:\n- service: http_status:404\n'
CFG = TMP / 'tunnel' / 'config.yml'
CFG.parent.mkdir(parents=True)


class FakeProcess:
    def __init__(self, exit_code=None, stubborn=False):
        self.pid, self.returncode, self.stubborn, self.killed = 4242, exit_code, stubborn, False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None and self.stubborn and not self.killed:
            raise subprocess.TimeoutExpired('cloudflared', timeout)
        self.returncode = 0
        return 0

    def kill(self):
        self.killed = True
        self.returncode = 1


def scenario(temp='ok', service='ok', restart_fails=False, early_exit=False):
    """events에 순서를 남기는 모의 환경을 꾸민다. service: ok|stale(재시작 뒤에도 옛/임시 ID만)|ok_after_rollback."""
    CFG.write_bytes(OLD)
    events, state = [], {'restarts': 0, 'process': None}

    def start(exe, raw, config_file, tunnel):
        events.append('start')
        temp_file = Path(config_file).parent / f'{th.MARK}{time.time_ns()}.yml'
        temp_file.write_bytes(raw)
        state['process'] = FakeProcess(exit_code=3 if early_exit else None)
        state['temp_file'] = temp_file
        return th.Replica(state['process'], temp_file, '127.0.0.1:20300', open(os.devnull, 'wb'))

    def ready(address):
        if address == '127.0.0.1:20300':
            if temp == 'ok' and state['process'].returncode is None:
                return {'connectorId': 'temp', 'readyConnections': 4}
            return None
        # 서비스 metrics
        if state['restarts'] == 0:
            return {'connectorId': 'old', 'readyConnections': 4}
        if service == 'ok' or (service == 'ok_after_rollback' and state['restarts'] >= 2):
            return {'connectorId': f"svc{state['restarts']}", 'readyConnections': 4}
        return {'connectorId': 'old', 'readyConnections': 4}

    def replace(path, raw, expected):
        events.append('replace:' + ('new' if raw == NEW else 'old'))
        project_service._replace(path, raw, expected)

    def restart():
        state['restarts'] += 1
        events.append('restart')
        if restart_fails and state['restarts'] == 1:
            raise issues.StoreError('터널 서비스 재시작에 실패했어요.', 409)

    real_stop = th.stop

    def stop(replica):
        events.append('stop')
        real_stop(replica)

    th.start, th.ready, th.stop = start, ready, stop
    th._signal = lambda pid: events.append('signal') or True
    return events, state, replace, restart


def run(events, replace, restart):
    phases = []
    try:
        th.switch('cloudflared', CFG, NEW, th._hash(OLD), 't1', replace, restart, phase=phases.append)
        return None, phases
    except issues.StoreError as e:
        return str(e), phases


# 1) 정상: 임시 준비 → 교체 → 재시작 → 서비스(새 ID) 준비 → 임시 정상 종료
events, state, replace, restart = scenario()
error, phases = run(events, replace, restart)
check(error is None, '정상 전환 성공')
check(events == ['start', 'replace:new', 'restart', 'stop', 'signal'], f'순서 {events}')
check(events.index('replace:new') > events.index('start') and events.index('stop') > events.index('restart'), '임시 준비 뒤 교체, 재시작 뒤 정리')
check(CFG.read_bytes() == NEW, '새 설정 적용')
check(not state['temp_file'].exists(), '임시 설정 사본 삭제')
check(phases[0] == '임시 커넥터 준비' and phases[-1] == '임시 커넥터 정리' and '서비스 준비 확인' in phases, f'진행 표시 {phases}')

# 2) 임시가 붙지 않으면 설정·서비스 그대로
events, state, replace, restart = scenario(temp='never')
error, _ = run(events, replace, restart)
check(error and '바꾸지 않았어요' in error, f'임시 준비 실패 메시지: {error}')
check('restart' not in events and not any(e.startswith('replace') for e in events), '임시 준비 실패 시 교체·재시작 없음')
check(CFG.read_bytes() == OLD and 'stop' in events and not state['temp_file'].exists(), '설정 그대로·임시 정리')

# 3) 임시가 바로 죽어도 같음
events, state, replace, restart = scenario(early_exit=True)
error, _ = run(events, replace, restart)
check(error and '바로 종료' in error and 'restart' not in events and CFG.read_bytes() == OLD, '임시 조기 종료 시 무변경')

# 4) 재시작 뒤 서비스가 새 ID로 안 붙으면(옛·임시 ID만) 되돌린 다음 임시를 내린다
events, state, replace, restart = scenario(service='ok_after_rollback')
error, _ = run(events, replace, restart)
check(error and '제한 시간' in error and '되돌렸어요' in error, f'서비스 준비 실패 → 되돌림: {error}')
check(events == ['start', 'replace:new', 'restart', 'replace:old', 'restart', 'stop', 'signal'], f'되돌림이 임시 정리보다 먼저 {events}')
check(CFG.read_bytes() == OLD, '원래 설정 복구')

# 5) 재시작 명령이 실패해도 되돌림 뒤 정리
events, state, replace, restart = scenario(restart_fails=True)
error, _ = run(events, replace, restart)
check(error and '재시작에 실패' in error and '되돌렸어요' in error and events[-1] in ('stop', 'signal') and CFG.read_bytes() == OLD, '재시작 실패 → 되돌림·정리')

# 6) 되돌림도 실패하면 그렇게 알린다(임시는 그래도 정리)
events, state, replace, restart = scenario(service='stale')
error, _ = run(events, replace, restart)
check(error and '되돌리지 못했어요' in error and 'stop' in events, '이중 실패 알림·임시 정리')

# 7) 다른 작업이 설정을 바꿨으면 시작도 안 한다
events, state, replace, restart = scenario()
CFG.write_bytes(OLD + b'# changed\n')
error, _ = run(events, replace, restart)
check(error and '덮어쓰지 않고' in error and events == [], '해시 불일치 시 아무것도 안 함')

# 8) stop: 정상 종료가 안 되면 강제 종료, 사본은 언제나 삭제
temp_file = CFG.parent / f'{th.MARK}{time.time_ns()}.yml'
temp_file.write_bytes(b'x')
stubborn = FakeProcess(stubborn=True)
th._signal = lambda pid: True
from importlib import reload  # noqa: E402
real = reload(th)  # 원래 stop을 쓰기 위해 다시 읽는다
real.time.sleep = lambda s: None
real._signal = lambda pid: True
real.stop(real.Replica(stubborn, temp_file, '127.0.0.1:20300', open(os.devnull, 'wb')))
check(stubborn.killed and not temp_file.exists(), '정상 종료 실패 시 강제 종료·사본 삭제')

# 9) 서비스 설정 준비값과 metrics 주소
data = real.prepare({'tunnel': 't1', 'ingress': []})
check(data['metrics'] == '127.0.0.1:20241' and data['grace-period'] == '1s', '없으면 metrics·grace-period 추가')
data = real.prepare({'tunnel': 't1', 'metrics': '127.0.0.1:20250', 'grace-period': '5s'})
check(data['metrics'] == '127.0.0.1:20250' and data['grace-period'] == '5s', '있으면 그대로')
check(real.metrics_address(OLD) == '127.0.0.1:20241', 'metrics 없으면 기본 주소')
try:
    real.metrics_address(b'tunnel: t1\nmetrics: 0.0.0.0:2000\n')
    check(False, '루프백 밖 metrics 거절')
except issues.StoreError:
    check(True, '루프백 밖 metrics 거절')

# 10) 남은 임시 정리: 젊은 임시(진행 중 전환일 수 있음)는 건드리지 않고, 서비스가 안 붙었으면 남긴다
now_ns = time.time_ns()
old_line = f'C:/cloudflared.exe tunnel --config C:/x/{real.MARK}{now_ns - 3600 * 10**9}.yml run t1'
young_line = f'C:/cloudflared.exe tunnel --config C:/x/{real.MARK}{now_ns}.yml run t1'
check(real._born(old_line) and time.time() - real._born(old_line) > 3000, '사본 이름에서 생성 시각 읽기')
real.strays = lambda min_age=real.STALE_SEC: [11]
real.ready = lambda address: None
signals = []
real._signal = lambda pid: signals.append(pid) or True
note = real.cleanup(CFG, '127.0.0.1:20241')
check('남겨 뒀어요' in note and not signals, '서비스가 안 붙었으면 임시를 남김')
gone = []
real.strays = lambda min_age=real.STALE_SEC: [] if gone else [11]
real.ready = lambda address: {'connectorId': 'svc', 'readyConnections': 4}
real._signal = lambda pid: signals.append(pid) or gone.append(pid) or True
stale_file = CFG.parent / f'{real.MARK}{now_ns - 3600 * 10**9}.yml'
young_file = CFG.parent / f'{real.MARK}{now_ns}.yml'
stale_file.write_bytes(b'x')
young_file.write_bytes(b'x')
check(real.cleanup(CFG, '127.0.0.1:20241') == '' and signals == [11], '서비스가 붙어 있으면 남은 임시를 정상 종료')
check(not stale_file.exists() and young_file.exists(), '오래된 사본만 삭제')
young_file.unlink()

# 11) 연결 해제 단계 표시
project = {'service_domain': '', 'service_port': 0}
record = {'action': 'disconnect', 'step': 5, 'status': 'running', 'phase': '서비스 준비 확인', 'error': ''}
steps = project_service._steps(project, record)
check([s['status'] for s in steps] == ['done', 'done', 'running'] and '서비스 준비 확인' in steps[2]['label'], f'해제 진행 중 단계 {steps}')
steps = project_service._steps(project, {**record, 'status': 'failed', 'step': 3, 'error': '검사 실패'})
check([s['status'] for s in steps] == ['done', 'failed', 'pending'] and steps[1]['error'] == '검사 실패', '해제 실패 단계·이유')
steps = project_service._steps(project, {**record, 'status': 'disconnected'})
check(all(s['status'] == 'done' for s in steps), '해제 완료 단계')
steps = project_service._steps(project, {**record, 'stop_apps': 1, 'step': 6})
check([s['status'] for s in steps] == ['done', 'done', 'done', 'running'] and steps[3]['label'] == '앱 멈추기', '앱 정지는 네 번째 단계')
steps = project_service._steps(project, {**record, 'stop_apps': 1, 'step': 6, 'status': 'disconnected', 'app_error': '앱 정지 실패'})
check([s['status'] for s in steps] == ['done', 'done', 'done', 'failed'] and steps[3]['error'] == '앱 정지 실패', '앱 실패여도 터널 해제는 완료')

# 12) 복구: 설정이 이미 원본이고 서비스가 붙어 있으면 재시작하지 않는다
with db.connect() as c:
    c.execute("INSERT INTO projects(key,name,created_at) VALUES('HX','hx',?)", (db.now_iso(),))
    pid = c.execute("SELECT id FROM projects WHERE key='HX'").fetchone()[0]
    c.execute("INSERT INTO project_services(project_id,status,actor,action,updated_at) VALUES(?, 'failed','human:admin','disconnect',?)", (pid, db.now_iso()))
backup = TMP / 'backup.yml'
backup.write_bytes(OLD)
CFG.write_bytes(OLD)
switched = []
project_service._switch = lambda *a: switched.append(a)
project_service.tunnel_handoff.ready = lambda address: {'connectorId': 'svc', 'readyConnections': 4}
row = {'project_id': pid, 'backup_path': str(backup), 'applied_hash': 'h', 'config_path': str(CFG)}
check('유지' in project_service._restore(row) and not switched, '원본 그대로·서비스 정상이면 재시작 없음')
CFG.write_bytes(NEW)
project_service._restore({**row, 'applied_hash': real._hash(NEW)})
check(switched and switched[0][2] == OLD, '설정이 바뀌어 있으면 무중단 전환으로 복구')

print('all passed')
