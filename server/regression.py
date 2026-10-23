"""서버 검사 단계·개별 파일과 에이전트 검사 지시를 공통 설정으로 관리한다."""
import ast
import hashlib
import json
import re
import sys
from pathlib import Path

import db
import issues

CHECKS = (
    ('server_completion', '서버', '완료 전 테스트', '작업 완료 등록 전에 선택된 테스트 파일을 실행해요. 실패하면 한 번 수정 재시도해요.'),
    ('server_merge', '서버', '병합 전 테스트', '시험 병합 결과를 검사해요. 동일 코드의 통과 결과를 재사용해요.'),
    ('server_rollback', '서버', '롤백 후 테스트', '롤백 사본에서 선택된 테스트 파일을 실행해요.'),
    ('server_api_warning', '서버', '새 API 검사 누락 경고', '새 라우트에 실제 요청 테스트가 있는지 검사하고, 없으면 완료 요약에 경고해요.'),
    ('agent_related', '에이전트', 'Task 관련 검사', 'Task 확인 항목과 직접 작성·수정한 검사를 실행하도록 지시해요. Claude·Codex 공통이에요.'),
    ('agent_api', '에이전트', '새 API 요청 검사', '새 HTTP 라우트에 실제 앱 요청 검사를 작성·실행하도록 지시해요.'),
    ('agent_capture', '에이전트', 'UI 캡처 확인', '변경 화면을 낮·밤 × PC·모바일로 캡처하고 확인하도록 지시해요.'),
)


def catalog():
    rows = [dict(key=k, group=g, name=n, description=d) for k, g, n, d in CHECKS]
    for path in sorted(Path(__file__).resolve().parents[1].joinpath('tests').glob('test_*.py')):
        try:
            doc = ast.get_docstring(ast.parse(path.read_text(encoding='utf-8'))) or '개별 회귀 검사'
        except (OSError, SyntaxError, UnicodeError):
            doc = '개별 회귀 검사'
        rows.append(dict(key='file:' + path.name, group='테스트 파일', name=path.name,
                         description=doc.splitlines()[0]))
    return rows


def values():
    with db.connect() as c:
        # 프롬프트 생성·독립 Git 검사는 DB 초기화 전에도 기본 설정으로 동작한다.
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_settings'").fetchone():
            return {}
        return {r['key'][11:]: {'true': 'on', 'false': 'off'}.get(r['value'], r['value']) for r in c.execute(
            "SELECT key,value FROM app_settings WHERE key LIKE 'regression:%'")}


def enabled(key, ref=None, repo=None):
    return resolve(ref, repo)['checks'].get(key, {'enabled': True})['enabled']


def settings():
    saved = values()
    return {'checks': [{**row, 'mode': saved.get(row['key'], 'auto')} for row in catalog()]}


def update(actor, changes):
    if actor['kind'] != 'human':
        raise issues.StoreError('검사 설정은 사람만 바꿀 수 있어요.', 403)
    keys = {row['key'] for row in catalog()}
    if not isinstance(changes, dict) or not changes or any(k not in keys or v not in ('on', 'off', 'auto') for k, v in changes.items()):
        raise issues.StoreError('검사 종류와 On·Off·Auto 값을 확인해 주세요.', 400)
    with db.connect() as c:
        for key, value in changes.items():
            c.execute('INSERT INTO app_settings(key,value,updated_at,updated_by) VALUES(?,?,?,?) '
                      'ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at,updated_by=excluded.updated_by',
                      ('regression:' + key, value, db.now_iso(), issues.actor_label(actor)))
    return settings()


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
            if any((k not in keys and not re.fullmatch(r'file:test_[A-Za-z0-9_]+\.py', k)) or type(v) is not bool for k, v in choice['checks'].items()):
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
        files = ', '.join(f"file:{p.name}={Path(p).relative_to(repo).as_posix()}" for p in test_files(repo)) if repo and Path(repo).is_dir() else ''
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
    changed_tests = {Path(p).name for p in changed if re.search(r'(^|/)tests/test_[A-Za-z0-9_]+\.py$', p)}
    dev = _is_dev(repo, issue)
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
        mode = saved.get(key, 'auto')
        checks[key] = {'mode': mode, 'enabled': mode == 'on' or mode == 'auto' and auto,
                       'reason': reason if mode == 'auto' else '사람 설정: ' + mode.upper(), 'name': row['name']}
    # 다른 프로젝트의 독자 테스트는 이름 일치/고위험/계획 선택이 없으면 직접 실행 단계의 기본 검사로 본다.
    if repo:
        for path in test_files(repo):
            key = 'file:' + path.name
            if key not in checks:
                auto = high or choice.get('checks', {}).get(key, True)
                mode = saved.get(key, 'auto')   # 사람의 파일 이름별 On/Off는 다른 프로젝트의 같은 이름 검사에도 적용된다
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
    return {'level': level, 'changed_files': changed, 'planned': bool(choice), 'checks': checks,
            'changed_tests': sorted(changed_tests), 'changed_tests_off': human_off}


def test_files(repo):
    """루트 검사와 Moai의 패키지 검사를 모두 찾는다."""
    return sorted({path for folder in ('tests', 'backend/tests')
                   for path in Path(repo, folder).glob('test_*.py')})


def tree_identity(repo):
    import execute
    if not Path(repo).is_dir():
        return None
    status = execute._run_git(str(repo), 'status', '--porcelain', '--untracked-files=no')
    tree = execute._run_git(str(repo), 'rev-parse', 'HEAD^{tree}')
    added = execute._run_git(str(repo), 'ls-files', '--others', '--exclude-standard')
    if status.returncode or status.stdout.strip() or tree.returncode or added.returncode or any(p.endswith(('.py', '.js', '.ts', '.tsx', '.jsx', '.vue', '.css', '.html')) for p in added.stdout.splitlines()):
        return None
    return tree.stdout + sys.version + str(sys.executable)


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
