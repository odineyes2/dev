"""프로젝트 비밀값(DEV-239) — `.env.example`의 키 값을 그 프로젝트의 `<local_path>/.env`에만 쓴다. 값은 DB·이벤트·응답에 남기지 않는다."""
import os
import re
import subprocess
import tempfile
from pathlib import Path

import config
import issues
import project_service

KEY_LINE = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=')
# dev ecosystem.config.js의 loadDotEnv와 같은 규칙 — 따옴표 한 겹만 벗긴다.
ENV_LINE = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$')
DEV_ONLY = ('NTFY_TOPIC', 'NTFY_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN', 'DEV_CODEX_AGENT_KEY')
MAX_VALUE = 4096


def _own(repo) -> bool:
    return Path(repo).resolve() == config.REPO_ROOT.resolve()


def _repo(project) -> Path:
    if not project['local_path']:
        raise issues.StoreError('Local path를 먼저 정해 주세요.', 409)
    repo = Path(project['local_path']).resolve()
    if _own(repo):
        raise issues.StoreError('dev 자신의 .env는 여기서 바꾸지 않아요.', 409)
    if not repo.is_dir():
        raise issues.StoreError('Local path의 프로젝트 폴더를 찾을 수 없어요.', 409)
    return repo


def _lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding='utf-8-sig').splitlines()
    except (OSError, UnicodeDecodeError):
        return []


def keys(repo) -> list[str]:
    """`.env.example`의 키를 순서대로(중복 제거) 읽는다."""
    found = []
    for line in _lines(Path(repo) / '.env.example'):
        m = KEY_LINE.match(line)
        if m and m.group(1) not in found:
            found.append(m.group(1))
    return found


def env_for(repo) -> dict:
    """pm2로 앱을 띄울 때 넘길 `.env` 키·값. dev 자신은 빈 사전이다."""
    if not repo or _own(repo):
        return {}
    out = {}
    for line in _lines(Path(repo) / '.env'):
        m = ENV_LINE.match(line)
        if m:
            out[m.group(1)] = re.sub(r'''^(['"])(.*)\1$''', r'\2', m.group(2))
    return out


def _dev_available(name) -> bool:
    return not name.startswith('DEV_') and name not in DEV_ONLY and bool(os.environ.get(name, '').strip())


def status(actor, key) -> dict:
    """키마다 설정 여부. 사람에게만 끝 4자(8자 이상일 때)와 dev 값 사용 가능 여부를 준다."""
    project = project_service._project(key)
    repo = _repo(project)
    current = env_for(repo)
    human = actor.get('kind') == 'human'
    items = []
    for name in keys(repo):
        value = current.get(name, '')
        item = {'key': name, 'set': bool(value)}
        if human:
            item.update(last4=value[-4:] if len(value) >= 8 else '', dev_available=_dev_available(name))
        items.append(item)
    return {'project': project['key'], 'keys': items}


def _value(name, raw) -> str:
    value = raw.strip()
    if not 1 <= len(value) <= MAX_VALUE:
        raise issues.StoreError(f'{name} 값은 1~{MAX_VALUE}자예요.', 400)
    if any(ch in value for ch in '\r\n\0"\\'):
        raise issues.StoreError(f'{name} 값에 줄바꿈·NUL·큰따옴표·역슬래시는 넣을 수 없어요.', 400)
    return f'"{value}"' if re.search(r"[\s#']", value) else value


def _check_ignored(repo: Path):
    try:
        r = subprocess.run(['git', 'check-ignore', '-q', '.env'], cwd=repo, capture_output=True, timeout=30,
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    except (OSError, subprocess.SubprocessError):
        raise issues.StoreError('.gitignore 확인에 실패했어요.', 409)
    if r.returncode != 0:
        raise issues.StoreError('프로젝트 .gitignore가 .env를 무시하지 않아요. 먼저 .gitignore에 .env를 넣어 주세요.', 409)


def _write(path: Path, updates: dict):
    """해당 키 줄만 바꾸거나 지우고 나머지 줄·주석·순서는 그대로 둔다. 같은 폴더 임시 파일 → os.replace."""
    out, done = [], set()
    for line in _lines(path):
        m = KEY_LINE.match(line)
        name = m.group(1) if m else None
        if name in updates:
            if updates[name] is not None and name not in done:
                out.append(f'{name}={updates[name]}')
            done.add(name)
            continue
        out.append(line)
    out += [f'{name}={value}' for name, value in updates.items() if value is not None and name not in done]
    fd, temp = tempfile.mkstemp(prefix='.env.dev-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            f.write('\n'.join(out) + ('\n' if out else ''))
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def _restart(actor, project) -> dict:
    if not project['service_app']:
        return {'status': 'skipped'}
    import project_restart
    try:
        result = project_restart.request(actor, project['key'])
    except issues.StoreError as error:
        return {'status': 'failed', 'error': str(error)}
    return {'status': result.get('status', ''), 'phase': result.get('phase', ''), 'error': result.get('error', '')}


def save(actor, key, changes) -> dict:
    """사람만. changes = {KEY: {"value": str} | {"clear": true} | {"from_dev": true}}. 오류 문구에는 키 이름만 넣는다."""
    if actor.get('kind') != 'human':
        raise issues.StoreError('비밀값은 관리자만 바꿀 수 있어요.', 403)
    if not isinstance(changes, dict) or not changes:
        raise issues.StoreError('바꿀 값이 없어요.', 400)
    project = project_service._project(key)
    repo = _repo(project)
    allowed = keys(repo)
    updates = {}
    for name, change in changes.items():
        if name not in allowed:
            raise issues.StoreError(f'.env.example에 없는 키예요: {str(name)[:80]}', 400)
        if not isinstance(change, dict) or len(change) != 1:
            raise issues.StoreError(f'{name} 변경 형식이 맞지 않아요.', 400)
        if change.get('clear') is True:
            updates[name] = None
        elif change.get('from_dev') is True:
            if not _dev_available(name):
                raise issues.StoreError(f'{name}는 dev 값을 가져올 수 없는 키예요.', 400)
            updates[name] = _value(name, os.environ[name])
        elif isinstance(change.get('value'), str):
            updates[name] = _value(name, change['value'])
        else:
            raise issues.StoreError(f'{name} 변경 형식이 맞지 않아요.', 400)
    _check_ignored(repo)
    _write(repo / '.env', updates)
    return {**status(actor, project['key']), 'restart': _restart(actor, project)}
