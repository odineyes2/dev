"""프로젝트 PM2 앱 신원과 소유권을 검사하며 정지·재연결 기록을 관리한다."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import config
import db
import issues


class AppError(Exception):
    pass


def path_key(value):
    return os.path.normcase(str(Path(value).resolve()))


def inside(child, parent):
    try:
        return os.path.commonpath((child, parent)) == parent
    except ValueError:
        return False


def run_json(args, cwd=None, loader=False):
    import project_service as service
    env = service.pm2_environment()
    if loader:
        env = {k: v for k, v in env.items() if k.upper() in
               ('PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'HOME', 'USERPROFILE')}
    try:
        # jlist에는 env가 있으므로 디스크·로그에 남기지 않고 메모리만 제한한다.
        chunks, overflow = [], threading.Event()
        with subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0) as process:
            def read_output():
                total = 0
                while True:
                    chunk = process.stdout.read1(65536)
                    if not chunk:
                        return
                    total += len(chunk)
                    if total > 1024 * 1024:
                        overflow.set()
                        process.kill()
                        return
                    chunks.append(chunk)
            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise AppError('시간 초과') from None
            finally:
                reader.join(timeout=1)
            if overflow.is_set() or reader.is_alive():
                raise AppError('응답 크기 또는 읽기 한도 초과')
            if process.returncode:
                raise AppError('종료 코드 오류')
            value = json.loads(b''.join(chunks))
            if not isinstance(value, list):
                raise ValueError()
            return value
    except subprocess.TimeoutExpired:
        raise AppError('시간 초과') from None
    except (OSError, ValueError, TypeError):
        raise AppError('앱 정보를 읽을 수 없어요') from None


def declarations(project):
    root = path_key(project['local_path']) if project['local_path'] else ''
    if not root or not Path(root, 'ecosystem.config.js').is_file():
        raise AppError('프로젝트 ecosystem.config.js가 없어요')
    node = shutil.which('node')
    if not node:
        raise AppError('Node.js를 찾을 수 없어요')
    rows = run_json([node, str(Path(__file__).with_suffix('.js')), root], root, True)
    result = []
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get('name'), str) or
                not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', row['name']) or
                not isinstance(row.get('cwd'), str) or not row['cwd'] or
                not isinstance(row.get('script'), str) or not row['script']):
            raise AppError('앱 메타데이터가 잘못됐어요')
        cwd = path_key(Path(root) / row['cwd'])
        result.append(dict(name=row['name'], cwd=cwd, exec=path_key(Path(cwd) / row['script'])))
    if len({r['name'] for r in result}) != len(result):
        raise AppError('중복된 앱 선언이 있어요')
    return root, result


def processes():
    import project_service as service
    try:
        rows = run_json([*service.pm2_launcher(), 'jlist'])
    except issues.StoreError:
        raise AppError('PM2를 찾을 수 없어요') from None
    result = []
    for row in rows:
        try:
            env = row['pm2_env']
            if (type(row['pm_id']) is not int or row['pm_id'] < 0 or
                    not isinstance(row['name'], str) or
                    not all(isinstance(env[k], str) and env[k] for k in ('pm_cwd', 'pm_exec_path', 'status'))):
                raise ValueError()
            result.append(dict(id=row['pm_id'], name=row['name'], cwd=path_key(env['pm_cwd']),
                               exec=path_key(env['pm_exec_path']), status=env['status']))
        except (KeyError, TypeError, ValueError):
            raise AppError('PM2 앱 메타데이터가 잘못됐어요') from None
    if len({r['id'] for r in result}) != len(result):
        raise AppError('PM2 앱 ID가 중복돼요')
    return result


def identity(row):
    return row['name'], row['cwd'], row['exec']


def owned(project, root, declaration, projects):
    if declaration['name'] == 'dev' or not inside(declaration['cwd'], root):
        return False
    if inside(declaration['cwd'], path_key(config.REPO_ROOT)):
        return False
    for other in projects:
        if other['id'] == project['id']:
            continue
        if other['service_app'] == declaration['name']:
            return False
        if other['local_path']:
            other_root = path_key(other['local_path'])
            if inside(declaration['cwd'], other_root) or inside(root, other_root) or inside(other_root, root):
                return False
    return True


def snapshot(project, connection=None):
    root, declared = declarations(project)
    current = processes()
    if connection is None:
        with db.connect() as c:
            projects = [dict(r) for r in c.execute('SELECT * FROM projects')]
    else:
        projects = [dict(r) for r in connection.execute('SELECT * FROM projects')]
    owner = next((p for p in projects if p['id'] == project['id']), None)
    if not owner or any(owner[k] != project[k] for k in ('local_path', 'service_app', 'service_domain', 'service_port')):
        raise AppError('프로젝트 설정이 바뀌었어요')
    allowed = [r for r in declared if owned(project, root, r, projects)]
    # 이름 기반 재연결도 안전하도록 다른 신원의 동명 프로세스가 있으면 제외한다.
    allowed = [r for r in allowed if not any(p['name'] == r['name'] and identity(p) != identity(r) for p in current)]
    candidates = sorted([p for p in current if any(identity(p) == identity(r) for r in allowed)], key=lambda p: p['id'])
    payload = dict(project=project, ecosystem=hashlib.sha256(Path(root, 'ecosystem.config.js').read_bytes()).hexdigest(),
                   declared=declared, allowed=allowed, candidates=candidates)
    result = dict(root=root, declared=allowed, candidates=candidates)
    result['token'] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return result


def preview(project, connection=None):
    try:
        snap = snapshot(project, connection)
        apps = []
        for row in snap['candidates']:
            group = next((a for a in apps if (a['name'], a['cwd']) == (row['name'], row['cwd'])), None)
            if group is None:
                group = dict(name=row['name'], ids=[], cwd=row['cwd'], status=row['status'])
                apps.append(group)
            group['ids'].append(row['id'])
            if group['status'] != row['status']:
                group['status'] = 'mixed'
        result = dict(apps=apps, stop_default=bool(project['service_app']), available=True, reason='')
        result['token'] = snap['token']
        return result
    except (AppError, OSError):
        return dict(apps=[], stop_default=bool(project['service_app']), available=False,
                    reason='앱 목록을 확인하지 못했어요. 프로젝트 설정과 PM2 실행 상태를 확인해 주세요.', token='')


def before_connect(project):
    """주 앱 시작 전 requested를 대조하고 기억한 주 앱의 이름 충돌도 막는다."""
    reconcile(project)
    main = [r for r in records(project['id']) if r['app_name'] == project['service_app']]
    if main:
        try:
            snap = snapshot(project)
            for rec in main:
                if rec['repo_path'] != snap['root'] or not any(
                        identity(r) == (rec['app_name'], rec['app_cwd'], rec['exec_path']) for r in snap['declared']):
                    raise AppError('대상 변경')
        except (AppError, OSError):
            raise issues.StoreError('기억한 주 앱의 소유권이 바뀌었어요. 프로젝트 설정과 PM2 앱을 확인해 주세요.', 409) from None


def records(pid):
    with db.connect() as c:
        return [dict(r) for r in c.execute('SELECT * FROM project_service_stopped_apps WHERE project_id=?', (pid,))]


def remember(project, row, state):
    with db.connect() as c:
        c.execute('''INSERT INTO project_service_stopped_apps
            (project_id,repo_path,app_name,app_cwd,exec_path,was_online,state,updated_at)
            VALUES(?,?,?,?,?,1,?,?) ON CONFLICT(project_id,app_name,app_cwd,exec_path)
            DO UPDATE SET state=excluded.state,updated_at=excluded.updated_at''',
            (project['id'], path_key(project['local_path']), row['name'], row['cwd'], row['exec'], state, db.now_iso()))


def command(args, cwd=None, environment=None):
    import project_service as service
    try:
        service._command([*service.pm2_launcher(), *args], 'PM2 앱 처리', cwd,
                         environment=environment or service.pm2_environment())
    except Exception as e:
        if isinstance(e, subprocess.TimeoutExpired) or isinstance(e.__cause__, subprocess.TimeoutExpired):
            raise AppError('시간 초과') from None
        raise AppError('종료 코드 오류') from None


def stop(project, approved):
    import project_service as service
    errors = []
    for target in approved:
        try:
            snap = snapshot(project)
            row = next((r for r in snap['candidates'] if r['id'] == target['id'] and identity(r) == identity(target)), None)
            if row is None:
                raise AppError('대상 변경')
            if row['status'] == 'stopped':
                continue
            if row['status'] != 'online':
                raise AppError('실행 상태를 확인할 수 없어요')
            remember(project, row, 'requested')
            command(['stop', str(row['id'])])
            current = processes()
            if not any(r['id'] == row['id'] and identity(r) == identity(row) and r['status'] == 'stopped' for r in current):
                raise AppError('정지 확인 실패')
            # cluster는 같은 영속 신원을 공유한다. 마지막 인스턴스 확인까지 requested로 남긴다.
            if not any(identity(r) == identity(row) and r['status'] != 'stopped' for r in current):
                remember(project, row, 'stopped')
        except (AppError, OSError) as exc:
            why = str(exc) if isinstance(exc, AppError) else '앱 정보 확인 실패'
            errors.append(f"터널 연결 해제는 끝났어요. {target['name']} 앱을 멈추지 못했어요: {why}.")
    if approved and service.save_pm2():
        errors.append('앱은 멈췄지만 PM2 목록 저장에 실패했어요. 재부팅 뒤 다시 켜질 수 있어요.')
    return '\n'.join(errors)


def reconcile(project):
    """중단된 requested 기록은 상태만 대조하며 프로세스를 변경하지 않는다."""
    pending = [r for r in records(project['id']) if r['state'] == 'requested']
    if not pending:
        return
    try:
        current = processes()
        for rec in pending:
            matches = [r for r in current if identity(r) == (rec['app_name'], rec['app_cwd'], rec['exec_path'])]
            if matches and all(r['status'] == 'stopped' for r in matches):
                remember(project, matches[0], 'stopped')
    except AppError:
        pass


def restore(project):
    """health 이후 기억한 앱만 복구한다. 저장은 호출자가 마지막에 한 번 수행한다."""
    reconcile(project)
    warnings, restored = [], []
    for rec in records(project['id']):
        if rec['state'] != 'stopped':
            warnings.append(f"{rec['app_name']} 앱의 중단 전 정지를 확인하지 못했어요. PM2 상태를 확인해 주세요.")
            continue
        if not rec['was_online']:
            continue
        try:
            snap = snapshot(project)
            key = (rec['app_name'], rec['app_cwd'], rec['exec_path'])
            if rec['repo_path'] != snap['root'] or not any(identity(r) == key for r in snap['declared']):
                raise AppError('대상 변경')
            rows = [r for r in snap['candidates'] if identity(r) == key]
            if rec['app_name'] != project['service_app'] and not (rows and all(r['status'] == 'online' for r in rows)):
                import project_service as service
                command(['startOrRestart', 'ecosystem.config.js', '--only', rec['app_name'], '--update-env'],
                        snap['root'], service.pin_pm2_environment(service.app_environment(snap['root'], project['service_port'])))
            end = time.monotonic() + 15
            while True:
                rows = [r for r in processes() if identity(r) == key]
                if rows and all(r['status'] == 'online' for r in rows):
                    break
                if time.monotonic() >= end:
                    raise AppError('실행 확인 실패')
                time.sleep(.5)
            restored.append(rec)
        except (AppError, OSError):
            warnings.append(f"{rec['app_name']} 앱을 다시 켜지 못했어요. 앱 선언·소유권과 실행 상태를 확인해 주세요.")
    return '\n'.join(warnings), restored


def forget(restored):
    with db.connect() as c:
        for rec in restored:
            c.execute('DELETE FROM project_service_stopped_apps WHERE project_id=? AND app_name=? AND app_cwd=? AND exec_path=?',
                      (rec['project_id'], rec['app_name'], rec['app_cwd'], rec['exec_path']))
