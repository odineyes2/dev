"""프로젝트 서비스 연결 — 공용 터널 변경은 서버에서 한 번에 하나씩 수행한다."""
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
              '앱 실행 준비·로컬 접속 확인', 'DNS 등록·터널 반영·재시작', '외부 HTTPS 접속 확인')
    if record and record['action'] != 'connect':
        return []
    current = record['step'] if record else 0
    result = []
    for number, label in enumerate(labels, 1):
        state = 'pending'
        if number == 1 and (current or project['service_domain'] and project['service_port']):
            state = 'done'
        if (current and number < current) or (record and record['status'] == 'connected'):
            state = 'done'
        elif number == current and record['status'] in ('running', 'failed'):
            state = 'running' if record['status'] == 'running' else 'failed'
        result.append({'number': number, 'label': label, 'status': state,
                       'error': record['error'] if state == 'failed' else ''})
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


def request(actor, key, action='connect'):
    if actor['kind'] != 'human':
        raise issues.StoreError('서비스 연결은 관리자 화면에서 실행해 주세요.', 403)
    if action not in ('connect', 'disconnect'):
        raise issues.StoreError('지원하지 않는 서비스 작업이에요.')
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
            c.execute('''INSERT INTO project_services(project_id,status,actor,action,updated_at,step) VALUES(?,'running',?,?,?,2)
                ON CONFLICT(project_id) DO UPDATE SET status='running',step=2,phase='연결 준비',error='',actor=excluded.actor,action=excluded.action,updated_at=excluded.updated_at''',
                (project['id'], issues.actor_label(actor), action, db.now_iso()))
    except sqlite3.IntegrityError:
        raise issues.StoreError('다른 서비스 연결이 진행 중이에요.', 409)
    worker = threading.Thread(target=_run, args=(project, action), daemon=True)
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


def _command(args, label, cwd=None, environment=None):
    try:
        # 앱 실행에 Dev의 인증키·알림 토큰을 물려주지 않는다. 앱은 자기 실행 설정에서 환경을 읽는다.
        env = environment if environment is not None else {k: v for k, v in os.environ.items() if not k.upper().startswith(('DEV_', 'NIGHTSHIFT_', 'CLOUDFLARE_', 'CF_')) and not re.search(r'(TOKEN|PASSWORD|SECRET|API_KEY)$', k.upper())}
        result = subprocess.run(args, cwd=cwd, capture_output=True, timeout=45,
                                env=env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode:
            if environment is not None:
                from project_audit import _safe
                detail = _safe(result.stderr.decode('utf-8', errors='replace'))[-1000:]
                raise issues.StoreError(label + '에 실패했어요. ' + detail, 409)
            raise issues.StoreError(label + '에 실패했어요. 실행 계정의 권한·설정과 기존 DNS 충돌을 확인해 주세요.', 409)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise issues.StoreError(label + '을 실행할 수 없거나 시간이 초과됐어요.', 409) from e


def _restart():
    name = os.environ.get('DEV_TUNNEL_SERVICE') or ('Cloudflared' if os.name == 'nt' else 'cloudflared')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', name):
        raise issues.StoreError('터널 서비스 이름 설정이 잘못됐어요.', 409)
    if os.name == 'nt':
        _command(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', "$ErrorActionPreference='Stop'; Restart-Service -Name '" + name + "'"], '터널 서비스 재시작')
    else:
        _command(['systemctl', 'restart', name], '터널 서비스 재시작')


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
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False).encode('utf-8'), str(data['tunnel'])


def _restore(row):
    if not row['backup_path'] or not row['applied_hash']:
        return ''
    try:
        path = Path(row['config_path'])
        original = Path(row['backup_path']).read_bytes()
        if _hash(path.read_bytes()) == _hash(original):
            _restart()
        else:
            _replace(path, original, row['applied_hash'])
            _restart()
        _save(row['project_id'], backup_path='', applied_hash='')
        return ' 기존 터널 설정을 복구했어요.'
    except Exception:
        return ' 터널 자동 복구를 완료하지 못했어요. 설정 백업과 서비스 상태를 확인해 주세요.'


def _run(project, action):
    pid = project['id']
    dns_attempted = False
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
                args = [*launcher, 'startOrRestart', 'ecosystem.config.js', '--only', app, '--update-env']
                import project_audit_runner
                app_env = {**project_audit_runner._environment(), 'PORT': str(port), 'HOST': '127.0.0.1'}
                if os.environ.get('PM2_HOME'):
                    app_env['PM2_HOME'] = os.environ['PM2_HOME']
                _command(args, 'PM2 앱 시작', str(repo), environment=app_env)
            _save(pid, phase='로컬 서비스 확인')
            _healthy(f'http://127.0.0.1:{port}/api/health')
            _save(pid, step=5, phase='DNS 연결')
            dns_attempted = True
            _command([exe, 'tunnel', '--config', str(path), 'route', 'dns', tunnel, domain], 'DNS 연결')
        backup = config.DATA_DIR / 'tunnel-backups' / (str(pid) + '-' + str(time.time_ns()) + '.yml')
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(raw)
        _save(pid, step=5, phase='터널 반영', backup_path=str(backup), config_path=str(path), applied_hash=_hash(candidate))
        _replace(path, candidate, _hash(raw))
        _restart()
        if domain:
            _save(pid, step=6, phase='외부 HTTPS 확인')
            _healthy('https://' + domain + '/api/health')
        _save(pid, status='connected' if domain else 'disconnected', phase='연결 완료' if domain else '연결 해제 완료', domain=domain, port=port, backup_path='', applied_hash='', error='')
    except Exception as e:
        with db.connect() as c:
            row = dict(c.execute('SELECT * FROM project_services WHERE project_id=?', (pid,)).fetchone())
        note = _restore(row)
        error = str(e) if isinstance(e, issues.StoreError) else '터널 설정에 접근하거나 서비스 연결을 처리하지 못했어요. 실행 계정의 권한·설정 파일을 확인해 주세요.'
        # shortcut: CLI에는 DNS 삭제 명령이 없으므로 기존 레코드를 보존한다. DNS 정리 자동화가 필요하면 API를 연결한다.
        if dns_attempted:
            note += ' DNS 레코드는 남아 있을 수 있어요. Cloudflare에서 확인해 주세요.'
        _save(pid, status='failed', phase='연결 실패', error=error + note)


def recover():
    with db.connect() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM project_services WHERE status='running'")]
    if not rows:
        return
    def restore_pending():
        for row in rows:
            _save(row['project_id'], status='failed', phase='작업 중단', error='서버가 재시작되어 연결 작업이 중단됐어요. DNS 레코드도 확인해 주세요.' + _restore(row))
    threading.Thread(target=restore_pending, daemon=True).start()
