"""검사 블록 단위 실행(DEV-231) — 블록 목록 캐시·블록 선택의 --only·바뀐 줄→바뀐 블록·R09 블록만 반복·블록 시간 기록·블록 미지원 파일."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
os.environ['NTFY_TOPIC'] = ''
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import db, orchestrate, regression, test_runner  # noqa: E402

db.init()

repo = Path(tempfile.mkdtemp())
(repo / 'tests').mkdir()
fake = repo / 'tests' / 'test_fake.py'
fake.write_text('''"""가짜 블록 검사."""
import json, sys
BLOCKS = [("alpha", "첫 블록", 10, 12), ("beta", "둘째 블록", 14, 16)]
if '--LIST' in sys.argv:
    print(json.dumps([{"id": b[0], "title": b[1], "lines": [b[2], b[3]]} for b in BLOCKS]))
    sys.exit(0)
only = sys.argv[sys.argv.index('--only') + 1].split(',') if '--only' in sys.argv else [b[0] for b in BLOCKS]
for b in only:
    print('OK: block:' + b)
'''.replace('--LIST', '--list-' + 'blocks'), encoding='utf-8')   # 이 파일 자체는 블록 검사로 보이지 않게 이어 붙인다
plain = repo / 'tests' / 'test_plain.py'
plain.write_text('"""블록 없는 검사."""\nprint("OK — 끝")\n', encoding='utf-8')

# --list-blocks와 캐시 — 같은 내용이면 다시 띄우지 않는다
listed = regression.blocks_of(repo, fake)
assert [b['id'] for b in listed] == ['alpha', 'beta'] and listed[0]['lines'] == [10, 12], listed
calls = []
real_list = test_runner.list_blocks
test_runner.list_blocks = lambda *a, **k: calls.append(a) or real_list(*a, **k)
assert regression.blocks_of(repo, fake) == listed and not calls
assert regression.blocks_of(repo, plain) is None   # 블록 미지원
test_runner.list_blocks = real_list
print('OK — 블록 목록 캐시')

# 블록 시간·--only 인자
r = test_runner.run(str(repo), fake, ('--only', 'beta'), dict(os.environ))
assert r.returncode == 0 and set(r.blocks) == {'beta'}, (r.stdout, r.blocks)
print('OK — --only·블록 시간')

# 바뀐 줄 → 바뀐 블록, 블록 밖이면 전체 1회
p = regression.block_plan(listed, {11}, ['alpha'])
assert p['only'] == ['alpha'] and p['repeat'] == ['alpha'] and not p['whole_once'], p
p = regression.block_plan(listed, {15}, ['alpha'])
assert p['only'] == ['alpha', 'beta'] and p['repeat'] == ['beta'], p
p = regression.block_plan(listed, {3, 15})
assert p['whole_once'] and not p['only'], p
assert regression.block_plan(listed, set())['whole_once']   # 새 파일
p = regression.block_plan(listed, None, ['beta', 'nope'])
assert p['only'] == ['beta'] and p['repeat'] == [], p
print('OK — 바뀐 줄 → 바뀐 블록')

# git diff에서 바뀐 줄 읽기
g = lambda *a: subprocess.run(['git', '-C', str(repo), *a], capture_output=True, text=True, check=True)  # noqa: E731
g('init', '-q', '-b', 'main')
g('-c', 'user.email=t@t', '-c', 'user.name=t', 'add', '-A')
g('-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'base')
text = fake.read_text(encoding='utf-8').splitlines()
text[2] = text[2] + '  # 바꿈'
fake.write_text('\n'.join(text) + '\n', encoding='utf-8')
assert regression.changed_lines(repo, 'tests/test_fake.py') == {3}
print('OK — git diff 바뀐 줄')


# 서버 완료 검사: 블록 선택·R09 블록만 2번 더·블록 밖은 전체 1회·병합은 전체·블록 미지원은 기존 3회
def run_stage(stage, blocks, changed):
    seen = []
    real = orchestrate.run_test

    def fake_run(repo_, t, args=(), env=None, on_item=None):
        seen.append((t.name, tuple(args)))
        return real(repo_, t, args, env=env, on_item=on_item)
    checks = {k: {'enabled': True, 'name': k, 'mode': 'auto'} for k in ('server_completion', 'server_merge')}
    selection = {'checks': checks, 'changed_tests': changed, 'blocks': blocks}
    olds = (regression.resolve, regression.test_files, orchestrate.run_test)
    regression.resolve = lambda *a, **k: selection
    regression.test_files = lambda *a, **k: [fake, plain]
    orchestrate.run_test = fake_run
    try:
        assert orchestrate._run_tests(str(repo), stage, None, [], None) is None
    finally:
        regression.resolve, regression.test_files, orchestrate.run_test = olds
    return seen


seen = run_stage('server_completion', {'test_fake.py': regression.block_plan(listed, {15}, ['alpha'])}, ['test_fake.py'])
assert [s for s in seen if s[0] == 'test_fake.py'] == [('test_fake.py', ('--only', 'alpha,beta'))] + [('test_fake.py', ('--only', 'beta'))] * 2, seen
assert [s for s in seen if s[0] == 'test_plain.py'] == [('test_plain.py', ())], seen
print('OK — 블록 선택·R09 바뀐 블록만 2회 추가')

seen = run_stage('server_completion', {'test_fake.py': regression.block_plan(listed, {3})}, ['test_fake.py'])
assert [s for s in seen if s[0] == 'test_fake.py'] == [('test_fake.py', ())], seen
print('OK — 블록 밖 변경은 전체 1회')

seen = run_stage('server_merge', {'test_fake.py': regression.block_plan(listed, None, ['alpha'])}, ['test_fake.py'])
assert [s for s in seen if s[0] == 'test_fake.py'] == [('test_fake.py', ())], seen
print('OK — 병합 검사는 파일 전체')

seen = run_stage('server_completion', {}, ['test_plain.py'])
assert [s for s in seen if s[0] == 'test_plain.py'] == [('test_plain.py', ())] * 3, seen
print('OK — 블록 미지원 파일은 기존 3회')

timing = regression.load_record('regression-durations:' + regression.project_key(str(repo)))
assert 'test_fake.py#alpha' in timing and 'test_fake.py#beta' in timing and 'test_fake.py' in timing, json.dumps(timing)
print('OK — 블록 시간 기록')
