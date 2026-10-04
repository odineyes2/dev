"""거절한 이슈의 병합을 검증 후 되돌리고, 재시작까지 확인한 뒤 닫는다."""
import json
import fnmatch
import re
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path

import config
import db
import issues
import orchestrate


class RollbackError(issues.StoreError):
    pass


def _directory():
    return config.DATA_DIR / 'rollbacks'


def _save(record):
    folder = _directory()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (record['id'] + '.json')
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def _records():
    for path in _directory().glob('*.json'):
        yield json.loads(path.read_text(encoding='utf-8'))


def active_merges(repo, refs):
    """first-parent 이력의 revert 및 revert 취소까지 계산해 아직 적용된 병합만 찾는다."""
    result = orchestrate._git(repo, 'log', '--first-parent', '--reverse', '--format=%H%x00%B%x1e', 'HEAD')
    if result.returncode:
        raise RollbackError('롤백할 저장소의 Git 이력을 읽을 수 없어요.', 409)
    effects, balances, selected = {}, {}, []
    for entry in result.stdout.split('\x1e'):
        if '\x00' not in entry:
            continue
        sha, body = entry.strip().split('\x00', 1)
        reverted = re.search(r'This reverts commit ([0-9a-f]{40})', body)
        effect = {key: -value for key, value in effects.get(reverted[1], {}).items()} if reverted else {sha: 1}
        effects[sha] = effect
        for key, value in effect.items():
            balances[key] = balances.get(key, 0) + value
        match = re.match(r'Merge relay/([^\s(]+)(?:\s|\(|$)', body)
        if match and match[1] in refs:
            selected.append((sha, match[1]))
    return [(sha, ref) for sha, ref in reversed(selected) if balances.get(sha, 0) > 0]


@contextmanager
def terminal_change(actor, ref, rejecting):
    """병합과 거절의 잠금 순서를 맞추고 DB 롤백 후에도 실패 사유를 남긴다."""
    with orchestrate._lock if rejecting else nullcontext():
        try:
            yield
        except RollbackError as error:
            issues.add_comment(actor, ref, '↩ 롤백 실패 — 이슈를 닫지 않았어요. ' + str(error))
            raise
    if rejecting:
        start_recovery()


def prepare(c, actor, root, rows, note):
    """운영 사본은 검사 성공 전까지 건드리지 않는다. 반환값이 있으면 배포 확인이 남았다."""
    for record in _records():
        if record['ref'] == root['ref'] and record['phase'] not in ('complete', 'failed'):
            return record
    refs = {row['ref'] for row in rows}
    project = c.execute('SELECT local_path FROM projects WHERE id=?', (root['project_id'],)).fetchone()
    repo = project['local_path']
    if not repo or not Path(repo).is_dir() or orchestrate._git(repo, 'rev-parse', '--git-dir').returncode:
        marks = ','.join('?' * len(rows))
        applied = c.execute(f"SELECT 1 FROM events WHERE issue_id IN ({marks}) AND kind='comment' AND body LIKE '🔀 %병합%' LIMIT 1", [row['id'] for row in rows]).fetchone()
        if applied:
            raise RollbackError('반영 기록이 있지만 프로젝트 저장소에 접근할 수 없어요.', 409)
        return None
    merges = active_merges(repo, refs)
    if not merges:
        return None
    if orchestrate._git(repo, 'rev-parse', '--abbrev-ref', 'HEAD').stdout.strip() != orchestrate.BASE_BRANCH:
        raise RollbackError('기준 브랜치가 아니라 롤백하지 않았어요.', 409)
    if orchestrate._git(repo, 'status', '--porcelain', '--untracked-files=no').stdout.strip():
        raise RollbackError('커밋하지 않은 변경이 있어 롤백하지 않았어요.', 409)
    if c.execute("SELECT 1 FROM runs WHERE status='running' LIMIT 1").fetchone():
        raise RollbackError('Agent 작업이 실행 중이에요 — 끝난 뒤 다시 거절해 주세요.', 409)
    before = orchestrate._git(repo, 'rev-parse', 'HEAD').stdout.strip()
    record = {'id': str(time.time_ns()), 'ref': root['ref'], 'repo': repo, 'actor': actor,
              'note': note, 'project_key': root['project_key'], 'before': before,
              'rows': [{'id': row['id'], 'ref': row['ref'], 'status': row['status'],
                        'status_event': c.execute("SELECT COALESCE(MAX(id), 0) FROM events WHERE issue_id=? AND kind='status'", (row['id'],)).fetchone()[0]} for row in rows],
              'merges': merges, 'phase': 'preparing'}
    _save(record)
    folder = config.DATA_DIR / 'rollback-worktrees'
    folder.mkdir(parents=True, exist_ok=True)
    worktree = Path(tempfile.mkdtemp(prefix='rollback-', dir=folder)).resolve()
    added = False
    try:
        result = orchestrate._git(repo, 'worktree', 'add', '--detach', str(worktree), before)
        if result.returncode:
            raise RollbackError('롤백 검사 사본을 만들지 못했어요: ' + result.stderr.strip(), 409)
        added = True
        for sha, ref in merges:
            result = orchestrate._git(str(worktree), 'revert', '-m', '1', '--no-edit', sha)
            if result.returncode:
                raise RollbackError(f'{ref} 롤백이 후속 변경과 충돌했어요 — 운영 코드는 유지했어요.\n' + (result.stdout + result.stderr)[-1500:], 409)
        failed = orchestrate.run_tests(str(worktree))
        if failed:
            raise RollbackError('롤백 뒤 검사가 실패했어요 — 운영 코드는 유지했어요.\n' + failed, 409)
        record['head'] = orchestrate._git(str(worktree), 'rev-parse', 'HEAD').stdout.strip()
        changed = orchestrate._git(str(worktree), 'diff', '--name-only', before, record['head']).stdout.split()
        cfg = {**orchestrate.DEFAULTS, **orchestrate.settings(root['project_key'])}
        record['restart'] = bool(cfg.get('pm2_app') and any(any(fnmatch.fnmatch(f, pattern) for pattern in cfg['restart_when']) for f in changed))
        record['phase'] = 'prepared'
        _save(record)  # Git 반영 전 의도를 보존해 서버 종료 뒤 이어서 처리한다.
        if orchestrate._git(repo, 'rev-parse', 'HEAD').stdout.strip() != before or orchestrate._git(repo, 'status', '--porcelain', '--untracked-files=no').stdout.strip():
            raise RollbackError('검사 중 운영 저장소가 바뀌었어요 — 롤백을 반영하지 않았어요.', 409)
        result = orchestrate._git(repo, 'merge', '--ff-only', record['head'])
        if result.returncode:
            raise RollbackError('롤백 반영에 실패했어요: ' + result.stderr.strip(), 409)
        record['phase'] = 'applied'
        _save(record)
    except (OSError, ValueError, subprocess.TimeoutExpired, RollbackError) as error:
        applied = record.get('head') and not orchestrate._git(repo, 'merge-base', '--is-ancestor', record['head'], 'HEAD').returncode
        record['phase'] = 'applied' if applied else 'failed'
        record['error'] = str(error)
        _save(record)
        if isinstance(error, RollbackError):
            raise
        raise RollbackError('롤백 검사에 실패했어요: ' + str(error), 409) from error
    finally:
        assert worktree.is_relative_to(folder.resolve())
        if added:
            orchestrate._git(repo, 'worktree', 'remove', '--force', str(worktree))
        elif worktree.exists():
            worktree.rmdir()
    for row in rows:
        issues._event(c, row['id'], actor, 'comment', f"↩ 코드를 되돌렸어요(`{record['head'][:7]}`) — 롤백 뒤 검사 통과." + (' 재시작 확인 전까지 닫지 않아요.' if record['restart'] else ''), {'rollback_id': record['id'], 'merges': merges})
    return record


def _recovery_rows(c, record):
    rows = [issues._find(c, row['ref']) for row in record['rows']]
    for row, snapshot in zip(rows, record['rows']):
        last = c.execute("SELECT id, data_json FROM events WHERE issue_id=? AND kind='status' ORDER BY id DESC LIMIT 1", (row['id'],)).fetchone()
        owned = bool(last and json.loads(last['data_json']).get('rollback_id') == record['id'])
        unchanged = row['status'] == snapshot['status'] and (last['id'] if last else 0) == snapshot['status_event']
        if not unchanged and not owned:
            raise RollbackError('롤백 대기 중 사람이 상태를 바꿨어요 — 종결하지 않았어요.', 409)
        if c.execute("SELECT 1 FROM runs WHERE issue_id=? AND status='running'", (row['id'],)).fetchone():
            raise RollbackError('실행 중인 이슈는 닫지 않아요.', 409)
    return rows


def recover():
    """저장된 롤백의 코드 반영·재시작을 확인하고 DB 종결을 이어간다."""
    waiting = False
    with orchestrate._lock:
        for record in list(_records()):
            if record['phase'] in ('complete', 'failed', 'preparing'):
                continue
            repo = record['repo']
            if orchestrate._git(repo, 'merge-base', '--is-ancestor', record['head'], 'HEAD').returncode:
                continue  # 반영되지 않은 prepared 기록은 자동으로 운영에 적용하지 않는다.
            try:
                with db.connect() as c:
                    _recovery_rows(c, record)
                if record['restart']:
                    cfg = {**orchestrate.DEFAULTS, **orchestrate.settings(record['project_key'])}
                    with db.connect() as c:
                        if c.execute("SELECT 1 FROM runs WHERE status='running' LIMIT 1").fetchone():
                            waiting = True
                            continue
                    if orchestrate._busy(cfg):
                        waiting = True
                        continue
                    if record['phase'] != 'restarting':
                        record['phase'] = 'restarting'
                        _save(record)
                        error = orchestrate._restart(repo, cfg)
                        if error:
                            raise RollbackError('운영 재시작 실패: ' + error, 409)
                    if not orchestrate._healthy(cfg):
                        raise RollbackError('재시작 뒤 정상 응답을 확인하지 못했어요.', 409)
                with db.connect() as c:
                    c.execute('BEGIN IMMEDIATE')
                    rows = _recovery_rows(c, record)
                    for row in rows:
                        c.execute("UPDATE jobs SET status='cancelled' WHERE issue_id=? AND status='queued'", (row['id'],))
                        if row['status'] != 'closed':
                            issues._set_status(c, record['actor'], row, 'closed', record['note'], {'rollback_id': record['id'], 'rollback_sha': record['head']})
                    for row in rows:
                        issues._event(c, row['id'], record['actor'], 'comment', '↩ 롤백과 운영 반영 확인을 완료했어요.', {'rollback_id': record['id'], 'rollback_sha': record['head']})
                record['phase'] = 'complete'
                _save(record)
            except (issues.StoreError, OSError, ValueError, subprocess.TimeoutExpired) as error:
                record['phase'] = 'restart_failed' if record['restart'] else 'failed'
                record['error'] = str(error)
                _save(record)
                issues.add_comment(record['actor'], record['ref'], '↩ 롤백 후속 확인 실패 — 닫지 않았어요. ' + str(error))
    if waiting:
        start_recovery(delay=30)


def start_recovery(startup=False, delay=1):
    # 응답·DB 커밋 이후에 재시작하며, 작업이 중단돼도 lifespan에서 저장 기록을 다시 읽는다.
    if not _directory().exists():
        return
    if startup:
        # DB 커밋 전에 종료된 경우에도 거절 대상의 대기 작업을 먼저 멈춘다.
        with db.connect() as c:
            for record in _records():
                if record['phase'] in ('complete', 'failed', 'preparing') or not record.get('head'):
                    continue
                if orchestrate._git(record['repo'], 'merge-base', '--is-ancestor', record['head'], 'HEAD').returncode:
                    continue
                for row in record['rows']:
                    c.execute("UPDATE jobs SET status='cancelled' WHERE issue_id=? AND status='queued'", (row['id'],))
    timer = threading.Timer(delay, recover)
    timer.daemon = True
    timer.start()
