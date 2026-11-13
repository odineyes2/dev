"""공용 터널 무중단 전환(DEV-255) — 새 설정의 임시 커넥터를 먼저 붙이고 서비스를 재시작한 뒤, 서비스가 다시 붙은 것을 보고 임시를 정상 종료로 내린다."""
import hashlib
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import yaml

import config
import issues

MARK = '.dev-handoff-'
# 서비스 설정에 metrics가 없으면 이 주소를 넣는다. cloudflared 기본값(20241~)과 같은 첫 포트다.
SERVICE_METRICS = os.environ.get('DEV_TUNNEL_METRICS') or '127.0.0.1:20241'
GRACE = '1s'
READY_SEC = int(os.environ.get('DEV_TUNNEL_READY_SEC') or 60)
TEMP_PORTS = range(20300, 20400)
WINDOWS = os.name == 'nt'
# 숨긴 콘솔에 붙어 Ctrl-Break를 보낸다. cloudflared는 이를 종료 신호로 받아 연결을 먼저 내리고 끝난다(강제 종료는 진행 중 요청이 502가 된다).
_BREAK = ("import ctypes,sys;k=ctypes.windll.kernel32;k.FreeConsole();p=int(sys.argv[1]);"
          "sys.exit(2) if not k.AttachConsole(p) else None;k.SetConsoleCtrlHandler(None,True);"
          "sys.exit(0 if k.GenerateConsoleCtrlEvent(1,p) else 3)")


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _env():
    return {k: v for k, v in os.environ.items() if not k.upper().startswith(('DEV_', 'NIGHTSHIFT_', 'CLOUDFLARE_', 'CF_'))
            and not re.search(r'(TOKEN|PASSWORD|SECRET|API_KEY)$', k.upper())}


def prepare(data):
    """서비스 설정에 고정 metrics 주소와 짧은 종료 유예를 넣는다. 이미 있으면 그대로 둔다."""
    data.setdefault('metrics', SERVICE_METRICS)
    data.setdefault('grace-period', GRACE)
    return data


def metrics_address(raw):
    """설정의 metrics 주소(루프백만). 없으면 cloudflared 기본 첫 주소를 본다."""
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        data = {}
    address = str((data.get('metrics') if isinstance(data, dict) else None) or SERVICE_METRICS)
    match = re.fullmatch(r'(127\.0\.0\.1|localhost|\[::1\]):(\d{1,5})', address)
    if not match:
        raise issues.StoreError('터널 metrics 주소는 127.0.0.1:포트 형식이어야 서비스 준비를 확인할 수 있어요.', 409)
    return address


def _connections(raw):
    try:
        data = yaml.safe_load(raw) or {}
        return max(1, min(int(data.get('ha-connections') or 4), 4))
    except (yaml.YAMLError, ValueError, TypeError, AttributeError):
        return 4


def ready(address):
    """커넥터의 /ready — 활성 연결이 있으면 {connectorId, readyConnections}, 아니면 None."""
    try:
        response = httpx.get(f'http://{address}/ready', timeout=2, trust_env=False, follow_redirects=False)
        data = response.json() if response.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        return None
    if isinstance(data, dict) and data.get('connectorId') and int(data.get('readyConnections') or 0) > 0:
        return data
    return None


def wait_ready(address, want, exclude=(), seconds=None, process=None, who='서비스'):
    """exclude에 없는 커넥터가 연결 want개 이상으로 붙을 때까지 기다린다. 시간만 지나 성공 처리하지 않는다."""
    end = time.monotonic() + (READY_SEC if seconds is None else seconds)
    best, stale = None, False
    while True:
        if process is not None and process.poll() is not None:
            raise issues.StoreError(f'{who} 커넥터가 바로 종료됐어요(코드 {process.returncode}).', 409)
        state = ready(address)
        if state and state['connectorId'] in exclude:
            stale = True
        elif state:
            best = state
            if int(state['readyConnections']) >= want:
                return state['connectorId']
        if time.monotonic() >= end:
            got = f"연결 {best['readyConnections']}개" if best else '이전·임시 커넥터만 보임' if stale else '연결 없음'
            raise issues.StoreError(f'{who} 커넥터가 제한 시간 안에 Cloudflare에 붙지 않았어요({got}).', 409)
        time.sleep(0.5)


def _free_port():
    for port in TEMP_PORTS:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if WINDOWS and hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                s.bind(('127.0.0.1', port))
            return port
        except OSError:
            continue
    raise issues.StoreError('임시 커넥터에 쓸 빈 포트(20300~20399)가 없어요.', 409)


class Replica:
    def __init__(self, process, path, address, log):
        self.process, self.path, self.address, self.log = process, path, address, log


def _log_file():
    folder = config.DATA_DIR / 'tunnel-handoffs'
    folder.mkdir(parents=True, exist_ok=True)
    for old in sorted(folder.glob('*.log'))[:-20]:
        old.unlink(missing_ok=True)
    return (folder / f'{time.time_ns()}.log').open('wb')


def start(exe, raw, config_file, tunnel):
    """새 설정으로 임시 커넥터를 띄운다. 설정 사본은 원래 설정 옆에 둬서 상대 경로·credentials가 같게 풀린다."""
    port = _free_port()
    path = Path(config_file).parent / f'{MARK}{time.time_ns()}.yml'
    try:
        path.write_bytes(raw)
    except OSError:
        raise issues.StoreError('임시 커넥터 설정을 터널 설정 폴더에 쓰지 못했어요. 서비스는 바꾸지 않았어요.', 409)
    address = f'127.0.0.1:{port}'
    args = [exe, 'tunnel', '--config', str(path), '--metrics', address, '--grace-period', GRACE, '--no-autoupdate', 'run', tunnel]
    log = _log_file()
    options = {}
    if WINDOWS:
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = 0
        options = {'creationflags': subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP, 'startupinfo': info}
    else:
        options = {'start_new_session': True}
    try:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   cwd=str(path.parent), env=_env(), **options)
    except OSError:
        log.close()
        path.unlink(missing_ok=True)
        raise issues.StoreError('임시 커넥터를 실행하지 못했어요. 서비스는 바꾸지 않았어요.', 409)
    return Replica(process, path, address, log)


def _signal(pid):
    """정상 종료 신호를 보낸다. 보냈으면 True."""
    if WINDOWS:
        try:
            return subprocess.run([sys.executable, '-c', _BREAK, str(pid)], timeout=10,
                                  creationflags=subprocess.DETACHED_PROCESS).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    try:
        os.kill(pid, 2)
        return True
    except OSError:
        return False


def stop(replica):
    """임시 커넥터를 정상 종료로 내리고, 15초 안에 안 끝나면 강제 종료한다. 어떤 경우에도 프로세스·사본을 남기지 않는다."""
    try:
        if replica.process.poll() is None:
            _signal(replica.process.pid)
            try:
                replica.process.wait(15)
            except subprocess.TimeoutExpired:
                replica.process.kill()
                replica.process.wait(10)
    finally:
        replica.log.close()
        replica.path.unlink(missing_ok=True)


def switch(exe, path, new_raw, expected_hash, tunnel, replace, restart, phase=lambda text: None):
    """path의 설정을 new_raw로 바꾸고 서비스를 끊김 없이 재시작한다.
    임시 커넥터가 준비되지 않으면 설정·서비스를 그대로 둔다. 바꾼 뒤 서비스가 안 붙으면 원래 설정으로 되돌린 다음에 임시를 내린다."""
    path = Path(path)
    old_raw = path.read_bytes()
    if _hash(old_raw) != expected_hash:
        raise issues.StoreError('다른 작업이 터널 설정을 바꿨어요. 덮어쓰지 않고 중단해요.', 409)
    want = _connections(new_raw)
    new_address, old_address = metrics_address(new_raw), metrics_address(old_raw)
    before = ready(old_address)
    phase('임시 커넥터 준비')
    replica = start(exe, new_raw, path, tunnel)
    try:
        try:
            temp_id = wait_ready(replica.address, want, (), process=replica.process, who='임시')
        except issues.StoreError as e:
            raise issues.StoreError(str(e) + ' 다른 사이트가 끊기지 않도록 터널 설정과 서비스를 바꾸지 않았어요.', 409) from e
        exclude = {temp_id} | ({before['connectorId']} if before else set())
        phase('서비스 전환(임시 커넥터가 대신 받는 중)')
        try:
            replace(path, new_raw, expected_hash)
            restart()
            phase('서비스 준비 확인')
            wait_ready(new_address, want, exclude)
        except Exception as e:
            note = _rollback(path, old_raw, new_raw, old_address, exclude, replace, restart, phase)
            message = str(e) if isinstance(e, issues.StoreError) else '터널 서비스 전환에 실패했어요.'
            raise issues.StoreError(message + note, 409) from e
    finally:
        phase('임시 커넥터 정리')
        stop(replica)


def _rollback(path, old_raw, new_raw, old_address, exclude, replace, restart, phase):
    """임시 커넥터가 살아 있는 동안 원래 설정으로 되돌리고 서비스가 다시 붙는 것까지 본다."""
    phase('기존 터널 설정으로 되돌리는 중')
    try:
        if _hash(path.read_bytes()) == _hash(new_raw):
            replace(path, old_raw, _hash(new_raw))
        elif _hash(path.read_bytes()) != _hash(old_raw):
            return ' 다른 작업이 터널 설정을 바꿔서 되돌리지 않았어요. 설정 백업과 서비스 상태를 확인해 주세요.'
        restart()
        wait_ready(old_address, 1, exclude)
        return ' 기존 터널 설정으로 되돌렸어요.'
    except Exception:
        return ' 기존 설정으로 되돌리지 못했어요. 설정 백업과 Cloudflared 서비스 상태를 확인해 주세요.'


STALE_SEC = 600  # 한 번의 전환은 이보다 짧다. 이보다 젊은 임시는 다른 프로세스가 진행 중인 전환일 수 있어 건드리지 않는다.


def _born(text):
    match = re.search(re.escape(MARK) + r'(\d{19})\.yml', text)
    return int(match.group(1)) / 1e9 if match else None


def strays(min_age=STALE_SEC):
    """dev가 띄웠다가 남은 임시 커넥터 PID 목록(명령줄에 MARK가 있고 min_age초보다 오래된 cloudflared만)."""
    lines = []
    try:
        if WINDOWS:
            script = ("Get-CimInstance Win32_Process -Filter \"Name='cloudflared.exe'\" | "
                      "Where-Object { $_.CommandLine -like '*" + MARK + "*' } | ForEach-Object { \"$($_.ProcessId) $($_.CommandLine)\" }")
            out = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True,
                                 timeout=30, creationflags=subprocess.CREATE_NO_WINDOW).stdout.decode(errors='replace')
            lines = [tuple(line.split(' ', 1)) for line in out.splitlines() if ' ' in line]
        else:
            for entry in Path('/proc').iterdir():
                if entry.name.isdigit():
                    try:
                        line = (entry / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
                    except OSError:
                        continue
                    if 'cloudflared' in line:
                        lines.append((entry.name, line))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    now = time.time()
    return [int(pid) for pid, line in lines if pid.strip().isdigit() and _born(line) and now - _born(line) >= min_age]


def cleanup(config_file, service_address):
    """남은 임시 커넥터를 정리한다. 서비스가 붙어 있지 않으면 사이트를 지키는 중일 수 있어 그대로 둔다."""
    pids = strays()
    if pids and not ready(service_address):
        return f'Cloudflared 서비스가 아직 붙지 않아 임시 커넥터 {len(pids)}개를 남겨 뒀어요.'
    for pid in pids:
        if _signal(pid):
            end = time.monotonic() + 15
            while time.monotonic() < end and pid in strays():
                time.sleep(1)
        if pid in strays():
            try:
                if WINDOWS:
                    subprocess.run(['taskkill', '/PID', str(pid), '/F'], capture_output=True, timeout=15,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    os.kill(pid, 9)
            except (OSError, subprocess.TimeoutExpired):
                pass
    if not strays():
        for leftover in Path(config_file).parent.glob(MARK + '*.yml'):
            born = _born(leftover.name)
            if born and time.time() - born >= STALE_SEC:
                leftover.unlink(missing_ok=True)
    return ''
