"""인증 에이전트의 기존 저장소 신규 등록과 본인 서비스 연결 권한을 제한한다."""
import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path

import config
import db
import issues

ROOT = config.REPO_ROOT.parent
SETUP_FIELDS = ('local_path', 'service_domain', 'service_port', 'service_app', 'service_prepare_provider')


def _agent(c, actor):
    if not actor or actor.get('kind') != 'agent' or not isinstance(actor.get('id'), int):
        raise issues.StoreError('인증된 에이전트 키가 필요해요.', 403)
    row = c.execute("SELECT enabled,kind FROM agents WHERE id=?", (actor['id'],)).fetchone()
    if not row or not row['enabled'] or row['kind'] != 'agent':
        raise issues.StoreError('활성 에이전트 키가 필요해요.', 403)


def _repo(value):
    if not isinstance(value, str) or not value.strip():
        raise issues.StoreError('기존 저장소의 로컬 경로가 필요해요.')
    path = Path(value)
    root = ROOT.resolve()
    try:
        repo = path.resolve(strict=True)
        if (not path.is_absolute() or not repo.is_dir() or repo.parent != root
                or repo == config.REPO_ROOT.resolve() or repo.name.startswith('.')
                or path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())):
            raise ValueError()
        command = ['git', '-c', 'core.hooksPath=', '-C', str(repo)]
        options = dict(capture_output=True, text=True, encoding='utf-8', timeout=10,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        result = subprocess.run([*command, 'rev-parse', '--show-toplevel'], **options)
        if result.returncode or Path(result.stdout.strip()).resolve() != repo:
            raise ValueError()
        head = subprocess.run([*command, 'rev-parse', '--verify', 'HEAD'], **options)
        if head.returncode or not re.fullmatch(r'[a-f0-9]{40,64}', head.stdout.strip()):
            raise ValueError()
        if not (repo / 'ecosystem.config.js').is_file():
            raise issues.StoreError('기존 ecosystem.config.js를 먼저 준비해 주세요. 유료 자동 생성은 하지 않아요.', 409)
        return repo
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise issues.StoreError('Projects 바로 아래의 실제 Git 저장소와 첫 커밋이 필요해요. 링크·다른 저장소의 하위 폴더·dev 자체는 등록할 수 없어요.') from None


def register(actor, body):
    """동일 소유자·동일 요청만 재사용하며 기존 프로젝트의 정보·권한은 바꾸지 않는다."""
    with db.connect() as c:
        _agent(c, actor)
    repo = _repo(body.get('local_path'))
    key = str(body.get('key') or '').strip().upper()
    if not re.fullmatch(r'[A-Z][A-Z0-9]{0,9}', key):
        raise issues.StoreError('키는 영문 대문자로 시작하는 10자 이하 영문·숫자예요.')
    name = issues._text(body.get('name'), '이름', 100, required=True).strip()
    repo_url = issues._text(body.get('repo_url', ''), '저장소 주소', 500).strip()
    description = issues._text(body.get('description', ''), '프로젝트 설명')
    import project_service
    service = project_service.validate_fields(body.get('service_domain', ''), body.get('service_port', 0), body.get('service_app', ''))
    if not service['service_domain'] or not service['service_port'] or not service['service_app'] or service['service_app'] == 'dev':
        raise issues.StoreError('새 서비스의 도메인·로컬 포트·PM2 앱 이름을 모두 지정해 주세요.')
    setup = {'local_path': str(repo), **service, 'service_prepare_provider': 'none'}
    request = {'key': key, 'name': name, 'repo_url': repo_url, 'description': description, **setup}
    try:
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            _agent(c, actor)
            previous = c.execute('SELECT * FROM projects WHERE key=?', (key,)).fetchone()
            if previous:
                registration = c.execute('SELECT * FROM agent_project_registrations WHERE project_id=?', (previous['id'],)).fetchone()
                if (not registration or registration['agent_id'] != actor['id']
                        or json.loads(registration['request_json']) != request or previous['archived']
                        or any(previous[k] != setup[k] for k in SETUP_FIELDS)):
                    raise issues.StoreError('이미 등록된 프로젝트예요. 기존 프로젝트의 권한이나 설정은 가져올 수 없어요.', 409)
                return issues._project_row(previous)
            for row in c.execute('SELECT local_path,service_domain,service_port,service_app FROM projects'):
                if ((row['local_path'] and Path(row['local_path']).resolve() == repo)
                        or row['service_domain'] == service['service_domain']
                        or row['service_port'] == service['service_port']
                        or row['service_app'] == service['service_app']):
                    raise issues.StoreError('다른 프로젝트가 쓰는 경로·도메인·포트·앱 이름이에요.', 409)
            project_service.check_new_domain(service['service_domain'])
            pid = c.execute('''INSERT INTO projects(key,name,repo_url,local_path,description,created_at,
                    service_domain,service_port,service_app,service_prepare_provider) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (key, name, repo_url, str(repo), description, db.now_iso(), *service.values(), 'none')).lastrowid
            c.execute('INSERT INTO agent_project_registrations(project_id,agent_id,request_json,created_at) VALUES(?,?,?,?)',
                      (pid, actor['id'], json.dumps(request, ensure_ascii=False, sort_keys=True), db.now_iso()))
            return issues._project_row(c.execute('SELECT * FROM projects WHERE id=?', (pid,)).fetchone())
    except sqlite3.IntegrityError:
        raise issues.StoreError('같은 키 또는 서비스 도메인의 프로젝트가 있어요.', 409) from None


def authorize_connection(c, actor, project):
    """서비스 쓰기 잠금 안에서 소유자와 등록 당시 설정을 다시 확인한다."""
    _agent(c, actor)
    record = c.execute('SELECT * FROM agent_project_registrations WHERE project_id=?', (project['id'],)).fetchone()
    if not record or record['agent_id'] != actor['id']:
        raise issues.StoreError('직접 신규 등록한 프로젝트만 연결할 수 있어요.', 403)
    request = json.loads(record['request_json'])
    if any(project[k] != request[k] for k in SETUP_FIELDS):
        raise issues.StoreError('관리자가 프로젝트 실행 설정을 변경했어요. 관리자 화면에서 연결해 주세요.', 409)
    _repo(project['local_path'])


def connect(actor, key):
    if not actor or actor.get('kind') != 'agent':
        raise issues.StoreError('인증된 에이전트 키가 필요해요.', 403)
    import project_service
    return project_service.request(actor, key, allow_agent=True)
