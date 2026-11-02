"""test_ui.py 블록 표(DEV-231) — --list-blocks의 id 중복 없음·기존 --x-only 옵션마다 블록이 있음·lines가 실제 함수 범위인지 본다(브라우저 없이)."""
import ast, json, re, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / 'tests' / 'test_ui.py'

r = subprocess.run([sys.executable, str(UI), '--list-blocks'], cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=60)
assert r.returncode == 0, r.stderr
blocks = json.loads(r.stdout.strip().splitlines()[-1])
ids = [b['id'] for b in blocks]
assert len(ids) == len(set(ids)), [i for i in ids if ids.count(i) > 1]
assert all(re.fullmatch(r'[\w.-]+', i) and b['title'] for i, b in zip(ids, blocks)), blocks

# 서버 쪽 읽기(test_runner.list_blocks)도 같은 목록을 받는다
sys.path.insert(0, str(ROOT / 'server'))
import test_runner
assert [b['id'] for b in test_runner.list_blocks(str(ROOT), UI) or []] == ids

source = UI.read_text(encoding='utf-8')
tree = ast.parse(source)
functions = {(n.lineno, n.end_lineno): n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
for b in blocks:
    assert tuple(b['lines']) in functions, (b['id'], b['lines'])
assert functions[tuple(blocks[ids.index('confirm')]['lines'])] == 'block_confirm'
assert functions[tuple(blocks[ids.index('issue-table')]['lines'])] == 'check_issue_table'

# 기존 `if '--x-only' in sys.argv` 옵션(재현 R16·옛 기록)마다 같은 이름의 블록이 있다
options = set()
for node in ast.walk(tree):
    if isinstance(node, ast.Compare) and isinstance(node.left, ast.Constant) and isinstance(node.left.value, str) \
            and re.fullmatch(r'--[a-z-]+', node.left.value) and ast.unparse(node.comparators[0]) == 'sys.argv':
        options.add(node.left.value)
options -= {'--only', '--list-blocks'}
assert len(options) >= 70, sorted(options)
missing = [o for o in options if re.sub(r'-only$', '', o[2:]) not in ids]
assert not missing, missing

# 모르는 블록은 서버를 띄우기 전에 거절한다
r = subprocess.run([sys.executable, str(UI), '--only', 'confirm,없는블록'], cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=60)
assert r.returncode != 0 and '없는블록' in r.stderr, (r.returncode, r.stderr)
print(f'OK: {len(ids)} blocks, {len(options)} legacy options')
