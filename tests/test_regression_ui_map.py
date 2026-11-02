"""화면 변경 → 대응표로 test_ui 블록 자동 선택(DEV-233-3) — 가짜 저장소·가짜 대응표로 선택·전체 1회 조건·계획 우선·별칭·선택 이유를 본다."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
os.environ['NTFY_TOPIC'] = ''
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
sys.path.insert(0, str(ROOT / 'scripts'))
import db, regression  # noqa: E402
import ui_block_map  # noqa: E402

db.init()

repo = Path(tempfile.mkdtemp())
(repo / 'tests').mkdir()
(repo / 'static').mkdir()
JS = 'function alpha() {\n  return 1;\n}\n\nfunction beta() {\n  return 2;\n}\n'
CSS = '.a {\n  color: red;\n}\n\n.b {\n  color: blue;\n}\n\n.c {\n  color: green;\n}\n'
HTML = '<!doctype html>\n<p>x</p>\n'
FILES = {'static/app.js': JS, 'static/app.css': CSS, 'static/index.html': HTML}
for rel, text in FILES.items():
    (repo / rel).write_bytes(text.encode('utf-8'))
# 가짜 test_ui.py — 블록 목록과 옛 이름 별칭만 답한다(이 파일 자체는 블록 검사로 보이지 않게 이어 붙인다)
(repo / 'tests' / 'test_ui.py').write_text('''"""가짜 화면 검사."""
import json, sys
if '--LIST-blocks' in sys.argv:
    print(json.dumps([{"id": i, "title": i, "lines": [n, n]} for n, i in enumerate(["one", "two", "three"], 10)])); sys.exit(0)
if '--LIST-aliases' in sys.argv:
    print(json.dumps({"aliases": {"old": ["one", "two"]}})); sys.exit(0)
print("OK: block:one")
'''.replace('--LIST', '--list'), encoding='utf-8')
MAP = {'generated_from': {rel: ui_block_map.blob_sha(text.encode('utf-8')) for rel, text in FILES.items()},
       'blocks': {'one': {'js': {'static/app.js': ['alpha']}, 'css': {'static/app.css': ['.a']}, 'complete': True},
                  'two': {'js': {'static/app.js': ['beta']}, 'css': {'static/app.css': ['.b']}, 'complete': True},
                  'three': {'js': {'static/app.js': ['beta']}, 'css': {'static/app.css': ['.c']}, 'complete': False}}}
map_path = repo / 'tests' / 'ui_block_map.json'
map_path.write_text(json.dumps(MAP), encoding='utf-8')
g = lambda *a: subprocess.run(['git', '-C', str(repo), *a], capture_output=True, text=True, check=True)  # noqa: E731
g('init', '-q', '-b', 'main')
g('config', 'core.autocrlf', 'false')
g('-c', 'user.email=t@t', '-c', 'user.name=t', 'add', '-A')
g('-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'base')


def pick(edits, choice=None, enabled=True):
    """edits: {경로: (바꿀 글, 새 글)}을 커밋하지 않은 채 적용하고 블록 선택을 본 뒤 되돌린다."""
    for rel, (old, new) in edits.items():
        (repo / rel).write_bytes(FILES[rel].replace(old, new).encode('utf-8'))
    checks = {'file:test_ui.py': {'mode': 'auto', 'enabled': enabled, 'reason': '실제 변경 범위로 선택', 'name': 'test_ui.py'}}
    try:
        out = regression.select_blocks(repo, '?', choice or {}, sorted(edits), set(), checks)
    finally:
        for rel in edits:
            (repo / rel).write_bytes(FILES[rel].encode('utf-8'))
    return out.get('test_ui.py'), checks['file:test_ui.py']


# CSS 선택자 변경 → 그 선택자를 쓰는 블록만, 이유를 선택 기록과 검사 이유에 남긴다
plan, item = pick({'static/app.css': ('red', 'pink')})
assert plan['only'] == ['one'] and not plan['whole_once'] and plan['repeat'] == [], plan
assert 'app.css `.a` → one' in plan['reason'] and plan['reason'] in item['reason'], (plan, item)
print('OK — CSS 선택자 변경 → 해당 블록만')

# JS 함수 변경 → 그 함수를 쓰는 블록만
plan, _ = pick({'static/app.js': ('return 1', 'return 11')})
assert plan['only'] == ['one'] and not plan['whole_once'], plan
assert 'app.js `alpha` → one' in plan['reason'], plan
print('OK — JS 함수 변경 → 해당 블록만')

# 정할 수 없으면 전체 1회
cases = {
    '새 함수': {'static/app.js': ('return 2;\n}\n', 'return 2;\n}\n\nfunction delta() {\n  return 4;\n}\n')},
    '최상위 코드': {'static/app.js': ('return 2;\n}\n', 'return 2;\n}\nlet x = 1;\n')},
    '모름': {'static/app.js': ('return 2;\n}\n', 'return 2;\n}\nfunction broken() {\n')},
    'index.html': {'static/index.html': ('x', 'y')},
    'complete:false': {'static/app.css': ('green', 'lime')},
}
for name, edits in cases.items():
    plan, _ = pick(edits)
    assert plan and plan['whole_once'] and plan['only'] == [] and plan['repeat'] == [], (name, plan)
    assert plan['reason'].startswith('대응표로 정할 수 없어 전체 1회'), (name, plan)
assert 'three' in pick(cases['complete:false'])[0]['reason']
print('OK — 새 함수·최상위·모름·index.html·complete:false → 전체 1회')

# 대응표를 만든 뒤 파일이 30% 넘게 바뀌었으면 전체 1회
old_css = subprocess.run(['git', '-C', str(repo), 'hash-object', '-w', '--stdin'], input='.z {\n  top: 0;\n}\n',
                         capture_output=True, text=True, check=True).stdout.strip()
map_path.write_text(json.dumps({**MAP, 'generated_from': {**MAP['generated_from'], 'static/app.css': old_css}}), encoding='utf-8')
plan, _ = pick({'static/app.css': ('red', 'pink')})
assert plan['whole_once'] and '30%' in plan['reason'], plan
map_path.write_text(json.dumps(MAP), encoding='utf-8')
print('OK — 30% 초과 → 전체 1회')

# 계획이 블록을 골랐으면 대응표를 쓰지 않고, 옛 블록 이름은 별칭으로 풀린다
plan, _ = pick({'static/app.css': ('red', 'pink')}, {'checks': {'file:test_ui.py#old': True}})
assert plan['only'] == ['one', 'two'] and 'reason' not in plan, plan
plan, _ = pick({'static/index.html': ('x', 'y')}, {'checks': {'file:test_ui.py#two': True}})
assert plan['only'] == ['two'] and not plan['whole_once'], plan
print('OK — 계획 선택 우선·옛 블록 이름 별칭')

# 꺼진 검사는 대응표가 켜지 않는다
plan, item = pick({'static/app.css': ('red', 'pink')}, enabled=False)
assert plan is None and not item['enabled'], (plan, item)
print('OK — 꺼진 검사는 그대로')

# resolve 전체 경로 — 선택 기록(blocks)에 대응표 선택과 이유가 남는다
(repo / 'static/app.css').write_bytes(CSS.replace('blue', 'navy').encode('utf-8'))
try:
    result = regression.resolve(None, repo, changed=['static/app.css'])
finally:
    (repo / 'static/app.css').write_bytes(CSS.encode('utf-8'))
plan = result['blocks']['test_ui.py']
assert plan['only'] == ['two'] and 'app.css `.b` → two' in plan['reason'], result['blocks']
assert plan['reason'] in result['checks']['file:test_ui.py']['reason'], result['checks']['file:test_ui.py']
print('OK — resolve 선택 기록')

# 계획 안내에 대응표 안내가 들어간다
assert '대응표' in regression._block_instructions(None, ROOT)
print('OK — 계획 안내')
