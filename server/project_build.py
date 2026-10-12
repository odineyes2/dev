"""검사한 커밋의 웹 산출물을 격리 빌드하고 스테이징·교체·복원한다."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
import time
import uuid

import config


class BuildError(RuntimeError):
    def __init__(self, phase, message, log=None):
        self.phase, self.log = phase, str(log or '')
        super().__init__(message)


def _secret(key):
    key = key.upper()
    return key.startswith(('DEV_', 'NIGHTSHIFT_', 'CLOUDFLARE_', 'CF_', 'NEXT_PUBLIC_', 'MOAI_')) or bool(re.search(r'(TOKEN|PASSWORD|SECRET|API_KEY|DATABASE_URL)$', key))


def environment(source=None):
    return {k: v for k, v in (os.environ if source is None else source).items() if not _secret(k)}


def _path(root, value, allow_root=False):
    if not isinstance(value, str) or not value or PureWindowsPath(value).drive or value.startswith(('/', '\\')) or '..' in value.replace('\\', '/').split('/'):
        raise BuildError('config', '저장소 안 상대경로만 허용한다.')
    path = root / value
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()) or (resolved == root.resolve() and not allow_root):
        raise BuildError('config', '저장소 밖 또는 루트 경로는 허용하지 않는다.')
    # 심볼릭 링크·junction을 거쳐 운영 파일에 쓰지 않는다.
    for part in [path, *path.parents]:
        if part == root.parent:
            break
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise BuildError('config', '링크 경로는 허용하지 않는다.')
    return path


def _git(repo, *args):
    result = subprocess.run(['git', *args], cwd=repo, env=dict(os.environ), capture_output=True, text=True, encoding='utf-8')
    if result.returncode:
        raise BuildError('config', result.stderr.strip())
    return result.stdout.strip()


def declarations(repo, value, *, _isolated=False):
    root = Path(repo).resolve()
    if value is None:
        return []
    if not isinstance(value, list):
        raise BuildError('config', 'build는 선언 배열이어야 한다.')
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise BuildError('config', '빌드 선언은 객체여야 한다.')
        entry = dict(item)
        _path(root, entry.get('cwd', '.'), True)
        out = _path(root, entry.get('out'))
        relative = out.relative_to(root).as_posix()
        if any(p.lower() in ('data', '.git') or p.lower().endswith(('.db', '.sqlite', '.sqlite3')) for p in out.relative_to(root).parts) or (not _isolated and out.resolve().is_relative_to(config.DATA_DIR.resolve())):
            raise BuildError('config', '데이터 경로를 산출물로 사용할 수 없다.')
        if _git(root, 'ls-files', '--', relative) or subprocess.run(['git', 'check-ignore', '-q', '--', relative + '/'], cwd=root, env=dict(os.environ)).returncode:
            raise BuildError('config', '산출물은 tracked 파일이 없는 git 제외 경로여야 한다.')
        for previous in result:
            other = root / previous['out']
            if out == other or out.is_relative_to(other) or other.is_relative_to(out):
                raise BuildError('config', '산출물 경로가 겹친다.')
        for name in ('install', 'run'):
            command = entry.get(name)
            if name == 'install' and command is None:
                continue
            if not isinstance(command, list) or not command or not all(isinstance(a, str) and a and '\0' not in a for a in command):
                raise BuildError('config', '명령은 비어 있지 않은 인자 배열이어야 한다.')
        timeout = entry.get('timeout', 900)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
            raise BuildError('config', '빌드 제한 시간은 1~3600초여야 한다.')
        entry['timeout'] = timeout
        expect = entry.get('expect', [])
        if not isinstance(expect, list) or not expect:
            raise BuildError('config', 'expect 파일을 하나 이상 지정해야 한다.')
        for name in expect:
            _path(out, name)
        when = entry.get('when', [])
        if not isinstance(when, list) or not all(isinstance(p, str) and p for p in when):
            raise BuildError('config', 'when은 경로 패턴 배열이어야 한다.')
        result.append(entry)
    return result


def _scan(out, secrets):
    for path in out.rglob('*'):
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise BuildError('expect', '산출물에 링크가 있다.')
        if path.is_file():
            content = path.read_bytes()
            if any(value.encode() in content for value in secrets):
                raise BuildError('secret', '산출물에서 비밀 환경값을 검출했다.')


def build(repo, revision, specs, project_key, data_dir=None):
    """현재 운영 사본을 건드리지 않고 지정 커밋을 빌드한다. 반환 경로를 배포/복원에 사용한다."""
    repo = Path(repo).resolve()
    sha = _git(repo, 'rev-parse', '--verify', revision + '^{commit}')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', project_key):
        raise BuildError('config', '프로젝트 키가 올바르지 않다.')
    base = Path(data_dir or config.DATA_DIR) / 'builds' / project_key / sha / uuid.uuid4().hex
    base.mkdir(parents=True)
    work = base / 'worktree'
    log = base / 'build.log'
    secrets = [v for k, v in os.environ.items() if _secret(k) and len(v) >= 8]
    manifest = {'revision': sha, 'built_at': datetime.now(timezone.utc).isoformat(), 'log': str(log), 'outputs': []}
    added = False
    try:
        _git(repo, 'worktree', 'add', '--detach', str(work), sha)
        added = True
        # 실행기가 만든 detached worktree만 데이터 폴더 안에서 빌드한다.
        entries = declarations(work, specs, _isolated=True)
        for index, entry in enumerate(entries):
            cwd = _path(work, entry.get('cwd', '.'), True)
            out = _path(work, entry['out'])
            for phase in ('install', 'run'):
                if not entry.get(phase):
                    continue
                label = 'build' if phase == 'run' else phase
                try:
                    command = list(entry[phase])
                    if os.name == 'nt':
                        command[0] = shutil.which(command[0]) or command[0]
                    r = subprocess.run(command, cwd=cwd, env=environment(), capture_output=True, timeout=entry['timeout'], shell=False)
                    output = (r.stdout + r.stderr).decode('utf-8', 'replace')
                    for secret in secrets:
                        output = output.replace(secret, '[REDACTED]')
                    with log.open('a', encoding='utf-8') as f:
                        f.write(f'[{label}]\n{output}\n')
                    if r.returncode:
                        raise BuildError(label, output[-1500:], log)
                except (OSError, subprocess.TimeoutExpired) as error:
                    raise BuildError(label, '빌드 명령 실행 실패: ' + type(error).__name__, log) from error
            for name in entry['expect']:
                if not _path(out, name).is_file():
                    raise BuildError('expect', '필수 산출물이 없다: ' + name, log)
            _scan(out, secrets)
            (out / 'dev-release.json').write_text(json.dumps({k: manifest[k] for k in ('revision', 'built_at', 'log')}), encoding='utf-8')
            staged = base / str(index)
            shutil.copytree(out, staged)
            manifest['outputs'].append({'out': entry['out'], 'staged': str(staged)})
        (base / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        return base
    except BuildError as error:
        error.log = str(log)
        with log.open('a', encoding='utf-8') as f:
            f.write(f'[{error.phase}] {error}\n')
        raise
    except OSError as error:
        with log.open('a', encoding='utf-8') as f:
            f.write('[staging] ' + type(error).__name__ + '\n')
        raise BuildError('staging', '산출물 준비에 실패했다: ' + type(error).__name__, log) from error
    finally:
        if added:
            _git(repo, 'worktree', 'remove', '--force', str(work))


def _rename(source, target):
    for attempt in range(3):
        try:
            source.rename(target)
            return
        except OSError:
            if attempt == 2:
                raise
            time.sleep(.1)


def deploy(repo, build_dir):
    """같은 파일시스템의 준비본을 rename한다. 실패하면 교체한 모든 산출물을 복원한다."""
    root = Path(repo).resolve()
    manifest = json.loads((Path(build_dir) / 'manifest.json').read_text(encoding='utf-8'))
    swaps = []
    journal_path = Path(build_dir) / 'deployment.json'
    journal = json.loads(journal_path.read_text(encoding='utf-8')) if journal_path.exists() else {}
    try:
        for item in manifest['outputs']:
            out = _path(root, item['out'])
            # 운영 경로도 다시 검사한다.
            declarations(root, [{'out': item['out'], 'run': ['unused'], 'expect': ['dev-release.json']}])
            previous = _path(root, item['out'] + '.prev')
            marker = out / 'dev-release.json'
            staged_marker = Path(item['staged']) / 'dev-release.json'
            if marker.is_file() and marker.read_bytes() == staged_marker.read_bytes():
                continue
            incoming = out.with_name(out.name + '.incoming-' + uuid.uuid4().hex)
            out.parent.mkdir(parents=True, exist_ok=True)
            if previous.exists():
                if not out.exists() and journal.get(item['out']) is True:
                    # 같은 배포가 기존 산출물을 옮긴 직후 중단됐으면 복원하고 다시 교체한다.
                    _rename(previous, out)
                else:
                    raise BuildError('swap', '이전 복원본이 남아 있다. 먼저 배포를 확인하거나 복원해야 한다.')
            journal[item['out']] = out.exists()
            journal_path.write_text(json.dumps(journal), encoding='utf-8')
            swaps.append((out, previous, incoming, out.exists()))
            shutil.copytree(item['staged'], incoming)
            if out.exists():
                _rename(out, previous)
            _rename(incoming, out)
        return manifest
    except (OSError, BuildError) as error:
        for out, previous, incoming, existed in reversed(swaps):
            if previous.exists():
                if out.exists():
                    shutil.rmtree(out)
                _rename(previous, out)
            elif not existed and out.exists():
                shutil.rmtree(out)
            if incoming.exists():
                shutil.rmtree(incoming)
        raise BuildError('swap', str(error), manifest['log']) from error


def restore(repo, build_dir):
    manifest = json.loads((Path(build_dir) / 'manifest.json').read_text(encoding='utf-8'))
    journal_path = Path(build_dir) / 'deployment.json'
    journal = json.loads(journal_path.read_text(encoding='utf-8')) if journal_path.exists() else {}
    for item in manifest['outputs']:
        out = _path(Path(repo).resolve(), item['out'])
        previous = _path(Path(repo).resolve(), item['out'] + '.prev')
        if previous.exists():
            if out.exists():
                shutil.rmtree(out)
            _rename(previous, out)
        elif journal.get(item['out']) is False and out.exists():
            marker = out / 'dev-release.json'
            if marker.is_file() and marker.read_bytes() == (Path(item['staged']) / 'dev-release.json').read_bytes():
                shutil.rmtree(out)


def confirm(repo, build_dir):
    """확인한 배포의 복원본을 정리해 다음 배포를 허용한다."""
    manifest = json.loads((Path(build_dir) / 'manifest.json').read_text(encoding='utf-8'))
    root = Path(repo).resolve()
    for item in manifest['outputs']:
        out = _path(root, item['out'])
        previous = _path(root, item['out'] + '.prev')
        marker = out / 'dev-release.json'
        if marker.is_file() and marker.read_bytes() == (Path(item['staged']) / 'dev-release.json').read_bytes() and previous.exists():
            shutil.rmtree(previous)
