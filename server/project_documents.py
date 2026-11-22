"""프로젝트 원격 저장소 기본 브랜치의 /docs를 격리 Git 캐시에서 읽는다. 원본·DB·작업 트리는 수정하지 않는다."""
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import threading
import time

import config
import db
import issues
from repo_snapshot import contains_sensitive

TEXT_TYPES = {'.md', '.markdown', '.txt'}
IMAGE_TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
               '.gif': 'image/gif', '.webp': 'image/webp'}
MAX_TEXT_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_DOCUMENTS = 1000
CACHE_SECONDS = 60
_lock = threading.RLock()
_snapshots = {}


def _repository(value):
    """인증값 없는 GitHub 주소만 정규화한다. 로컬·임의 호스트·Git 외부 프로토콜을 받지 않는다."""
    match = re.fullmatch(r'(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)'
                         r'([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?', str(value).strip())
    if not match or any(x in ('.', '..') for x in match.groups()):
        raise issues.StoreError('프로젝트 Repository에 GitHub 저장소 주소를 저장해 주세요.', 409)
    owner, name = match.groups()
    ssh = not str(value).strip().startswith('https:')
    return f'git@github.com:{owner}/{name}.git' if ssh else f'https://github.com/{owner}/{name}.git'


def _git(repo, *args):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
    env.update(GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='Never', GIT_OPTIONAL_LOCKS='0',
               GIT_SSH_COMMAND='ssh -oBatchMode=yes -oStrictHostKeyChecking=yes')
    try:
        result = subprocess.run(['git', '--no-replace-objects', '-c', 'credential.interactive=false',
                                 '-c', 'protocol.file.allow=never', '-c', 'protocol.ext.allow=never',
                                 '-c', 'http.followRedirects=false', '-c', 'maintenance.auto=false',
                                 *args], cwd=repo, capture_output=True, timeout=30, env=env,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise issues.StoreError('원격 문서를 읽지 못했어요. 저장소 접근 권한과 서버 Git 인증을 확인해 주세요.', 502) from e
    if result.returncode:
        # Git 오류 원문에는 URL·자격 정보가 포함될 수 있으므로 반환하지 않는다.
        raise issues.StoreError('원격 문서를 읽지 못했어요. 저장소 주소·기본 브랜치와 서버 Git 인증을 확인해 주세요.', 502)
    return result.stdout


def _path(value):
    value = str(value)
    parts = value.split('/')
    if (len(parts) < 2 or parts[0] != 'docs' or '\\' in value or ':' in value
            or any(not p or p.startswith('.') or any(ord(c) < 32 for c in p) for p in parts)):
        raise issues.StoreError('/docs 안의 문서 경로를 선택해 주세요.', 400)
    return value


def _snapshot(key, refresh=False):
    with db.connect() as c:
        row = c.execute('SELECT key,repo_url FROM projects WHERE key=?', (str(key).upper(),)).fetchone()
    if not row:
        raise issues.StoreError('프로젝트를 찾을 수 없어요.', 404)
    remote = _repository(row['repo_url'])
    identity = hashlib.sha256(remote.encode()).hexdigest()
    cached = _snapshots.get(identity)
    if cached and not refresh and time.monotonic() - cached['loaded'] < CACHE_SECONDS:
        return cached
    root = config.DATA_DIR / 'document-repositories' / identity
    root.mkdir(parents=True, exist_ok=True)
    if not (root / 'HEAD').exists():
        _git(root, 'init', '--bare', '--quiet')
    _git(root, 'config', 'remote.origin.url', remote)
    _git(root, 'config', 'remote.origin.promisor', 'true')
    _git(root, 'config', 'remote.origin.partialclonefilter', 'blob:none')
    info = _git(root, 'ls-remote', '--symref', 'origin', 'HEAD').decode('utf-8')
    match = re.search(r'^ref: refs/heads/([^\t\n]+)\tHEAD$', info, re.M)
    if not match:
        raise issues.StoreError('원격 저장소의 기본 브랜치를 찾지 못했어요.', 409)
    branch = match.group(1)
    _git(root, 'check-ref-format', 'refs/heads/' + branch)
    _git(root, 'fetch', '--quiet', '--no-tags', '--depth=1', '--filter=blob:none', 'origin', 'refs/heads/' + branch)
    sha = _git(root, 'rev-parse', '--verify', 'FETCH_HEAD^{commit}').decode('ascii').strip()
    entries = {}
    for item in _git(root, 'ls-tree', '-rz', sha, '--', 'docs/').split(b'\0'):
        if not item:
            continue
        meta, path_bytes = item.split(b'\t', 1)
        mode, kind, oid = meta.decode('ascii').split()
        try:
            path = _path(path_bytes.decode('utf-8'))
        except (UnicodeDecodeError, issues.StoreError):
            continue
        suffix = PurePosixPath(path).suffix.lower()
        if mode in ('100644', '100755') and kind == 'blob' and suffix in TEXT_TYPES | IMAGE_TYPES.keys():
            entries[path] = oid
    documents = sorted((p for p in entries if PurePosixPath(p).suffix.lower() in TEXT_TYPES), key=str.casefold)
    if len(documents) > MAX_DOCUMENTS:
        raise issues.StoreError('문서가 1,000개를 넘어요. /docs 범위를 정리해 주세요.', 413)
    preferred = next((p for name in ('handoff.md', 'readme.md', 'index.md')
                      for p in documents if p.lower() == 'docs/' + name), None)
    snapshot = {'root': root, 'remote': remote, 'branch': branch, 'commit': sha, 'checked_at': db.now_iso(),
                'loaded': time.monotonic(), 'entries': entries, 'documents': documents,
                'default_path': preferred or (documents[0] if documents else None)}
    _snapshots[identity] = snapshot
    return snapshot


def _metadata(snapshot, key):
    return {'project': str(key).upper(), 'source': 'remote', 'branch': snapshot['branch'],
            'commit': snapshot['commit'], 'checked_at': snapshot['checked_at'],
            'cache_seconds': CACHE_SECONDS}


def list_documents(key, refresh=False):
    with _lock:
        s = _snapshot(key, refresh)
        return {**_metadata(s, key), 'default_path': s['default_path'],
                'documents': [{'path': p, 'name': PurePosixPath(p).name,
                               'format': 'text' if p.lower().endswith('.txt') else 'markdown'}
                              for p in s['documents']]}


def _blob(s, path, maximum):
    oid = s['entries'].get(path)
    if not oid:
        raise issues.StoreError('기본 브랜치의 /docs에서 파일을 찾지 못했어요.', 404)
    size = int(_git(s['root'], 'cat-file', '-s', oid))
    if size > maximum:
        raise issues.StoreError('미리보기 용량 제한을 넘는 파일이에요.', 413)
    return _git(s['root'], 'cat-file', 'blob', oid)


def read_document(key, path, refresh=False):
    path = _path(path)
    if PurePosixPath(path).suffix.lower() not in TEXT_TYPES:
        raise issues.StoreError('Markdown 또는 텍스트 문서를 선택해 주세요.', 400)
    with _lock:
        s = _snapshot(key, refresh)
        try:
            content = _blob(s, path, MAX_TEXT_BYTES).decode('utf-8-sig')
        except UnicodeDecodeError as e:
            raise issues.StoreError('UTF-8 문서만 표시할 수 있어요.', 415) from e
        if contains_sensitive(content):
            raise issues.StoreError('문서에서 자격 정보로 보이는 값을 발견해 표시를 중단했어요. 원본을 확인해 주세요.', 422)
        return {**_metadata(s, key), 'path': path, 'content': content,
                'format': 'text' if path.lower().endswith('.txt') else 'markdown'}


def read_asset(key, path, commit):
    path = _path(path)
    mime = IMAGE_TYPES.get(PurePosixPath(path).suffix.lower())
    if not mime:
        raise issues.StoreError('문서 이미지는 PNG·JPEG·GIF·WebP만 지원해요.', 415)
    with _lock:
        s = _snapshot(key)
        if commit != s['commit']:
            raise issues.StoreError('문서 기준 커밋이 바뀌었어요. 새로고침해 주세요.', 409)
        return _blob(s, path, MAX_IMAGE_BYTES), mime
