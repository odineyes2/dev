"""마지막 실패 검사를 고정된 Task 커밋의 임시 사본에서 재현한다."""
import ast
import hashlib
import json
from pathlib import Path
import re
from threading import Thread
import uuid

import config
import db
import issues
import project_build
import regression

PROCESS = uuid.uuid4().hex
PREFIX = 'regression-reproduce:'
ROOT = config.DATA_DIR / 'check-reproductions'


def running(c):
    return c.execute("SELECT 1 FROM app_settings WHERE key LIKE ? AND json_extract(value,'$.status')='running' AND json_extract(value,'$.process')=?",
                     (PREFIX + '%', PROCESS)).fetchone() is not None


def result(ref):
    row = regression.load_record(PREFIX + ref)
    if row.get('status') == 'running' and row.get('process') != PROCESS:
        row = {**row, 'status': 'interrupted', 'note': '서버가 재시작되어 재현이 중단됐어요. 다시 실행해 주세요.'}
    return row


def panel(issue):
    ref = issue['ref']
    latest = result(ref)
    targets, sha, blocked = [], '', ''
    with db.connect() as c:
        completion = c.execute('SELECT note FROM execution_completion WHERE issue_id=? ORDER BY run_id DESC LIMIT 1', (issue['id'],)).fetchone()
        run = c.execute('SELECT id,status,note FROM runs WHERE issue_id=? ORDER BY id DESC LIMIT 1', (issue['id'],)).fetchone()
        project = c.execute('SELECT local_path FROM projects WHERE id=?', (issue['project_id'],)).fetchone()
        selections = c.execute('SELECT key,value FROM app_settings WHERE key IN (?,?) ORDER BY updated_at DESC,key DESC',
            tuple('regression-selection:' + ref + ':' + stage for stage in ('server_merge', 'server_completion'))).fetchall()
    stage, failed = '', []
    for selection in selections:
        record = json.loads(selection['value'])
        failed = [name for name, status in record.get('results', {}).items() if status == 'failed']
        if failed:
            stage = selection['key'].rsplit(':', 1)[-1]
            break
    if failed and project and project['local_path']:
        try:
            import execute
            repo = project['local_path']
            sha = project_build._git(repo, 'rev-parse', '--verify', execute.branch_name(ref) + '^{commit}')
            evidence = (run['note'] if run else '') + '\n' + (completion['note'] if completion else '')
            evidence += '\n' + next((e['body'] for e in reversed(issue.get('events', [])) if e['kind'] == 'status'), '')
            for name in failed:
                if not re.fullmatch(r'test_[A-Za-z0-9_]+\.py', name):
                    raise ValueError('검사 파일 이름이 유효하지 않아요.')
                paths = [p for p in ('tests/' + name, 'backend/tests/' + name)
                         if execute._run_git(repo, 'cat-file', '-e', sha + ':' + p).returncode == 0]
                if len(paths) != 1:
                    raise ValueError('실패한 검사 파일을 하나로 특정하지 못했어요.')
                args = []
                if name == 'test_ui.py':
                    text = evidence
                    for raw in re.findall(r'전체 로그:\s*([^\r\n]+)', evidence):
                        log = Path(raw.strip())
                        if log.parent.resolve() == (config.DATA_DIR / 'merge-tests').resolve() and log.name.startswith('test_ui-') and log.suffix == '.log' and log.is_file():
                            text += '\n' + log.read_text(encoding='utf-8', errors='replace')[-10000:]
                    functions = re.findall(r'test_ui\.py["\u0027], line \d+, in (check_\w+)', text)
                    source = project_build._git(repo, 'show', sha + ':' + paths[0])
                    options = {}
                    for node in ast.walk(ast.parse(source)):
                        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
                            continue
                        test = node.test
                        if not (isinstance(test.left, ast.Constant) and isinstance(test.left.value, str)
                                and re.fullmatch(r'--[a-z-]+-only', test.left.value)
                                and len(test.ops) == 1 and isinstance(test.ops[0], ast.In)
                                and ast.unparse(test.comparators[0]) == 'sys.argv'):
                            continue
                        calls = [statement.value.func.id for statement in node.body
                                 if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
                                 and isinstance(statement.value.func, ast.Name) and statement.value.func.id.startswith('check_')]
                        if len(calls) == 1:
                            options[calls[0]] = test.left.value
                    args = [options[functions[-1]]] if functions and functions[-1] in options else []
                    if not args:
                        raise ValueError('UI 실패 위치와 전용 실행 옵션을 찾지 못했어요. 전체 UI 검사는 실행하지 않아요.')
                targets.append({'file': paths[0], 'args': args})
        except (ValueError, SyntaxError, OSError, project_build.BuildError) as error:
            blocked = str(error)
    if not targets and not blocked:
        blocked = '재현할 실패 검사 기록이 없어요.'
    if latest.get('status') == 'running' or run and run['status'] == 'running' or issue.get('job') or issue['status'] in ('waiting', 'in_progress'):
        blocked = '현재 작업이나 검사 재현이 끝난 뒤 실행해 주세요.'
    if issue['status'] in ('done', 'closed'):
        blocked = '종결된 Task는 검사 재현을 시작할 수 없어요.'
    if not issue.get('parent_ref'):
        blocked = 'Task에서만 실패 검사를 재현할 수 있어요.'
    token = hashlib.sha256(json.dumps([sha, stage, targets, run['id'] if run else None], sort_keys=True).encode()).hexdigest()
    return {'available': bool(targets) and not blocked, 'blocked': blocked, 'targets': targets,
            'source_sha': sha, 'token': token, 'result': latest}


def start(actor, ref, token):
    if actor.get('kind') != 'human':
        raise issues.StoreError('사람(관리자)만 검사 재현을 실행할 수 있어요.', 403)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        state = panel(issues.get_issue(ref))
        if not state['available'] or token != state['token']:
            raise issues.StoreError(state['blocked'] or '실패 기록이나 Task 커밋이 바뀌었어요. 새로고침해 주세요.', 409)
        if running(c):
            raise issues.StoreError('다른 검사 재현이 진행 중이에요.', 409)
        import jobs
        busy = jobs.start_block(c, ref.split('-')[0], 'execute')
        if busy:
            raise issues.StoreError(busy, 409)
        repo = c.execute('SELECT local_path FROM projects WHERE key=?', (ref.split('-')[0],)).fetchone()[0]
        row = {'id': uuid.uuid4().hex, 'status': 'running', 'process': PROCESS, 'source_sha': state['source_sha'],
               'targets': state['targets'], 'results': [], 'started_at': db.now_iso(), 'note': ''}
        c.execute('INSERT INTO app_settings(key,value,updated_at,updated_by) VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at,updated_by=excluded.updated_by',
                  (PREFIX + ref, json.dumps(row), db.now_iso(), issues.actor_label(actor)))
    try:
        Thread(target=worker, args=(ref, repo, row), daemon=True).start()
    except RuntimeError:
        regression.save_record(PREFIX + ref, {**row, 'status': 'failed', 'note': '검사 스레드를 시작하지 못했어요.'})
        raise issues.StoreError('검사 스레드를 시작하지 못했어요.', 503) from None
    return row


def worker(ref, repo, row):
    import execute, orchestrate
    folder = ROOT / row['id']
    work = folder / 'worktree'
    added, lines = False, []
    try:
        folder.mkdir(parents=True)
        project_build._git(repo, 'worktree', 'add', '--detach', str(work.resolve()), row['source_sha'])
        added = True
        env = project_build.environment(execute.safe_env(repo))
        for target in row['targets']:
            path = work / target['file']
            output = orchestrate.run_test(str(work), path, target['args'], env)
            row['results'].append({**target, 'exit_code': output.returncode,
                                   'status': 'passed' if output.returncode == 0 else 'failed'})
            lines.append(target['file'] + ' ' + ' '.join(target['args']) + '\n' + (output.stdout or '') + '\n' + (output.stderr or ''))
        row['status'] = 'passed' if all(r['status'] == 'passed' for r in row['results']) else 'failed'
    except Exception as error:
        row.update(status='failed', note='검사 재현 실행 실패: ' + type(error).__name__)
        lines.append(str(error))
    finally:
        if added:
            try:
                project_build._git(repo, 'worktree', 'remove', '--force', str(work.resolve()))
            except project_build.BuildError:
                row['note'] += ' 임시 사본 정리가 끝나지 않았어요.'
        folder.mkdir(parents=True, exist_ok=True)
        text = '\n\n'.join(lines)
        import os
        for key, value in os.environ.items():
            if project_build._secret(key) and len(value) >= 8:
                text = text.replace(value, '[REDACTED]')
        (folder / 'log.txt').write_text(text, encoding='utf-8')
        row['ended_at'] = db.now_iso()
        regression.save_record(PREFIX + ref, row)


def log(ref):
    issues.get_issue(ref)
    row = result(ref)
    if not re.fullmatch(r'[a-f0-9]{32}', row.get('id', '')) or not (ROOT / row['id'] / 'log.txt').is_file():
        raise issues.StoreError('아직 재현 로그가 없어요.', 404)
    return (ROOT / row['id'] / 'log.txt').read_text(encoding='utf-8')
