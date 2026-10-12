"""저장된 PM2 앱 재시작. 별도 프로세스가 Dev 자기 재시작 뒤 health까지 확인한다."""
from datetime import datetime, timezone
import os
import json
import shutil
import threading
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit

import httpx

import config
import db
import issues
import project_service
import project_build

ACTIVE = ('waiting', 'restarting', 'checking')
WAIT_SECONDS = 1800
HEALTH_SECONDS = 60


def _target(project):
    import orchestrate
    repo = Path(project['local_path']).resolve() if project['local_path'] else None
    if project['archived'] or not repo or not (repo / 'ecosystem.config.js').is_file():
        raise issues.StoreError('Local path의 PM2 실행 설정을 준비해 주세요.', 409)
    own = repo == config.REPO_ROOT.resolve()
    settings = orchestrate.settings(project['key'])
    app = 'dev' if own else project['service_app'] or settings.get('pm2_app', '')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', app) or (app == 'dev' and not own):
        raise issues.StoreError('프로젝트의 PM2 앱 이름을 확인해 주세요.', 409)
    port = config.PORT if own else project['service_port']
    url = f'http://127.0.0.1:{port}/api/health' if port else settings.get('health_url', '')
    parsed = urlsplit(url)
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path != '/api/health':
        raise issues.StoreError('로컬 포트 또는 루프백 health 주소를 준비해 주세요.', 409)
    try:
        port = parsed.port
    except ValueError:
        port = None
    if not port or not 1 <= port <= 65535:
        raise issues.StoreError('로컬 포트를 확인해 주세요.', 409)
    return {'repo': str(repo), 'app': app, 'health_url': url, 'self_restart': int(own), 'process_id': orchestrate.PROCESS_ID if own else ''}


def _recover(c):
    cutoff = datetime.now(timezone.utc).timestamp() - 120
    for row in c.execute("SELECT id,updated_at FROM project_app_restarts WHERE status IN ('waiting','restarting','checking')"):
        if datetime.fromisoformat(row['updated_at']).timestamp() < cutoff:
            c.execute("UPDATE project_app_restarts SET status='failed',phase='재시작 확인 중단',error='재시작 작업의 응답이 끊겼어요. 상태를 확인하고 다시 요청해 주세요.',updated_at=? WHERE id=? AND updated_at=? AND status IN ('waiting','restarting','checking')",
                      (db.now_iso(), row['id'], row['updated_at']))


def block(c, key):
    _recover(c)
    row = c.execute("SELECT r.*,p.key FROM project_app_restarts r JOIN projects p ON p.id=r.project_id WHERE r.status IN ('waiting','restarting','checking')").fetchone()
    return '앱 재시작이 끝나면 새 작업을 시작해요.' if row and (row['self_restart'] or row['key'] == key) else None


def status(key):
    project = project_service._project(key)
    with db.connect() as c:
        _recover(c)
        row = c.execute('SELECT * FROM project_app_restarts WHERE project_id=? ORDER BY id DESC LIMIT 1', (project['id'],)).fetchone()
    result = dict(row) if row else {'status': 'idle', 'mode': 'restart', 'phase': '재시작 기록이 없어요.', 'error': ''}
    result['log_tail'] = ''
    if result.get('log'):
        from project_audit import _safe
        try:
            with Path(result['log']).open('rb') as log:
                log.seek(0, 2)
                log.seek(max(0, log.tell() - 6000))
                result['log_tail'] = _safe(log.read().decode('utf-8', 'replace'))[-1500:]
        except OSError:
            pass
    try:
        target = _target(project)
        project_service.pm2_launcher()
        result.update(available=True, setup_error='', target_app=target['app'], target_health_url=target['health_url'], target_self_restart=target['self_restart'])
        if not row:
            result.update(target)
    except (issues.StoreError, ValueError) as error:
        result.update(available=False, setup_error=str(error))
    result['current_release'] = _current_release(project)
    result['deploy_available'] = False
    try:
        import orchestrate
        result['deploy_available'] = result['available'] and bool(project_build.declarations(project['local_path'], orchestrate.settings(key).get('build')))
    except (project_build.BuildError, OSError, ValueError):
        pass
    result.pop('build_json', None)
    result['project'] = project['key']
    result.pop('repo', None)
    result.pop('process_id', None)
    return result


def _current_release(project):
    """운영 산출물의 릴리스 표식을 읽는다."""
    import orchestrate
    releases = []
    try:
        root = Path(project['local_path']).resolve()
        for entry in project_build.declarations(root, orchestrate.settings(project['key']).get('build')):
            marker = project_build._path(root, entry['out']) / 'dev-release.json'
            if marker.is_file():
                body = json.loads(marker.read_text(encoding='utf-8'))
                releases.append({'out': entry['out'], 'revision': body.get('revision'), 'built_at': body.get('built_at')})
    except (project_build.BuildError, OSError, ValueError):
        pass
    return releases


def _details(aid, **values):
    with db.connect() as c:
        c.execute('UPDATE project_app_restarts SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?', (*values.values(), aid))


def _heartbeat(aid, stopped):
    while not stopped.wait(20):
        with db.connect() as c:
            c.execute("UPDATE project_app_restarts SET updated_at=? WHERE id=? AND status IN ('waiting','restarting','checking')", (db.now_iso(), aid))


def _restart(row):
    import project_audit_runner
    env = project_audit_runner._environment()
    env.update(HOST='127.0.0.1', PORT=str(urlsplit(row['health_url']).port))
    if row['self_restart']:
        env.update(DEV_DATA_DIR=str(config.DATA_DIR.resolve()), DEV_DB_PATH=str(config.DB_PATH.resolve()))
    if os.environ.get('PM2_HOME'):
        env['PM2_HOME'] = os.environ['PM2_HOME']
    project_service._command([*project_service.pm2_launcher(), 'restart', 'ecosystem.config.js', '--only', row['app'], '--update-env'],
                             'PM2 앱 재시작', row['repo'], environment=env)


def _verify_release(row, body, build_dir):
    sha = row['release_sha']
    if project_build._git(row['repo'], 'rev-parse', 'HEAD') != sha:
        raise project_build.BuildError('release', '배포 중 운영 HEAD가 바뀌었어요.')
    for name in ('revision', 'web_revision'):
        if name in body and body[name] != sha:
            raise project_build.BuildError('release', '앱의 릴리스 식별자가 빌드 커밋과 달라요.')
    manifest = json.loads((Path(build_dir) / 'manifest.json').read_text(encoding='utf-8'))
    for item in manifest['outputs']:
        marker = project_build._path(Path(row['repo']), item['out']) / 'dev-release.json'
        if json.loads(marker.read_text(encoding='utf-8')).get('revision') != sha:
            raise project_build.BuildError('release', '운영 산출물 표식이 빌드 커밋과 달라요.')
    return '' if all(k in body for k in ('revision', 'web_revision')) else '⚠️ 앱이 릴리스 식별자를 공개하지 않아 로컬 표식으로만 확인했어요'


def request(actor, key, mode='restart'):
    if actor.get('kind') != 'human':
        raise issues.StoreError('앱 재시작은 관리자만 할 수 있어요.', 403)
    if mode not in ('restart', 'deploy'):
        raise issues.StoreError('지원하지 않는 작업이에요.', 400)
    project = project_service._project(key)
    target = _target(project)
    specs = []
    if mode == 'deploy':
        import orchestrate
        try:
            specs = project_build.declarations(target['repo'], orchestrate.settings(key).get('build'))
        except project_build.BuildError as error:
            raise issues.StoreError(str(error), 409) from error
        if not specs:
            raise issues.StoreError('프로젝트 빌드 선언을 먼저 준비해 주세요.', 409)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        _recover(c)
        current = c.execute('SELECT * FROM projects WHERE id=?', (project['id'],)).fetchone()
        if not current or dict(current) != project:
            raise issues.StoreError('프로젝트 정보가 바뀌었어요. 새로고침 후 다시 요청해 주세요.', 409)
        active = c.execute("SELECT * FROM project_app_restarts WHERE status IN ('waiting','restarting','checking')").fetchone()
        if active:
            if active['project_id'] == project['id'] and active['mode'] == mode:
                return {k: v for k, v in dict(active).items() if k not in ('repo', 'process_id', 'build_json')} | {'available': True, 'project': project['key']}
            raise issues.StoreError('다른 앱의 재시작이 진행 중이에요.', 409)
        now = db.now_iso()
        aid = c.execute('''INSERT INTO project_app_restarts(project_id,actor,app,repo,health_url,self_restart,process_id,status,phase,created_at,updated_at,mode,build_json)
            VALUES(?,?,?,?,?,?,?,'waiting','진행 중인 작업 종료 대기',?,?,?,?)''',
            (project['id'], issues.actor_label(actor), target['app'], target['repo'], target['health_url'], target['self_restart'], target['process_id'], now, now, mode, json.dumps(specs))).lastrowid
    try:
        launch(aid)
    except (OSError, subprocess.SubprocessError):
        _save(aid, 'failed', '재시작 도우미 시작 실패', '재시작 도우미를 시작하지 못했어요.')
    return status(key)


def launch(aid):
    import project_audit_runner
    env = project_audit_runner._environment()
    env.update(DEV_DATA_DIR=str(config.DATA_DIR.resolve()), DEV_DB_PATH=str(config.DB_PATH.resolve()), PYTHONIOENCODING='utf-8')
    if os.environ.get('PM2_HOME'):
        env['PM2_HOME'] = os.environ['PM2_HOME']
    folder = config.DATA_DIR / 'app-restarts'
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / f'{aid}.log').open('ab') as log:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(aid)], cwd=config.REPO_ROOT / 'server', env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            creationflags=(subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if os.name == 'nt' else 0,
            start_new_session=os.name != 'nt')


def _save(aid, state, phase, error=''):
    from project_audit import _safe
    with db.connect() as c:
        return c.execute("UPDATE project_app_restarts SET status=?,phase=?,error=?,updated_at=? WHERE id=? AND status IN ('waiting','restarting','checking')",
            (state, phase, _safe(error)[:4000], db.now_iso(), aid)).rowcount


def _busy(c, row):
    import jobs
    import rollback
    lanes = jobs._active_lanes(c)
    key = c.execute('SELECT key FROM projects WHERE id=?', (row['project_id'],)).fetchone()['key']
    if (lanes if row['self_restart'] else any(lane[0] == key for lane in lanes)):
        return True
    if c.execute("SELECT 1 FROM project_services WHERE status='running'").fetchone():
        return True
    return any(r['phase'] not in ('complete', 'failed') and (row['self_restart'] or r['project_key'] == key) for r in rollback._records())


def run(aid):
    with db.connect() as c:
        row = c.execute('SELECT * FROM project_app_restarts WHERE id=?', (aid,)).fetchone()
    if not row or row['status'] != 'waiting':
        return
    row = dict(row)
    build_dir = None
    swapped = False
    phase = '앱 재시작 실패'
    stopped = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat, args=(aid, stopped), daemon=True)
    heartbeat.start()
    try:
        end = time.monotonic() + WAIT_SECONDS
        while True:
            with db.connect() as c:
                busy = _busy(c, row)
            if not _save(aid, 'waiting', '진행 중인 작업 종료 대기'):
                return
            if not busy:
                break
            if time.monotonic() >= end:
                raise issues.StoreError('작업 종료 대기 시간이 초과됐어요. 앱은 재시작하지 않았어요.', 409)
            time.sleep(2)
        if row['mode'] == 'deploy':
            phase = '웹 빌드 중'
            _save(aid, 'restarting', phase)
            row['release_sha'] = project_build._git(row['repo'], 'rev-parse', 'HEAD')
            _details(aid, release_sha=row['release_sha'])
            with db.connect() as c:
                key = c.execute('SELECT key FROM projects WHERE id=?', (row['project_id'],)).fetchone()['key']
            build_dir = project_build.build(row['repo'], row['release_sha'], json.loads(row['build_json']), key)
            manifest = json.loads((build_dir / 'manifest.json').read_text(encoding='utf-8'))
            _details(aid, build_dir=str(build_dir), log=manifest['log'])
            if project_build._git(row['repo'], 'rev-parse', 'HEAD') != row['release_sha']:
                raise project_build.BuildError('release', '빌드 중 운영 HEAD가 바뀌었어요. 배포하지 않았어요.')
            phase = '산출물 교체 중'
            _save(aid, 'restarting', phase)
            project_build.deploy(row['repo'], build_dir)
            swapped = True
        phase = '앱 재시작 실패'
        _save(aid, 'restarting', 'PM2 앱 재시작')
        if row['self_restart']:
            time.sleep(2)
        _restart(row)
        phase = '재시작 후 로컬 접속 확인 실패' if build_dir else phase
        _save(aid, 'checking', '재시작 후 로컬 접속 확인')
        end = time.monotonic() + HEALTH_SECONDS
        with httpx.Client(timeout=2, trust_env=False) as client:
            while time.monotonic() < end:
                try:
                    response = client.get(row['health_url'], params={'execution_identity': 'true'} if row['self_restart'] else {})
                    body = response.json() if (row['self_restart'] or build_dir) and response.status_code == 200 else {}
                    body = body if isinstance(body, dict) else {}
                    identity = body.get('process_id') if isinstance(body, dict) else None
                    if response.status_code == 200 and (not row['self_restart'] or identity not in (None, '', row['process_id'])):
                        warning = ''
                        if build_dir:
                            _details(aid, reported_revision=str(body.get('revision', '')), reported_web_revision=str(body.get('web_revision', '')))
                            warning = _verify_release(row, body, build_dir)
                        _save(aid, 'complete', '빌드·배포·접속 확인 완료' if build_dir else '앱 재시작·접속 확인 완료', warning)
                        if build_dir:
                            # 검증 완료 뒤 복원본 정리 오류로 정상 배포를 되돌리지 않는다.
                            for item in manifest['outputs']:
                                previous = project_build._path(Path(row['repo']), item['out'] + '.prev')
                                if previous.exists():
                                    try:
                                        shutil.rmtree(previous)
                                    except OSError:
                                        _details(aid, error=warning + '\n이전 복원본 정리에 실패했어요. 다음 배포 전에 확인해 주세요.')
                        return
                except (httpx.HTTPError, ValueError):
                    pass
                time.sleep(1)
        raise issues.StoreError('재시작 후 로컬 health 확인에 실패했어요.', 409)
    except Exception as error:
        detail = str(error) if isinstance(error, issues.StoreError) else f'재시작 작업 오류({type(error).__name__}). 도우미 로그를 확인해 주세요.'
        if isinstance(error, project_build.BuildError):
            phase = '빌드 단계: ' + error.phase
            detail = str(error)[-1500:]
            _details(aid, log=error.log or (manifest['log'] if build_dir else ''))
        if swapped:
            try:
                project_build.restore(row['repo'], build_dir)
                _restart(row)
                detail += '\n이전 산출물을 복원하고 다시 재시작했어요.'
            except Exception as recovery_error:
                detail += '\n복원/재시작 실패: ' + type(recovery_error).__name__
        _save(aid, 'failed', phase, detail)
    finally:
        stopped.set()
        heartbeat.join(timeout=2)


if __name__ == '__main__':
    run(int(sys.argv[1]))
