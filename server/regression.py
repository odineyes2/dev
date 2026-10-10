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


def planning_instructions():
    return ('\n계획서에 `## 검사 선택` 절과 json 코드 블록을 추가한다. Task 번호별로 검사 수준과 선택 이유, '
            'Auto 항목의 필요 여부를 적는다. 형식: '
            '{"1":{"level":"light","reason":"문구만 변경","checks":{"agent_capture":true,"agent_api":false}}}. '
            '수준은 light(문구/화면/문서), normal(개별 기능), high(인증/DB/공통 실행)이다. '
            '각 Task의 Auto 항목을 모두 판단하고 개별 파일도 file:test_x.py 키로 선택한다. '
            'On/Off는 사람 설정이며 바꾸지 않는다. 검사 키와 현재 모드: '
            + ', '.join(row['key'] + '=' + row['mode'] for row in settings()['checks']) + '\n')


def resolve(ref=None, repo=None, changed=None):
    """사람 설정 > 실제 고위험 변경 > 승인 계획 > 변경 범위 기본값 순으로 결정한다."""
    import execute
    saved = values()
    issue, choice = None, {}
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
                tasks = issues.parse_tasks((parent.get('plan') or {}).get('body', ''))
                task = next((t for t in tasks if t['title'] == issue['title']), None)
                choices = parse_plan((parent.get('plan') or {}).get('body', ''))
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
            changed = list(execute.task_files(issue))
    changed = sorted(set(p.replace('\\', '/') for p in changed))
    named_tests = set(re.findall(r'tests/(test_[A-Za-z0-9_]+\.py)', (issue or {}).get('body', '')))
    critical = {'auth', 'db', 'issues', 'execute', 'review', 'jobs', 'orchestrate', 'rollback', 'regression'}
    modules = {Path(p).stem for p in changed if p.endswith('.py') and not p.startswith('tests/')}
    high = bool(modules & critical) or any(re.search(r'(^|/)(auth|migrations|security)(/|\.)', p) for p in changed)
    ui = any(p.startswith(('static/', 'src/components/', 'src/pages/')) or p.endswith(('.css', '.html', '.tsx', '.jsx', '.vue')) for p in changed)
    code = any(p.endswith(('.py', '.js', '.ts', '.tsx', '.jsx', '.vue')) for p in changed)
    level = 'high' if high else choice.get('level') or ('normal' if not changed or code else 'light')
    checks = {}
    for row in catalog():
        key = row['key']
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
        reason = '실제 변경 범위로 선택' if auto else '변경 범위와 관련 없음'
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
        for path in Path(repo, 'tests').glob('test_*.py'):
            key = 'file:' + path.name
            if key not in checks:
                auto = choice.get('checks', {}).get(key, True)
                checks[key] = {'mode': 'auto', 'enabled': high or auto, 'reason': '서버 보강: 고위험 변경' if high else '계획 에이전트: ' + choice['reason'] if key in choice.get('checks', {}) else '프로젝트 자체 테스트', 'name': path.name}
    if not ref and not changed:
        for key, item in checks.items():
            if item['mode'] == 'auto':
                item.update(enabled=True, reason='Task 문맥 없는 직접 검사')
    selected_files = any(item['enabled'] for key, item in checks.items() if key.startswith('file:'))
    forced_file = any(item['mode'] == 'on' for key, item in checks.items() if key.startswith('file:'))
    for key in ('server_completion', 'server_merge', 'server_rollback'):
        if checks[key]['mode'] == 'auto' and (key not in choice.get('checks', {}) or forced_file):
            checks[key].update(enabled=selected_files, reason='선택된 테스트 파일이 있어 실행' if selected_files else '선택된 테스트 파일 없음')
    return {'level': level, 'changed_files': changed, 'planned': bool(choice), 'checks': checks}


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
