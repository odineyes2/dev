"""서버 검사 단계·개별 파일과 에이전트 검사 지시를 공통 설정으로 관리한다."""
import ast
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import checks as registry
import db
import issues

# 등록부(checks.REGISTRY)에서 Settings로 바꿀 수 있는 항목만 — 키는 저장된 설정·기록과 호환된다.
CHECKS = tuple((r['key'], '서버' if r['stage'].startswith('서버') else '에이전트', r['name'], r['description'])
               for r in registry.REGISTRY if r['mode'] == 'settable')
DEV_KEY = 'DEV'


def _file_doc(path):
    if path.suffix == '.js':
        text = path.read_text(encoding='utf-8', errors='replace')
        m = re.search(r'^\s*(?://|/\*+)\s*(.+?)\s*(?:\*/)?\s*$', text, re.M)
        return m[1] if m else '개별 회귀 검사'
    try:
        return (ast.get_docstring(ast.parse(path.read_text(encoding='utf-8'))) or '개별 회귀 검사').splitlines()[0]
    except (OSError, SyntaxError, UnicodeError):
        return '개별 회귀 검사'


def catalog(project=None):
    """project가 없으면 dev 자신의 검사 파일, 있으면 그 프로젝트 저장소의 검사 파일을 담는다."""
    nums = {r['key']: r['number'] for r in registry.REGISTRY}
    rows = [dict(key=k, group=g, name=n, description=d, number=nums[k]) for k, g, n, d in CHECKS]
    repo = Path(__file__).resolve().parents[1]
    if project and project != DEV_KEY:
        repo = next((Path(p['local_path']) for p in issues.list_projects() if p['key'] == project and p['local_path']), None)
    if not project:   # 문맥 없는 목록은 dev 루트 tests/만(기존 동작)
        files = sorted(p for pattern in ('test_*.py', 'test_*.js') for p in repo.joinpath('tests').glob(pattern))
    else:
        files = test_files(repo, project) if repo and repo.is_dir() else []
    rels = {p: 'tests/' + p.name if not project else p.relative_to(repo).as_posix() for p in files}
    labels = registry.file_numbers(project or DEV_KEY, rels.values()) if repo else {}
    for path in files:
        rel = rels[path]
        rows.append(dict(key='file:' + path.name, group='테스트 파일', name=path.name, description=_file_doc(path),
                         number=labels.get(rel, ''), procedure=registry.FILE_NUMBER, path=rel, kind=registry.kind(path)))
    return rows


def file_mode(saved, project, name):
    """검사 파일의 사람 설정 — 프로젝트별 키 `file:<프로젝트>:<이름>`. 옛 이름 단위 키는 dev 것으로만 읽는다."""
    key = f'file:{project}:{name}'
    if key in saved:
        return saved[key]
    return saved.get('file:' + name, 'auto') if project == DEV_KEY else 'auto'


def project_key(repo=None, issue=None):
    if issue:
        return issue.get('project_key') or DEV_KEY
    if _is_dev(repo):
        return DEV_KEY
    try:
        projects = issues.list_projects()
    except Exception:   # DB 초기화 전(프롬프트 생성·독립 Git 검사)에는 프로젝트를 알 수 없다
        return '?'
    return next((p['key'] for p in projects if p['local_path'] and Path(p['local_path']).resolve() == Path(repo).resolve()), '?')


def values():
    with db.connect() as c:
        # 프롬프트 생성·독립 Git 검사는 DB 초기화 전에도 기본 설정으로 동작한다.
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_settings'").fetchone():
            return {}
        return {r['key'][11:]: {'true': 'on', 'false': 'off'}.get(r['value'], r['value']) for r in c.execute(
            "SELECT key,value FROM app_settings WHERE key LIKE 'regression:%'")}


def enabled(key, ref=None, repo=None):
    return resolve(ref, repo)['checks'].get(key, {'enabled': True})['enabled']


def _row_mode(saved, pk, row):
    if row['key'].startswith('file:'):
        return file_mode(saved, pk, row['name'])
    return saved.get(row['key'], 'auto')


def settings(project=None):
    """project: 파일 목록·파일별 설정의 프로젝트 키(없으면 dev). registry는 번호 순 전체 절차(고정·관리형 포함)."""
    saved = values()
    pk = project or DEV_KEY
    rows = [{**row, 'mode': _row_mode(saved, pk, row)} for row in catalog(project)]
    reg = [{**{k: r[k] for k in ('number', 'key', 'name', 'stage', 'description')}, 'kind': r['mode'],
            'mode': saved.get(r['key'], 'auto') if r['mode'] == 'settable' else r['mode']} for r in registry.REGISTRY]
    return {'project': pk, 'checks': rows, 'registry': reg}


def update(actor, changes, project=None):
    if actor['kind'] != 'human':
        raise issues.StoreError('검사 설정은 사람만 바꿀 수 있어요.', 403)
    pk = project or DEV_KEY
    keys = {row['key'] for row in catalog(project)}   # 고정·관리형 절차는 여기 없어 바꿀 수 없다
    if not isinstance(changes, dict) or not changes or any(k not in keys or v not in ('on', 'off', 'auto') for k, v in changes.items()):
        raise issues.StoreError('검사 종류와 On·Off·Auto 값을 확인해 주세요. (고정 절차는 끌 수 없어요)', 400)
    with db.connect() as c:
        for key, value in changes.items():
            stored = f'file:{pk}:{key[5:]}' if key.startswith('file:') else key
            c.execute('INSERT INTO app_settings(key,value,updated_at,updated_by) VALUES(?,?,?,?) '
                      'ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at,updated_by=excluded.updated_by',
                      ('regression:' + stored, value, db.now_iso(), issues.actor_label(actor)))
    return settings(project)


def parse_plan(body):
    """승인 계획 본문의 선택 JSON을 검사한다. 없는 기존 계획은 빈 선택이다."""
    section = re.search(r'^## 검사 선택\s*\n```json\s*\n(.*?)\n```', body or '', re.M | re.S)
    if not section:
        if re.search(r'^## 검사 선택\s*$', body or '', re.M):
            raise issues.StoreError('검사 선택 절에는 json 코드 블록을 넣어 주세요.')
        return {}
    try:
        choices = json.loads(section[1])
        keys = {row['key'] for row in catalog()}
        if not isinstance(choices, dict):
            raise ValueError()
        for n, choice in choices.items():
            if not str(n).isdigit() or not isinstance(choice, dict) or choice.get('level') not in ('light', 'normal', 'high'):
                raise ValueError()
            if not isinstance(choice.get('reason'), str) or not choice['reason'].strip() or not isinstance(choice.get('checks'), dict):
                raise ValueError()
            if any((k not in keys and not registry.valid_file_key(k)) or type(v) is not bool for k, v in choice['checks'].items()):
                raise ValueError()
        return choices
    except (ValueError, TypeError):
        raise issues.StoreError('검사 선택은 Task 번호별 level(light/normal/high), reason, checks(검사 키: true/false) 형식이에요.')


def _is_dev(repo=None, issue=None):
    """dev 저장소(또는 그 Task worktree)인지 — catalog의 개별 파일 키는 dev 자신의 tests/다(DEV-147)."""
    if issue:
        return issue.get('project_key') == 'DEV'
    return not repo or Path(repo, 'server', 'regression.py').exists()


def _project_repo(ref):
    with db.connect() as c:   # 프롬프트 생성은 DB 초기화 전에도 기본 안내로 동작한다
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='issues'").fetchone():
            return None, None
    try:
        issue = issues.get_issue(ref)
    except issues.StoreError:
        return None, None
    return issue, next((p['local_path'] for p in issues.list_projects() if p['key'] == issue['project_key']), '')


def planning_instructions(ref=None):
    """계획 에이전트용 검사 선택 안내. 개별 파일 키는 그 이슈 프로젝트의 검사 파일만 보여 준다(DEV-147 — Moai 계획에 dev 검사 목록이 섞였다)."""
    issue, repo = _project_repo(ref) if ref else (None, None)
    rows = [r for r in settings()['checks'] if not r['key'].startswith('file:')]
    if issue and not _is_dev(repo, issue):
        files = ', '.join(f"file:{p.name}={Path(p).relative_to(repo).as_posix()}" for p in test_files(repo, issue['project_key'])) if repo and Path(repo).is_dir() else ''
        keys = ', '.join(r['key'] + '=' + r['mode'] for r in rows) + ('. 이 프로젝트의 검사 파일(키=경로): ' + files if files else '. 이 프로젝트에는 찾은 검사 파일이 없다')
    else:
        keys = ', '.join(row['key'] + '=' + row['mode'] for row in settings()['checks'])
    return ('\n계획서에 `## 검사 선택` 절과 json 코드 블록을 추가한다. Task 번호별로 검사 수준과 선택 이유, '
            'Auto 항목의 필요 여부를 적는다. 형식: '
            '{"1":{"level":"light","reason":"문구만 변경","checks":{"agent_capture":true,"agent_api":false}}}. '
            '수준은 light(문구/화면/문서), normal(개별 기능), high(인증/DB/공통 실행)이다. '
            '각 Task의 Auto 항목을 모두 판단하고 개별 파일도 file:test_x.py 키로 선택한다. '
            'Task가 추가·수정하는 검사 파일은 서버가 항상 돌리므로 false로 두어도 꺼지지 않는다. '
            'On/Off는 사람 설정이며 바꾸지 않는다. 검사 키와 현재 모드: ' + keys + '\n')


def _plan_task(issue, body):
    tasks = issues.parse_tasks(body)
    task = next((t for t in tasks if t['title'] == issue['title']), None)
    if task:
        return task
    ref_number = re.fullmatch(r'[^-]+-\d+-(\d+)', issue.get('ref', '')) if issue.get('parent_ref') else None
    numbers = [issue.get('sub_number') or (int(ref_number[1]) if ref_number else None)]
    match = re.search(r'\bTask\s+(\d+)\b', issue.get('body', ''), re.I)
    if match:
        numbers.append(int(match[1]))
    return next((t for n in numbers if n is not None for t in tasks if t['n'] == int(n)), None)


def _planned_paths(issue, task, body):
    import execute
    paths = set(execute.task_files(issue))
    if task and task['files']:
        paths.update(execute.task_files({'body': '파일: ' + task['files']}))
    if paths:
        return paths, False
    # 옛 계획에는 Tasks 절이 없어도 본문에 구체적인 경로가 남아 있다.
    paths = set(re.findall(r'`((?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]+)`', body))
    return paths, bool(paths)


def capture_skip_reason(ref, repo=None):
    """사람 설정·계획 선택·Auto 판단의 캡처 미실시 사유를 공통 문구로 만든다."""
    check = resolve(ref, repo)['checks']['agent_capture']
    if check['enabled']:
        return ''
    if check['mode'] == 'off':
        return '사람 설정(Settings → 회귀검사 → UI 캡처 확인: Off)으로 캡처하지 않는다.'
    if check['reason'].startswith('계획 에이전트:'):
        return '계획서 검사 선택: ' + check['reason'].removeprefix('계획 에이전트: ')
    return ('Auto: 화면 변경이 없다고 판단해 캡처하지 않는다(이유: ' + check['reason'] + '). '
            '화면을 바꿨다면 Settings에서 On으로 바꾸거나 계획서 검사 선택에 agent_capture:true를 적는다.')


def capture_planned_off(ref, repo):
    """실행 전 계획 경로만으로 판단했을 때 캡처 Auto가 꺼졌는지 확인한다."""
    issue = issues.get_issue(ref)
    paths = set()
    if issue.get('parent_ref'):
        parent = issues.get_issue(issue['parent_ref'])
        approval = parent.get('approval') or {}
        if approval.get('verdict') in ('approve', 'approve_notes') and not approval.get('stale'):
            body = (parent.get('plan') or {}).get('body', '')
            paths, _ = _planned_paths(issue, _plan_task(issue, body), body)
    if not paths:
        import execute
        paths = execute.task_files(issue)
    check = resolve(ref, repo, changed=list(paths))['checks']['agent_capture']
    return check['mode'] == 'auto' and not check['enabled']


def resolve(ref=None, repo=None, changed=None):
    """사람 설정 > 실제 고위험 변경 > 승인 계획 > 변경 범위 기본값 순으로 결정한다."""
    import execute
    saved = values()
    issue, choice, planned_paths, inferred_plan = None, {}, set(), False
    if ref:
        with db.connect() as c:
            ready = c.execute("SELECT 1 FROM sqlite_master WHERE name='issues'").fetchone()
        if ready:
            try:
                issue = issues.get_issue(ref)
            except issues.StoreError as error:
                if error.status != 404:
                    raise
        if issue is None:
            ref = None
    if issue:
        if not repo:
            repo = next((p['local_path'] for p in issues.list_projects() if p['key'] == issue['project_key']), '')
        if issue.get('parent_ref'):
            parent = issues.get_issue(issue['parent_ref'])
            a = parent.get('approval') or {}
            if a and not a.get('stale') and a.get('verdict') in ('approve', 'approve_notes'):
                body = (parent.get('plan') or {}).get('body', '')
                task = _plan_task(issue, body)
                planned_paths, inferred_plan = _planned_paths(issue, task, body)
                choices = parse_plan(body)
                choice = choices.get(str(task['n']), {}) if task else {}
    if changed is None:
        changed = []
        if repo and Path(repo).is_dir():
            result = execute._run_git(str(repo), 'diff', '--name-only', f'{execute.BASE_BRANCH}...HEAD')
            if result.returncode == 0:
                changed = result.stdout.splitlines()
            dirty = execute._run_git(str(repo), 'diff', '--name-only', 'HEAD')
            if dirty.returncode == 0:
                changed += dirty.stdout.splitlines()
            added = execute._run_git(str(repo), 'ls-files', '--others', '--exclude-standard')
            if added.returncode == 0:
                changed += [p for p in added.stdout.splitlines() if p.endswith(('.py', '.js', '.ts', '.tsx', '.jsx', '.vue', '.css', '.html')) and not p.startswith(('.ui-captures/', 'tests/shots/'))]
        if not changed and issue:
            changed = list(planned_paths or execute.task_files(issue))
        else:
            inferred_plan = False
    else:
        inferred_plan = False
    changed = sorted(set(p.replace('\\', '/') for p in changed))
    named_tests = set(re.findall(r'tests/(test_[A-Za-z0-9_]+\.py)', (issue or {}).get('body', '')))
    critical = {'auth', 'db', 'execute', 'review', 'jobs', 'orchestrate', 'rollback', 'regression'}
    modules = {Path(p).stem for p in changed if p.endswith('.py') and not p.startswith('tests/')}
    high = bool(modules & critical) or any(re.search(r'(^|/)(auth|migrations|security)(/|\.)', p) for p in changed)
    ui = any(p.startswith(('static/', 'src/components/', 'src/pages/')) or p.endswith(('.css', '.html', '.tsx', '.jsx', '.vue')) for p in changed)
    code = any(p.endswith(('.py', '.js', '.ts', '.tsx', '.jsx', '.vue')) for p in changed)
    level = 'high' if high else choice.get('level') or ('normal' if not changed or code else 'light')
    # Task가 추가·수정한 검사 파일(tests/·backend/tests/ 등) — 계획 선택으로 끌 수 없다(DEV-147)
    changed_tests = {Path(p).name for p in changed if re.search(r'(^|/)tests/test_[A-Za-z0-9_]+\.(py|js)$', p)}
    dev = _is_dev(repo, issue)
    pk = project_key(repo, issue)
    checks = {}
    for row in catalog():
        key = row['key']
        if key.startswith('file:') and not dev:
            continue   # dev 자신의 검사 이름 — 다른 프로젝트 파일과 이름이 겹쳐도 섞지 않는다(아래 test_files가 그 프로젝트 것을 넣는다)
        if key.startswith('file:'):
            name = key[5:]
            path = Path(repo or Path(__file__).resolve().parents[1]) / 'tests' / name
            imports = set()
            if path.exists():
                try:
                    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                        if isinstance(node, ast.Import):
                            imports.update(a.name.split('.')[0] for a in node.names)
                        elif isinstance(node, ast.ImportFrom) and node.module:
                            imports.add(node.module.split('.')[0])
                except (SyntaxError, UnicodeError):
                    pass
            auto = level == 'high' or name in named_tests or 'tests/' + name in changed or bool(modules & imports) or name == 'test_ui.py' and ui and level != 'light'
        else:
            auto = {'agent_related': level != 'light' or any(p.startswith('tests/') for p in changed), 'agent_api': 'app' in modules or high,
                    'agent_capture': ui, 'server_api_warning': 'app' in modules or high}.get(key, level != 'light')
        reason = ('계획서 경로로 추정' if inferred_plan and changed else
                  '실제 변경 범위로 선택' if auto else '변경 범위와 관련 없음')
        if key in choice.get('checks', {}):
            auto = choice['checks'][key]
            reason = '계획 에이전트: ' + choice['reason']
        if high and (key.startswith('file:') or key in ('server_completion', 'server_merge', 'server_rollback', 'agent_related', 'agent_api', 'server_api_warning')):
            auto, reason = True, '서버 보강: 인증·DB·공통 실행 변경'
        mode = file_mode(saved, pk, key[5:]) if key.startswith('file:') else saved.get(key, 'auto')
        checks[key] = {'mode': mode, 'enabled': mode == 'on' or mode == 'auto' and auto,
                       'reason': reason if mode == 'auto' else '사람 설정: ' + mode.upper(), 'name': row['name']}
    # 다른 프로젝트의 독자 테스트는 이름 일치/고위험/계획 선택이 없으면 직접 실행 단계의 기본 검사로 본다.
    if repo:
        for path in test_files(repo, pk):
            key = 'file:' + path.name
            if key not in checks:
                auto = high or choice.get('checks', {}).get(key, True)
                mode = file_mode(saved, pk, path.name)   # 파일 설정은 프로젝트별이라 다른 프로젝트의 같은 이름 검사에 번지지 않는다
                reason = '서버 보강: 고위험 변경' if high else '계획 에이전트: ' + choice['reason'] if key in choice.get('checks', {}) else '프로젝트 자체 테스트'
                checks[key] = {'mode': mode, 'enabled': mode == 'on' or mode == 'auto' and auto,
                               'reason': reason if mode == 'auto' else '사람 설정: ' + mode.upper(), 'name': path.name}
    if not ref and not changed:
        for key, item in checks.items():
            if item['mode'] == 'auto':
                item.update(enabled=True, reason='Task 문맥 없는 직접 검사')
    human_off = []
    for name in sorted(changed_tests):
        item = checks.get('file:' + name)
        if item is None:
            continue
        if item['mode'] == 'off':
            human_off.append(name)
        else:
            item.update(enabled=True, reason='Task가 추가·수정한 검사 — 계획 선택과 관계없이 실행')
    changed_on = any(checks['file:' + n]['enabled'] for n in changed_tests if 'file:' + n in checks)
    if changed_on and checks['agent_related']['mode'] == 'auto':
        checks['agent_related'].update(enabled=True, reason='Task가 추가·수정한 검사를 직접 실행')
    selected_files = any(item['enabled'] for key, item in checks.items() if key.startswith('file:'))
    forced_file = any(item['mode'] == 'on' for key, item in checks.items() if key.startswith('file:'))
    for key in ('server_completion', 'server_merge', 'server_rollback'):
        if checks[key]['mode'] == 'auto' and (key not in choice.get('checks', {}) or forced_file):
            checks[key].update(enabled=selected_files, reason='선택된 테스트 파일이 있어 실행' if selected_files else '선택된 테스트 파일 없음')
        if checks[key]['mode'] == 'auto' and changed_on and key != 'server_rollback':
            checks[key].update(enabled=True, reason='Task가 추가·수정한 검사가 있어 실행')
    scan = Path(repo or Path(__file__).resolve().parents[1])
    labels = {}
    if scan.is_dir():
        found = {f.name: (f.relative_to(scan) if f.is_absolute() and f.is_relative_to(scan) else f).as_posix() for f in test_files(scan, pk)}
        by_path = registry.file_numbers(pk, found.values())
        labels = {name: by_path.get(rel, '') for name, rel in found.items()}
    for key, item in checks.items():
        item['number'] = labels.get(key[5:], '') if key.startswith('file:') else registry.number_of(key)
    return {'level': level, 'project': pk, 'changed_files': changed, 'planned': bool(choice), 'checks': checks,
            'changed_tests': sorted(changed_tests), 'changed_tests_off': human_off}


def test_files(repo, project_key=None):
    """프로젝트의 검사 규칙(orchestrate.json `tests`, 없으면 기본 — 루트 검사와 Moai의 패키지 검사)으로 찾는다(DEV-212, test_runner)."""
    import test_runner
    return test_runner.test_files(repo, project_key)


def work_tree_hash(repo):
    """작업 폴더의 실제 내용으로 만든 트리 해시. 임시 인덱스를 쓰므로 실제 인덱스·작업 폴더는 그대로다.
    커밋 전(Codex 완료 검사)이어도 execute가 나중에 만들 커밋의 트리와 같다(같은 add -A·캡처 경로 제외 규칙)."""
    import execute
    import os
    import tempfile
    if not Path(repo).is_dir():
        return None
    fd, index = tempfile.mkstemp(prefix='dev-index-')
    os.close(fd)
    os.unlink(index)   # git이 빈 인덱스를 새로 만들게 한다
    env = {**os.environ, 'GIT_INDEX_FILE': index}

    def git(*args):
        return subprocess.run(['git', *args], cwd=str(repo), env=env, capture_output=True, text=True, encoding='utf-8', errors='replace',
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    try:
        for args in (('read-tree', 'HEAD'), ('add', '-A', '--', '.'),
                     ('rm', '-r', '--cached', '-q', '--ignore-unmatch', '--', *execute.CAPTURE_PATHS)):
            if git(*args).returncode:
                return None
        tree = git('write-tree')
        return tree.stdout.strip() if not tree.returncode and tree.stdout.strip() else None
    finally:
        Path(index).unlink(missing_ok=True)


def tree_identity(repo):
    tree = work_tree_hash(repo)
    return tree + '\n' + sys.version + str(sys.executable) if tree else None


def fingerprint(test, identity):
    return hashlib.sha256((identity + '\n' + test.name).encode() + test.read_bytes() + Path(__file__).read_bytes()).hexdigest() if identity else None


def load_record(key):
    with db.connect() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='app_settings'").fetchone():
            return {}
        row = c.execute('SELECT value FROM app_settings WHERE key=?', (key,)).fetchone()
        return json.loads(row['value']) if row else {}


def save_record(key, value):
    with db.connect() as c:
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='app_settings'").fetchone():
            c.execute('INSERT INTO app_settings(key,value,updated_at,updated_by) VALUES(?,?,?,?) '
                      'ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at,updated_by=excluded.updated_by',
                      (key, json.dumps(value, ensure_ascii=False), db.now_iso(), 'server'))
