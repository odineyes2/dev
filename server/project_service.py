"""프로젝트 서비스 연결 — 공용 터널 변경은 서버에서 한 번에 하나씩 수행한다."""
import json
import hashlib
import errno
import os
import re
import shutil
import sqlite3
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml

import config
import db
import issues
import tunnel_handoff


def find_free_port():
    """등록 포트는 보관 여부와 무관하게 제외하고 루프백 TCP 후보만 검사한다."""
    try:
        with db.connect() as c:
            reserved = {row[0] for row in c.execute(
                'SELECT service_port FROM projects WHERE service_port > 0 '
                'UNION SELECT port FROM project_services WHERE port > 0')}
        reserved.add(config.PORT)
        origin = urlsplit(config.NIGHTSHIFT_URL)
        if origin.hostname in ('localhost', '127.0.0.1', '::1'):
            reserved.add(origin.port or (443 if origin.scheme == 'https' else 80))
        for port in range(8400, 9000):
            if port in reserved:
                continue
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
                    if os.name == 'nt' and hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                        candidate.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                    candidate.bind(('127.0.0.1', port))
                return port
            except OSError as error:
                if error.errno in (errno.EADDRINUSE, errno.EACCES) or getattr(error, 'winerror', None) in (10048, 10013):
                    continue
                raise
    except (OSError, sqlite3.Error, ValueError):
        raise issues.StoreError('빈 포트를 확인하지 못했어요. 직접 입력하거나 다시 시도해 주세요.', 503)
    raise issues.StoreError('8400~8999에 빈 포트가 없어요. 다른 포트를 직접 입력해 주세요.', 503)


def validate_prepare_provider(value):
    if value not in ('auto', 'codex', 'claude', 'none'):
        raise issues.StoreError('실행 파일 생성 방식은 자동·Codex·Claude·기존 파일 중에서 선택해 주세요.')
    return value


def validate_fields(domain='', port=0, app=''):
    domain = issues._text(domain, '서비스 도메인', 253).strip().lower().rstrip('.')
    if domain:
        try:
            domain = domain.encode('idna').decode('ascii')
        except UnicodeError:
            raise issues.StoreError('올바른 서비스 도메인을 입력해 주세요.')
        if len(domain) > 253 or '.' not in domain or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part) for part in domain.split('.')) or domain.replace('.', '').isdigit():
            raise issues.StoreError('서비스 도메인은 주소·경로 없이 입력해 주세요(예: app.lomebrote.com).')
    if isinstance(port, bool) or not re.fullmatch(r'\d{1,5}', str(port or 0)) or not 0 <= int(port or 0) <= 65535:
        raise issues.StoreError('로컬 포트는 1~65535 사이 정수로 입력해 주세요.')
    app = issues._text(app, 'PM2 앱 이름', 80).strip()
    if app and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', app):
        raise issues.StoreError('PM2 앱 이름은 영문·숫자·밑줄·하이픈으로 입력해 주세요.')
    return {'service_domain': domain, 'service_port': int(port or 0), 'service_app': app}


def config_path():
    default = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/config/systemprofile/.cloudflared/config.yml' if os.name == 'nt' else Path.home() / '.cloudflared/config.yml'
    return Path(os.environ.get('DEV_TUNNEL_CONFIG') or default)


def binary():
    found = os.environ.get('DEV_CLOUDFLARED_BIN') or shutil.which('cloudflared')
    if not found and os.name == 'nt':
        found = 'C:/Program Files (x86)/cloudflared/cloudflared.exe'
    if not found or not Path(found).is_file():
        raise issues.StoreError('cloudflared 실행 파일을 찾을 수 없어요. DEV_CLOUDFLARED_BIN을 설정해 주세요.', 409)
    return found


def _project(key):
    with db.connect() as c:
        row = c.execute('SELECT * FROM projects WHERE key=?', (str(key).upper(),)).fetchone()
    if not row:
        raise issues.StoreError('프로젝트를 찾을 수 없어요.', 404)
    return dict(row)


def app_environment(repo, port):
    """서비스 연결의 앱 시작 env — 허용 목록 env에 프로젝트 .env 비밀값(DEV-239)을 더하고 PORT·HOST가 이긴다."""
    import project_secrets
    import repo_snapshot
    env = {**repo_snapshot._environment(), **project_secrets.env_for(repo), 'PORT': str(port), 'HOST': '127.0.0.1'}
    if os.environ.get('PM2_HOME'):
        env['PM2_HOME'] = os.environ['PM2_HOME']
    return env


def _existing_service(project):
    """저장된 값과 알려진 운영 주소로 조회한다. 기존 터널의 소유권은 가져오지 않는다."""
    domain, port = project['service_domain'], project['service_port']
    try:
        repo = Path(project['local_path']).resolve() if project['local_path'] else None
    except OSError:
        repo = None
    if repo == config.REPO_ROOT.resolve():
        domain = domain or urlsplit(config.PUBLIC_URL).hostname
        port = port or config.PORT
    elif repo == (config.REPO_ROOT.parent / 'nightshift').resolve():
        domain = domain or urlsplit(config.NIGHTSHIFT_PUBLIC_URL).hostname
        port = port or urlsplit(config.NIGHTSHIFT_URL).port or 80
    if domain and not port:
        try:
            data = yaml.safe_load(config_path().read_bytes()) or {}
            for rule in data.get('ingress', []):
                if str(rule.get('hostname', '')).lower().rstrip('.') != domain or rule.get('path'):
                    continue
                origin = urlsplit(rule.get('service', ''))
                if origin.scheme == 'http' and origin.hostname in ('localhost', '127.0.0.1', '::1'):
                    port = origin.port or 80
                    break
        except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError):
            pass
    return domain or '', port


def _reachable(url):
    try:
        return httpx.get(url + '/api/health', timeout=3, trust_env=False, follow_redirects=False).status_code == 200
    except httpx.HTTPError:
        return False


def _steps(project, record):
    labels = ('도메인·포트 저장', '터널 설정·재시작 권한 확인', '터널 규칙 사전 검사',
              '앱 실행 준비·로컬 접속 확인', 'DNS 등록·무중단 터널 전환', '외부 HTTPS 접속 확인')
    disconnect = bool(record and record['action'] == 'disconnect')
    if disconnect:
        # 연결 해제는 step 2·3·5를 지나고 앱 정지를 선택하면 6까지 간다.
        labels = ('터널 설정·재시작 권한 확인', '터널 규칙 사전 검사', '무중단 터널 전환')
    record = dict(record) if record else None
    if disconnect and record.get('stop_apps'):
        labels += ('앱 멈추기',)
    current = record['step'] if record else 0
    result = []
    for number, label in enumerate(labels, 1):
        stored = (2, 3, 5, 6)[number - 1] if disconnect else number
        state = 'pending'
        if not disconnect and number == 1 and (current or project['service_domain'] and project['service_port']):
            state = 'done'
        if (current and stored < current) or (record and record['status'] in ('connected', 'disconnected')):
            state = 'done'
        elif stored == current and record['status'] in ('running', 'failed'):
            state = 'running' if record['status'] == 'running' else 'failed'
        if state == 'running' and stored == 5 and record['phase']:
            # 전환 단계는 임시 커넥터 준비·서비스 전환·준비 확인·정리 중 어디인지 덧붙인다.
            label = label + ' — ' + record['phase']
        if disconnect and stored == 6 and record.get('app_error') and record['status'] == 'disconnected':
            state = 'failed'
        result.append({'number': number, 'label': label, 'status': state,
                       'error': (record.get('app_error') or record['error']) if state == 'failed' else ''})
    return result


def status(key):
    project = _project(key)
    with db.connect() as c:
        row = c.execute('SELECT * FROM project_services WHERE project_id=?', (project['id'],)).fetchone()
    state = dict(row) if row else {'status': 'idle', 'domain': '', 'port': 0, 'phase': '', 'error': ''}
    state['steps'] = _steps(project, row)
    if state['status'] == 'failed' and state.get('step') == 4 and state['error'] == '실행 파일 생성에 실패했어요. 에이전트 CLI 로그인과 설정을 확인해 주세요.':
        import service_prepare
        detail = service_prepare.previous_failure(project['id'], state['updated_at'])
        if detail:
            state['error'] = '실행 파일 생성에 실패했어요. ' + detail
            for step in state['steps']:
                if step['status'] == 'failed':
                    step['error'] = state['error']
    state['managed'] = bool(state['domain'])
    domain, port = _existing_service(project)
    domain, port = state['domain'] or domain, state['port'] or port
    for field in ('backup_path', 'config_path', 'applied_hash'):
        state.pop(field, None)
    state['url'] = 'https://' + domain if domain else ''
    state['observed_port'] = port
    state['online'] = _reachable(state['url']) if domain and state['status'] != 'running' else None
    if state['online'] and not state['managed']:
        state.update(status='external', phase='기존 서비스 운영 중', domain=domain, port=port)
    state['changed'] = bool(state['managed'] and (state['domain'], state['port']) != (project['service_domain'], project['service_port']))
    state['config_path'] = str(config_path())
    try:
        binary()
        with config_path().open('rb') as f:
            f.read(1)
        state['setup_error'] = ''
    except (OSError, issues.StoreError):
        state['setup_error'] = '터널 설정에 접근할 수 없거나 cloudflared가 없어요. Dev 실행 계정의 파일 접근·서비스 재시작 권한과 DEV_TUNNEL_CONFIG를 확인해 주세요.'
    return state


def _save(pid, **fields):
    with db.connect() as c:
        c.execute('UPDATE project_services SET ' + ','.join(k + '=?' for k in fields) + ',updated_at=? WHERE project_id=?', (*fields.values(), db.now_iso(), pid))


def disconnect_preview(key):
    import pm2_apps
    project = _project(key)
    pm2_apps.reconcile(project)
    return pm2_apps.preview(project)


def request(actor, key, action='connect', stop_apps=False, preview_token=''):
    if actor['kind'] != 'human':
        raise issues.StoreError('서비스 연결은 관리자 화면에서 실행해 주세요.', 403)
    if action not in ('connect', 'disconnect'):
        raise issues.StoreError('지원하지 않는 서비스 작업이에요.')
    if type(stop_apps) is not bool or (action == 'connect' and stop_apps):
        raise issues.StoreError('앱 멈추기는 연결 해제에서 참·거짓으로 선택해 주세요.')
    if not isinstance(preview_token, str):
        raise issues.StoreError('앱 목록을 다시 확인해 주세요.', 409)
    approved = None
    project = _project(key)
    if project['archived']:
        raise issues.StoreError('보관된 프로젝트에는 서비스를 연결할 수 없어요.', 409)
    if action == 'connect' and not (project['service_domain'] and project['service_port']):
        raise issues.StoreError('서비스 도메인과 로컬 포트를 입력하고 저장해 주세요.', 409)
    try:
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            # 프로젝트 수정·삭제와 같은 쓰기 잠금에서 대상 정보를 고정한다.
            current = c.execute('SELECT * FROM projects WHERE id=?', (project['id'],)).fetchone()
            if not current or dict(current) != project:
                raise issues.StoreError('프로젝트 정보가 바뀌었어요. 새로고침 후 다시 연결해 주세요.', 409)
            if c.execute("SELECT 1 FROM project_app_restarts WHERE status IN ('waiting','restarting','checking')").fetchone():
                raise issues.StoreError('앱 재시작이 끝난 뒤 서비스 연결을 변경해 주세요.', 409)
            if c.execute("SELECT 1 FROM project_services WHERE status='running'").fetchone():
                raise issues.StoreError('다른 서비스 연결이 진행 중이에요. 끝난 뒤 다시 시도해 주세요.', 409)
            import jobs
            if jobs._active_lanes(c):
                raise issues.StoreError('에이전트 작업·병합·프로젝트 생성이 끝난 뒤 공용 터널을 연결해 주세요.', 409)
            old = c.execute('SELECT * FROM project_services WHERE project_id=?', (project['id'],)).fetchone()
            if action == 'disconnect' and (not old or not old['domain']):
                raise issues.StoreError('연결된 서비스가 없어요.', 409)
            if c.execute("SELECT 1 FROM project_initializations WHERE project_id=? AND status IN ('queued','running','cancelling')", (project['id'],)).fetchone():
                raise issues.StoreError('프로젝트 초기 생성이 끝난 뒤 연결해 주세요.', 409)
            if stop_apps:
                import pm2_apps
                try:
                    approved = pm2_apps.snapshot(project, c)
                except (pm2_apps.AppError, OSError):
                    raise issues.StoreError('앱 목록을 확인하지 못했어요. 다시 확인해 주세요.', 409) from None
                if approved['token'] != preview_token:
                    raise issues.StoreError('앱 목록이 바뀌었어요. 다시 확인해 주세요.', 409)
            c.execute('''INSERT INTO project_services(project_id,status,actor,action,updated_at,step,stop_apps) VALUES(?,'running',?,?,?,2,?)
                ON CONFLICT(project_id) DO UPDATE SET status='running',step=2,phase='연결 준비',error='',app_error='',stop_apps=excluded.stop_apps,actor=excluded.actor,action=excluded.action,updated_at=excluded.updated_at''',
                (project['id'], issues.actor_label(actor), action, db.now_iso(), int(stop_apps)))
    except sqlite3.IntegrityError:
        raise issues.StoreError('다른 서비스 연결이 진행 중이에요.', 409)
    worker = threading.Thread(target=_run, args=(project, action, approved) if stop_apps else (project, action), daemon=True)
    try:
        worker.start()
    except RuntimeError:
        _save(project['id'], status='failed', error='서비스 작업을 시작할 수 없어요.')
        raise issues.StoreError('서비스 작업을 시작할 수 없어요.', 503)
    return status(key)


def pm2_launcher():
    pm2 = shutil.which('pm2')
    launcher = [pm2] if pm2 else [shutil.which('npx'), '--no-install', 'pm2']
    if not launcher[0]:
        raise issues.StoreError('설치된 PM2 또는 npx를 찾을 수 없어요.', 409)
    if os.name == 'nt':
        node = shutil.which('node')
        if not node:
            raise issues.StoreError('Dev 실행 환경에서 Node.js를 찾을 수 없어요. PATH를 확인해 주세요.', 409)
        parent = Path(launcher[0]).parent
        package_cli = Path('pm2/bin/pm2' if pm2 else 'npm/bin/npx-cli.js')
        candidates = [parent / 'node_modules' / package_cli]
        # npm exec·npx는 node_modules/.bin의 바로가기를 PATH 앞에 둔다.
        if parent.name == '.bin':
            candidates.append(parent.parent / package_cli)
        if not pm2:
            candidates.append(parent / 'npx-cli.js')
        cli = next((path for path in candidates if path.is_file()), None)
        if cli is None:
            raise issues.StoreError('설치된 PM2 또는 npx의 실행 파일을 찾을 수 없어요. 설치 경로를 확인해 주세요.', 409)
        launcher = [node, str(cli), *launcher[1:]]
    return launcher


def pm2_environment():
    """Dev 실행 계정의 환경만 사용해 같은 PM2 daemon과 홈을 고정한다."""
    return {k: v for k, v in os.environ.items() if not k.upper().startswith(('DEV_', 'NIGHTSHIFT_', 'CLOUDFLARE_', 'CF_')) and not re.search(r'(TOKEN|PASSWORD|SECRET|API_KEY)$', k.upper())}


def pm2_app_registered(app):
    """환경값을 노출하지 않고 현재 daemon의 앱 이름만 확인한다."""
    try:
        result = subprocess.run([*pm2_launcher(), 'jlist'], env=pm2_environment(),
                                capture_output=True, text=True, encoding='utf-8', errors='replace',
                                timeout=45, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        return result.returncode == 0 and any(item.get('name') == app for item in json.loads(result.stdout))
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        return False


def pin_pm2_environment(env):
    """프로젝트 환경이 PM2 daemon과 실행 계정 홈을 바꾸지 못하게 한다."""
    identity = {'PM2_HOME', 'HOME', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH'}
    result = {k: v for k, v in env.items() if k.upper() not in identity}
    result.update({k: v for k, v in pm2_environment().items() if k.upper() in identity})
    return result


PM2_SAVE_WARNING = 'PM2 목록 저장에 실패했어요. 재부팅 뒤 자동으로 다시 켜지지 않을 수 있어요.'


def save_pm2(launcher=None, environment=None):
    """저장 실패는 정상 연결을 되돌리지 않고 고정된 경고로 반환한다."""
    try:
        _command([*(launcher if launcher is not None else pm2_launcher()), 'save'],
                 'PM2 목록 저장', environment=environment if environment is not None else pm2_environment(), timeout=45)
        return ''
    except Exception:
        return PM2_SAVE_WARNING


def _command(args, label, cwd=None, environment=None, timeout=45):
    try:
        # 앱 실행에 Dev의 인증키·알림 토큰을 물려주지 않는다. 앱은 자기 실행 설정에서 환경을 읽는다.
        env = environment if environment is not None else {k: v for k, v in os.environ.items() if not k.upper().startswith(('DEV_', 'NIGHTSHIFT_', 'CLOUDFLARE_', 'CF_')) and not re.search(r'(TOKEN|PASSWORD|SECRET|API_KEY)$', k.upper())}
        result = subprocess.run(args, cwd=cwd, capture_output=True, timeout=timeout,
                                env=env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode:
            if environment is not None:
                from repo_snapshot import _safe
                detail = _safe(result.stderr.decode('utf-8', errors='replace'))[-1000:]
                raise issues.StoreError(label + '에 실패했어요. ' + detail, 409)
            raise issues.StoreError(label + '에 실패했어요. 실행 계정의 권한·설정과 기존 DNS 충돌을 확인해 주세요.', 409)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise issues.StoreError(label + '을 실행할 수 없거나 시간이 초과됐어요.', 409) from e


def _restart():
    name = os.environ.get('DEV_TUNNEL_SERVICE') or ('Cloudflared' if os.name == 'nt' else 'cloudflared')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', name):
        raise issues.StoreError('터널 서비스 이름 설정이 잘못됐어요.', 409)
    # 예전 설정으로 뜬 서비스는 종료 유예가 30초라 멈추는 데 그만큼 걸릴 수 있다(그동안은 임시 커넥터가 받는다).
    if os.name == 'nt':
        _command(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', "$ErrorActionPreference='Stop'; Restart-Service -Name '" + name + "'"], '터널 서비스 재시작', timeout=120)
    else:
        _command(['systemctl', 'restart', name], '터널 서비스 재시작', timeout=120)


def _restart_permission():
    if os.name != 'nt':
        return
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL('advapi32', use_last_error=True)
    api.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    api.OpenSCManagerW.restype = wintypes.HANDLE
    api.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
    api.OpenServiceW.restype = wintypes.HANDLE
    api.CloseServiceHandle.argtypes = [wintypes.HANDLE]
    manager = api.OpenSCManagerW(None, None, 1)
    service = api.OpenServiceW(manager, os.environ.get('DEV_TUNNEL_SERVICE') or 'Cloudflared', 0x4 | 0x10 | 0x20) if manager else None
    if service:
        api.CloseServiceHandle(service)
    if manager:
        api.CloseServiceHandle(manager)
    if not service:
        raise issues.StoreError('Dev 실행 계정에 Cloudflared 서비스를 시작·중지할 권한이 없어요. 서비스 권한을 설정해 주세요.', 409)


def _healthy(url, seconds=30):
    end = time.monotonic() + seconds
    with httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as client:
        while True:
            try:
                if client.get(url).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic() >= end:
                raise issues.StoreError('서비스 health 확인에 실패했어요: ' + url, 409)
            time.sleep(1)


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _replace(path, raw, expected):
    if _hash(path.read_bytes()) != expected:
        raise issues.StoreError('다른 작업이 터널 설정을 바꿨어요. 덮어쓰지 않고 중단해요.', 409)
    fd, temp = tempfile.mkstemp(prefix='.dev-tunnel-', suffix='.yml', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def _candidate(raw, old, domain, port):
    data = yaml.safe_load(raw)
    if not isinstance(data, dict) or not data.get('tunnel') or not isinstance(data.get('ingress'), list):
        raise issues.StoreError('로컬 관리 터널의 tunnel·ingress 설정이 필요해요.', 409)
    rules = data['ingress']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}', str(data['tunnel'])):
        raise issues.StoreError('터널 ID 또는 이름 설정을 확인해 주세요.', 409)
    if not rules or any(not isinstance(r, dict) for r in rules) or 'hostname' in rules[-1] or 'path' in rules[-1]:
        raise issues.StoreError('터널의 마지막 catch-all 규칙을 확인해 주세요.', 409)
    old_domain, old_port = old['domain'], old['port']
    if old_domain:
        matches = [r for r in rules if str(r.get('hostname', '')).lower().rstrip('.') == old_domain]
        if len(matches) != 1 or matches[0].get('service') != f'http://127.0.0.1:{old_port}' or set(matches[0]) != {'hostname', 'service'}:
            raise issues.StoreError('연결 이후 기존 터널 규칙이 바뀌었어요. 자동으로 덮어쓰거나 삭제하지 않아요.', 409)
        rules.remove(matches[0])
    if domain:
        if any(str(r.get('hostname', '')).lower().rstrip('.') == domain for r in rules):
            raise issues.StoreError('기존 터널에서 사용하는 도메인이에요. 다른 도메인을 입력해 주세요.', 409)
        rules.insert(0, {'hostname': domain, 'service': f'http://127.0.0.1:{port}'})
    tunnel_handoff.prepare(data)
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False).encode('utf-8'), str(data['tunnel'])


def _switch(pid, path, new_raw, expected_hash):
    """설정 반영은 언제나 무중단 전환으로 한다(DEV-255). 서비스를 바로 재시작하는 길은 두지 않는다."""
    tunnel = str((yaml.safe_load(new_raw) or {}).get('tunnel') or '')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}', tunnel):
        raise issues.StoreError('터널 ID 또는 이름 설정을 확인해 주세요.', 409)
    tunnel_handoff.switch(binary(), path, new_raw, expected_hash, tunnel, _replace, _restart,
                          phase=lambda text: _save(pid, phase=text))


def _restore(row):
    if not row['backup_path'] or not row['applied_hash']:
        return ''
    try:
        path = Path(row['config_path'])
        original = Path(row['backup_path']).read_bytes()
        current = path.read_bytes()
        if _hash(current) == _hash(original):
            # 설정이 그대로면 서비스가 붙어 있는지만 본다. 괜히 재시작하지 않는다.
            if not tunnel_handoff.ready(tunnel_handoff.metrics_address(original)):
                _switch(row['project_id'], path, original, _hash(current))
        else:
            _switch(row['project_id'], path, original, row['applied_hash'])
        _save(row['project_id'], backup_path='', applied_hash='')
        return ' 기존 터널 설정을 유지했어요.' if _hash(current) == _hash(original) else ' 기존 터널 설정을 복구했어요.'
    except Exception:
        return ' 터널 자동 복구를 완료하지 못했어요. 설정 백업과 서비스 상태를 확인해 주세요.'


def _run(project, action, approved=None):
    pid = project['id']
    dns_attempted = False
    started_pm2 = False
    try:
        _save(pid, step=2, phase='터널 설정·권한 확인')
        with db.connect() as c:
            old = dict(c.execute('SELECT * FROM project_services WHERE project_id=?', (pid,)).fetchone())
        if old['backup_path']:
            _restore(old)
            with db.connect() as c:
                old = dict(c.execute('SELECT * FROM project_services WHERE project_id=?', (pid,)).fetchone())
            if old['backup_path']:
                raise issues.StoreError('이전 실패의 터널 백업이 남아 있어요. 설정과 서비스를 복구한 뒤 다시 시도해 주세요.', 409)
        path, exe = config_path(), binary()
        _restart_permission()
        raw = path.read_bytes()
        domain = project['service_domain'] if action == 'connect' else ''
        port = project['service_port'] if domain else 0
        _save(pid, step=3, phase='터널 규칙 사전 검사')
        candidate, tunnel = _candidate(raw, old, domain, port)
        # 실제 설정과 같은 디렉터리에서 쓰기 권한과 ingress 유효성을 먼저 확인한다.
        fd, temp = tempfile.mkstemp(prefix='.dev-validate-', suffix='.yml', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(candidate)
            _command([exe, 'tunnel', '--config', temp, 'ingress', 'validate'], '터널 규칙 검사')
        finally:
            Path(temp).unlink(missing_ok=True)
        if domain:
            import pm2_apps
            pm2_apps.before_connect(project)
            _save(pid, step=4, phase='앱 실행 준비')
            app = project['service_app']
            if app and app != 'dev':
                repo = Path(project['local_path'])
                if not project['local_path'] or not repo.is_dir():
                    raise issues.StoreError('Local path의 프로젝트 폴더를 찾을 수 없어요.', 409)
                if not (repo / 'ecosystem.config.js').is_file():
                    if project.get('service_prepare_provider', 'auto') == 'none':
                        raise issues.StoreError('기존 파일 사용을 선택했어요. 프로젝트의 ecosystem.config.js를 준비하거나 실행 파일 생성 방식을 Codex·Claude로 바꿔 주세요.', 409)
                    _save(pid, phase='에이전트가 실행 파일을 생성·검사하고 있어요')
                    import service_prepare
                    service_prepare.prepare(project)
                    if not (repo / 'ecosystem.config.js').is_file():
                        raise issues.StoreError('자동 생성 결과에 ecosystem.config.js가 없어요.', 409)
                _save(pid, phase='앱 시작')
                launcher = pm2_launcher()
                pm2_env = pm2_environment()
                app_env = app_environment(repo, port)
                # 프로젝트 비밀값으로 daemon·실행 계정의 홈 해석이 바뀌지 않게 한다.
                identity = {'PM2_HOME', 'HOME', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH'}
                app_env = {k: v for k, v in app_env.items() if k.upper() not in identity}
                app_env.update({k: v for k, v in pm2_env.items() if k.upper() in identity})
                args = [*launcher, 'startOrRestart', 'ecosystem.config.js', '--only', app, '--update-env']
                _command(args, 'PM2 앱 시작', str(repo), environment=app_env)
                started_pm2 = True
            _save(pid, phase='로컬 서비스 확인')
            _healthy(f'http://127.0.0.1:{port}/api/health')
            _save(pid, step=5, phase='DNS 연결')
            dns_attempted = True
            _command([exe, 'tunnel', '--config', str(path), 'route', 'dns', tunnel, domain], 'DNS 연결')
        backup = config.DATA_DIR / 'tunnel-backups' / (str(pid) + '-' + str(time.time_ns()) + '.yml')
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(raw)
        _save(pid, step=5, phase='터널 반영', backup_path=str(backup), config_path=str(path), applied_hash=_hash(candidate))
        _switch(pid, path, candidate, _hash(raw))
        if domain:
            _save(pid, step=6, phase='외부 HTTPS 확인')
            _healthy('https://' + domain + '/api/health')
        if not domain and approved is not None:
            # 해제 확정과 step=6을 함께 남긴다. 이후 앱 오류는 터널을 복원하지 않는다.
            _save(pid, step=6, phase='앱 멈추기', domain='', port=0, backup_path='', applied_hash='')
            import pm2_apps
            try:
                warning = pm2_apps.stop(project, approved['candidates'])
            except Exception:
                warning = '터널 연결 해제는 끝났어요. 앱 정지 처리를 완료하지 못했어요. PM2 상태를 확인해 주세요.'
            _save(pid, status='disconnected', phase='연결 해제 완료', app_error=warning, error=warning)
            return
        restored, app_warning = [], ''
        had_remembered = False
        if domain:
            import pm2_apps
            try:
                had_remembered = bool(pm2_apps.records(pid))
                app_warning, restored = pm2_apps.restore(project)
            except Exception:
                app_warning = '기억한 앱의 복구 상태를 확인하지 못했어요. 다시 연결해 주세요.'
        save_warning = save_pm2(launcher, pm2_env) if started_pm2 else (save_pm2() if had_remembered else '')
        if not save_warning and restored:
            try:
                pm2_apps.forget(restored)
            except sqlite3.Error:
                app_warning += '\n앱 복구 기록을 정리하지 못했어요. 다음 연결에서 다시 확인해요.'
        warning = '\n'.join(filter(None, (app_warning, save_warning)))
        _save(pid, status='connected' if domain else 'disconnected', phase='연결 완료' if domain else '연결 해제 완료', domain=domain, port=port, backup_path='', applied_hash='', error=warning)
    except Exception as e:
        with db.connect() as c:
            row = dict(c.execute('SELECT * FROM project_services WHERE project_id=?', (pid,)).fetchone())
        note = _restore(row)
        error = str(e) if isinstance(e, issues.StoreError) else '터널 설정에 접근하거나 서비스 연결을 처리하지 못했어요. 실행 계정의 권한·설정 파일을 확인해 주세요.'
        # shortcut: CLI에는 DNS 삭제 명령이 없으므로 기존 레코드를 보존한다. DNS 정리 자동화가 필요하면 API를 연결한다.
        if dns_attempted:
            note += ' DNS 레코드는 남아 있을 수 있어요. Cloudflare에서 확인해 주세요.'
        _save(pid, status='failed', phase='연결 실패', error=error + note)


INIT_CONNECT_WAIT_SEC = 1800
INIT_ACTOR = 'system:project_initialization'


def _ingress_hosts():
    try:
        data = yaml.safe_load(config_path().read_bytes()) or {}
        rules = data.get('ingress') or []
        return {str(r.get('hostname', '')).lower().rstrip('.') for r in rules if isinstance(r, dict) and r.get('hostname')}
    except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError):
        raise issues.StoreError('터널 설정을 읽을 수 없어 자동 연결 도메인을 확인하지 못했어요. DEV_TUNNEL_CONFIG와 파일 접근 권한을 확인해 주세요.', 409)


def check_new_domain(domain):
    """초기 생성의 자동 연결은 기존 터널 도메인의 하위 새 이름만 허용한다(DEV-247)."""
    hosts = _ingress_hosts()
    zones = sorted({h.split('.', 1)[1] for h in hosts if h.count('.') >= 2})
    if not any(domain.endswith('.' + zone) for zone in zones):
        raise issues.StoreError('자동 연결 도메인은 기존 터널 도메인(' + (', '.join(zones) or '없음') + ')의 하위 이름만 쓸 수 있어요.')
    if domain in hosts:
        raise issues.StoreError('기존 터널에서 쓰는 도메인이에요. 다른 도메인을 입력해 주세요.')


def _init_failed(pid, error):
    with db.connect() as c:
        c.execute('''INSERT INTO project_services(project_id,status,actor,action,updated_at,step,phase,error) VALUES(?,'failed',?,'connect',?,2,'연결 실패',?)
            ON CONFLICT(project_id) DO UPDATE SET status='failed',phase='연결 실패',error=excluded.error,updated_at=excluded.updated_at''',
                  (pid, INIT_ACTOR, db.now_iso(), error))


def connect_for_initialization(project):
    """초기 생성 스레드 안에서 공용 터널 잠금을 잡고, 다른 작업이 끝난 뒤 기존 connect 6단계를 동기로 돈다."""
    import jobs
    pid, key = project['id'], project['key']
    end = time.monotonic() + INIT_CONNECT_WAIT_SEC
    while True:
        try:
            with db.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                c.execute('''INSERT INTO project_services(project_id,status,actor,action,updated_at,step,phase) VALUES(?,'running',?,'connect',?,2,'다른 작업이 끝나기를 기다리는 중')
                    ON CONFLICT(project_id) DO UPDATE SET status='running',step=2,phase=excluded.phase,error='',actor=excluded.actor,action=excluded.action,updated_at=excluded.updated_at''',
                          (pid, INIT_ACTOR, db.now_iso()))
            break
        except sqlite3.IntegrityError:
            if time.monotonic() >= end:
                _init_failed(pid, '다른 서비스 연결이 끝나지 않아 서비스 연결을 못 했어요.')
                return _row(pid)
            time.sleep(1)
    # 잠금을 잡은 뒤에는 start_block이 새 작업을 막는다. 이미 도는 작업만 끝나기를 기다린다.
    try:
        while True:
            with db.connect() as c:
                lanes = jobs._active_lanes(c) - {(key, 'initialization')}
                restarting = c.execute("SELECT 1 FROM project_app_restarts WHERE status IN ('waiting','restarting','checking')").fetchone()
            if not lanes and not restarting:
                break
            if time.monotonic() >= end:
                _save(pid, status='failed', phase='연결 실패', error='다른 작업이 끝나지 않아 서비스 연결을 못 했어요.')
                return _row(pid)
            time.sleep(1)
    except Exception:
        _save(pid, status='failed', phase='연결 실패', error='다른 작업의 상태를 확인하지 못해 서비스 연결을 못 했어요.')
        return _row(pid)
    _run(project, 'connect')
    return _row(pid)


def _row(pid):
    with db.connect() as c:
        row = c.execute('SELECT status,error FROM project_services WHERE project_id=?', (pid,)).fetchone()
    return dict(row) if row else None


def recover():
    with db.connect() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM project_services WHERE status='running'")]

    def restore_pending():
        for row in rows:
            if row['action'] == 'disconnect' and row['step'] == 6 and not row['domain']:
                import pm2_apps
                try:
                    with db.connect() as c:
                        project = dict(c.execute('SELECT * FROM projects WHERE id=?', (row['project_id'],)).fetchone())
                    pm2_apps.reconcile(project)
                except Exception:
                    pass
                warning = '터널 연결 해제는 끝났어요. 서버 재시작으로 앱 정지 작업이 중단됐어요. PM2 상태를 확인해 주세요.'
                _save(row['project_id'], status='disconnected', phase='작업 중단', app_error=warning, error=warning)
                continue
            _save(row['project_id'], status='failed', phase='작업 중단', error='서버가 재시작되어 연결 작업이 중단됐어요. DNS 레코드도 확인해 주세요.' + _restore(row))
        # 전환 도중 dev가 죽어 남은 임시 커넥터는 서비스가 붙어 있을 때만 내린다(DEV-255).
        try:
            path = config_path()
            tunnel_handoff.cleanup(path, tunnel_handoff.metrics_address(path.read_bytes()))
        except Exception:
            pass
    threading.Thread(target=restore_pending, daemon=True).start()
