"""브랜드 draft를 격리 커밋하고 기존 병합 경로로 적용·되돌리며 git 기록을 읽는다."""
import copy
from pathlib import Path, PurePosixPath
import re
import tempfile
import threading
from threading import Thread
import uuid
from urllib.parse import urlsplit

from fastapi import HTTPException
import brand_assets as assets
import brand_render as render
import config
import orchestrate

_states = {}
_state_lock = threading.RLock()
_ACTIVE = {'waiting', 'committing', 'merging'}


def status(key):
    with _state_lock:
        return copy.deepcopy(_states.get(key, dict(phase='idle', sha=None, error=None, log='', warnings=[])))


def _update(key, **values):
    with _state_lock:
        _states[key].update(values)


def _git(repo, *args):
    result = orchestrate._git(str(repo), *args)
    if result.returncode:
        raise ValueError((result.stderr or result.stdout).strip()[-2000:])
    return result.stdout.strip()


def _root(key):
    root = assets.project_root(key)
    if not root or not Path(root).is_dir():
        raise HTTPException(409, '로컬 저장소를 찾지 못했어요 — 프로젝트 경로를 확인해 주세요.')
    return Path(root)


def _path(root, path):
    parts = PurePosixPath(path).parts
    if (not parts or path.startswith('/') or '\\' in path or '\x00' in path or
            any(p.startswith('.') or ':' in p or p == 'data' for p in parts)):
        raise ValueError('브랜드 대상 경로가 잘못됐어요.')
    target = root / path
    if any(p.is_symlink() for p in (target, *target.parents)):
        raise ValueError('심볼릭 링크는 브랜드 대상으로 쓸 수 없어요.')
    target.resolve().relative_to(root.resolve())
    return target


def _validate(repo, paths, hashes=None):
    for path in paths:
        _path(repo, path)
    dirty = [path for path in paths if _git(repo, '--literal-pathspecs', 'status', '--porcelain', '--', path)]
    if dirty:
        raise HTTPException(409, dict(detail='대상 파일에 커밋 안 된 변경이 있어요 — 먼저 정리해 주세요.', files=dirty))
    if hashes is not None:
        for path in paths:
            target = _path(repo, path)
            current = _git(repo, 'hash-object', '--', path) if target.is_file() else None
            if current != hashes.get(path):
                raise HTTPException(409, '그 사이 저장소가 바뀌었어요 — 다시 만들어 주세요.')


def history(key, repo=None):
    repo = repo or assets.project_root(key)
    if not repo:
        return []
    result = orchestrate._git(str(repo), 'log', orchestrate.BASE_BRANCH, '--first-parent', '--grep=^brand:',
                              '--format=%H%x00%aI%x00%an%x00%B%x00%x1e')
    if result.returncode:
        return []
    entries, reverted = [], {}
    for record in result.stdout.split('\x1e'):
        fields = record.strip().split('\x00')
        if len(fields) < 4:
            continue
        sha, at, author, body = fields[:4]
        slots = re.search(r'^Brand-Slots: (.*)$', body, re.M)
        undo = re.search(r'^Brand-Revert: ([0-9a-f]{40,64})$', body, re.M)
        if undo:
            reverted.setdefault(undo[1], sha)
        files = _git(repo, 'diff-tree', '--no-commit-id', '--name-only', '-r', sha).splitlines()
        entries.append(dict(sha=sha, at=at, author=author,
                            slots=slots[1].split(',') if slots and slots[1] else [], files=files, reverted_by=None))
    for entry in entries:
        entry['reverted_by'] = reverted.get(entry['sha'])
    return entries


def request(key, actor, *, draft_id=None, sha=None):
    if actor.get('kind') != 'human':
        raise HTTPException(403, '사람(관리자)만 할 수 있어요.')
    repo = _root(key)
    # 재생성 중인 draft와 섞이지 않게 적용 바이트를 메모리에 고정한다.
    with _state_lock:
        if status(key)['phase'] in _ACTIVE:
            raise HTTPException(409, '이미 브랜드를 적용 중이에요 — 완료될 때까지 기다려 주세요.')
        if sha is not None:
            entry = next((e for e in history(key, repo) if e['sha'] == sha), None)
            if entry is None or entry['reverted_by']:
                raise HTTPException(409, '되돌릴 브랜드 기록을 확인해 주세요.')
            paths, slots = entry['files'], entry['slots']
            payload, hashes = None, None
        else:
            try:
                with render._LOCK:
                    folder, metadata = render.read_draft(key, draft_id)
                    paths = [c['path'] for c in metadata['changes']]
                    if not paths:
                        raise HTTPException(409, '바뀐 파일이 없어요 — 다시 만들어 주세요.')
                    payload = {p: _path(folder/'files', p).read_bytes() for p in paths}
                    hashes, slots = metadata['blob_hashes'], metadata['slots']
            except (OSError, ValueError, KeyError) as error:
                raise HTTPException(409, str(error)) from error
        try:
            _validate(repo, paths, hashes)
        except (OSError, ValueError) as error:
            raise HTTPException(409, str(error)) from error
        _states[key] = dict(phase='waiting', sha=None, error=None,
                            log='다른 작업 병합이 끝나면 적용돼요', warnings=[])
        snapshot = status(key)
        worker = Thread(target=_worker, args=(key, repo, dict(actor), paths, slots, payload, hashes, sha), daemon=True)
        try:
            worker.start()
        except Exception:
            _update(key, phase='failed', error='적용 작업을 시작하지 못했어요.')
            raise
        return snapshot


def _worker(key, repo, actor, paths, slots, payload, hashes, revert_sha):
    worktree = None
    branch = None
    acquired = False
    terminal = {}
    try:
        acquired = orchestrate._lock.acquire(blocking=False)
        if not acquired:
            _update(key, phase='waiting', log='다른 작업 병합이 끝나면 적용돼요')
            orchestrate._lock.acquire()
            acquired = True
        _validate(repo, paths, hashes)
        if _git(repo, 'rev-parse', '--abbrev-ref', 'HEAD') != orchestrate.BASE_BRANCH:
            raise ValueError('기준 브랜치가 아니에요 — 기준 브랜치로 돌아온 뒤 다시 적용해 주세요.')
        if _git(repo, 'status', '--porcelain', '--untracked-files=no'):
            raise ValueError('작업 폴더에 커밋 안 된 변경이 있어요 — 정리 후 다시 적용해 주세요.')
        before = _git(repo, 'rev-parse', 'HEAD')
        _update(key, phase='committing', log='브랜드 커밋을 만들고 있어요')
        parent = config.DATA_DIR/'brand-worktrees'
        parent.mkdir(parents=True, exist_ok=True)
        worktree = Path(tempfile.mkdtemp(prefix='brand-', dir=parent)).resolve()
        _git(repo, 'worktree', 'add', '--detach', str(worktree), before)
        name = str(actor.get('name') or 'admin')
        name = re.sub(r'[<>\r\n\x00]', '', name).strip() or 'admin'
        email = re.sub(r'[^\w.-]', '_', name) + '@dev.local'
        identity = ('-c', f'user.name={name}', '-c', f'user.email={email}')
        if revert_sha:
            result = orchestrate._git(str(worktree), 'revert', '--no-commit', revert_sha)
            if result.returncode:
                raise ValueError('이후 적용과 겹쳐 되돌릴 수 없어요 — 최근 기록부터 되돌려 주세요.')
            title = f'brand: 되돌리기 {revert_sha[:7]} (dev 브랜드 탭)'
        else:
            for path, data in payload.items():
                target = _path(worktree, path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            _git(worktree, '--literal-pathspecs', 'add', '--', *paths)
            title = 'brand: 앱 아이콘·파비콘·시작 화면 교체 (dev 브랜드 탭)'
        message = title + '\n\n' + '\n'.join(paths) + '\n\nBrand-Slots: ' + ','.join(slots)
        if revert_sha:
            message += '\nBrand-Revert: ' + revert_sha
        _git(worktree, *identity, 'commit', '--author', f'{name} <{email}>', '-m', message)
        commit = _git(worktree, 'rev-parse', 'HEAD')
        candidate = 'relay/brand-' + uuid.uuid4().hex
        _git(repo, 'branch', candidate, commit)
        branch = candidate
        _update(key, phase='merging', log='검사한 브랜드 커밋을 반영하고 있어요')
        _validate(repo, paths, hashes)
        cfg = orchestrate.settings(key)
        phase, note = orchestrate._merge(str(repo), f'{key}-brand', [], branch=branch, fast_forward=True, cfg=cfg)
        if phase != 'merged':
            raise ValueError(note)
        _update(key, sha=note)
        phase, log = orchestrate.deploy(str(repo), cfg, note)
        if phase != 'merged':
            raise ValueError(log)
        warnings = []
        if not revert_sha:
            try:
                warnings = _check_urls(repo, key, [p for p in paths if hashes.get(p) is None], cfg)
            except (OSError, ValueError, KeyError) as error:
                warnings = ['반영은 됐지만 새 URL을 확인하지 못했어요: ' + str(error)]
        terminal = dict(phase='done', log=log, warnings=warnings)
    except Exception as error:
        detail = error.detail if isinstance(error, HTTPException) else str(error)
        terminal = dict(phase='failed', error=detail, log=str(detail))
    finally:
        cleanup = []
        if worktree:
            result = orchestrate._git(str(repo), 'worktree', 'remove', '--force', str(worktree))
            if result.returncode and worktree.exists():
                # add 전에 실패해 남은 빈 디렉터리만 직접 지운다.
                try:
                    worktree.rmdir()
                except OSError:
                    cleanup.append('임시 worktree 정리에 실패했어요: ' + result.stderr.strip())
        if branch:
            result = orchestrate._git(str(repo), 'branch', '-D', branch)
            if result.returncode:
                cleanup.append('임시 브랜치 정리에 실패했어요: ' + result.stderr.strip())
        if cleanup:
            terminal = dict(phase='failed', error='\n'.join(cleanup))
        _update(key, **terminal)
        if acquired:
            orchestrate._lock.release()


def _check_urls(repo, key, paths, cfg):
    health = urlsplit(cfg.get('health_url') or '')
    if not health.netloc:
        return []
    origin = f'{health.scheme}://{health.netloc}'
    info = assets.inspect_repo(repo, key)
    tracked = {f['path'] for s in info['slots'] for f in s['files']}
    urls = set()
    for html in info['html']:
        for tag in assets.Tags((repo/html).read_text('utf-8')).tags:
            raw = tag['attrs'].get('href') or tag['attrs'].get('src') or tag['attrs'].get('content') or ''
            parsed = urlsplit(raw)
            if parsed.path.startswith('/') and assets.resolve_url(repo, tracked, parsed.path, html) in paths:
                urls.add(parsed.path)
    if info['manifest']:
        import json
        manifest = info['manifest']['path']
        for icon in json.loads((repo/manifest).read_text('utf-8')).get('icons', []):
            raw = icon.get('src', '')
            if assets.resolve_url(repo, tracked, raw, manifest) in paths:
                from urllib.parse import urljoin
                manifest_url = next((t['attrs']['href'] for t in assets.Tags((repo/info['html'][0]).read_text('utf-8')).tags
                                     if t['attrs'].get('rel') == 'manifest'), '/')
                urls.add(urlsplit(urljoin(manifest_url, raw)).path)
    warnings = []
    for url in sorted(urls):
        code, _ = orchestrate._get(origin + url)
        if code != 200:
            warnings.append(f'{url} 응답이 {code}예요 — 프로젝트의 정적 파일 서빙 설정을 확인해 주세요.')
    return warnings
