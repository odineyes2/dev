"""test_ui.py 블록 표(DEV-231·233) — 블록 id 중복 없음·블록 사이 검사 함수 겹침 없음·옛 블록 id와 옛 --x-only가 별칭으로 풀림·lines가 실제 함수 범위인지 본다(브라우저 없이)."""
import ast, json, re, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / 'tests' / 'test_ui.py'


def run(*args):
    r = subprocess.run([sys.executable, str(UI), *args], cwd=ROOT, capture_output=True, timeout=60)
    return r.returncode, r.stdout.decode('ascii'), r.stderr.decode('utf-8', 'replace')   # 목록 출력은 콘솔 인코딩과 무관한 ASCII JSON


code, out, err = run('--list-blocks')
assert code == 0, err
blocks = json.loads(out.strip().splitlines()[-1])
ids = [b['id'] for b in blocks]
assert len(ids) == len(set(ids)), [i for i in ids if ids.count(i) > 1]
assert all(re.fullmatch(r'[\w.-]+', i) and b['title'] for i, b in zip(ids, blocks)), blocks
code, out, err = run('--list-aliases')
assert code == 0, err
listed = json.loads(out.strip().splitlines()[-1])
aliases, flags = listed['aliases'], listed['flags']
print('OK — list-blocks·list-aliases ASCII JSON')

# 서버 쪽 읽기(test_runner.list_blocks)도 같은 목록을 받는다
sys.path.insert(0, str(ROOT / 'server'))
import test_runner
assert [b['id'] for b in test_runner.list_blocks(str(ROOT), UI) or []] == ids
print('OK — server reads the same blocks')

source = UI.read_text(encoding='utf-8')
tree = ast.parse(source)
functions = {(n.lineno, n.end_lineno): n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
for b in blocks:
    assert tuple(b['lines']) in functions, (b['id'], b['lines'])
name_of = {b['id']: functions[tuple(b['lines'])].name for b in blocks}
assert name_of['confirm'] == 'block_confirm' and name_of['issue-table'] == 'check_issue_table' and name_of['board-flow'] == 'block_board_flow'
print('OK — lines are real function ranges')

# 블록 사이 겹침 없음: 블록 함수에서 (중첩 호출까지) 닿는 검사 함수를 두 블록이 같이 쓰지 않는다
defs = {n.name: n for n in functions.values()}


def reach(name, seen):
    for n in ast.walk(defs[name]):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in defs and n.func.id not in seen                 and n.func.id.startswith(('check_', 'block_')):
            seen.add(n.func.id)
            reach(n.func.id, seen)
    return seen


owner = {}
for bid, name in name_of.items():
    for f in reach(name, {name}):
        owner.setdefault(f, []).append(bid)
shared = {f: o for f, o in owner.items() if len(o) > 1}
assert not shared, shared
print(f'OK — {len(owner)} check/block functions, each reached from one block')

# 옛 블록 id(DEV-231 표)와 옛 `--x-only` 옵션은 모두 지금 블록으로 풀린다
OLD_IDS = ['confirm', 'login', 'nav-shortcut', 'delegate-confirm', 'complete-summary', 'decision-layout', 'task-alignment', 'result-attach',
           'queue-limit', 'test-jobs', 'queue-edges', 'self-restart-banner', 'list-spacing', 'issue-table', 'suggestions', 'suggestions-regression',
           'trends', 'trend-issues', 'trend-stages', 'issue-summary', 'rating', 'manual-usage', 'progress-clock', 'issue-tokens', 'auto-master',
           'settings-navigation', 'global-agent-orders', 'publication', 'manual-actions', 'global-auto', 'free-port', 'service-diagnostic', 'fonts',
           'project-genres', 'project-wizard', 'project-registration', 'project-documents', 'harness', 'management-agents', 'management-delay-probe',
           'management-layout', 'management-projects', 'management', 'new-issue-fields', 'new-issue-dependencies', 'chat-connections', 'jobs-scroll',
           'list-sort', 'initialization', 'usage', 'reproduce', 'register-repair', 'manual-merge', 'troubleshooter', 'restart', 'audits', 'fab',
           'page-gutter', 'regression-numbers', 'composer-chain', 'composer-close', 'attachments', 'new-attachment-images', 'composer-images',
           'progress', 'progress-note', 'qa-start', 'board-actions', 'detail-side', 'issue-types', 'edit-saving', 'rollback', 'account-menu',
           'list-loading', 'project-flows']
for old in OLD_IDS:
    assert old in ids or old in aliases, old
    assert all(x in ids for x in aliases.get(old, [old])), (old, aliases.get(old))
    assert '--' + old + '-only' in flags, old
assert '--management-delay-probe' in flags
assert all(all(x in ids for x in v) for v in flags.values()), flags
assert set(aliases).isdisjoint(ids), set(aliases) & set(ids)
print(f'OK — {len(OLD_IDS)} old ids and {len(flags)} options resolve to blocks')

# 인자 풀기: --only(블록·묶음), 옛 --x-only, 인자 없음(전체)
resolve = lambda *a: json.loads(run(*a, '--resolve')[1].strip().splitlines()[-1])
assert resolve('--only', 'confirm,issue-table') == ['confirm', 'issue-table']
assert resolve('--only', 'suggestions-regression,suggestions') == ['suggestions', 'regression-numbers']
assert resolve('--issue-table-only') == ['issue-table'] and resolve('--auto-master-only') == ['auto-master', 'auto-settings']
assert resolve('--management-delay-probe') == ['management-delay-probe'] and resolve('--detail-side-only') == ['sticky-tabbar', 'new-issue-fields']
assert resolve() == ids
print('OK — arguments resolve to blocks')

# 모르는 블록·옵션은 서버를 띄우기 전에 거절한다
code, out, err = run('--only', 'confirm,nosuchblock')
assert code != 0 and 'nosuchblock' in err, (code, err)
code, out, err = run('--nosuch-only')
assert code != 0 and '--nosuch-only' in err, (code, err)
print(f'OK: {len(ids)} blocks, {len(aliases)} aliases, {len(flags)} legacy options')
