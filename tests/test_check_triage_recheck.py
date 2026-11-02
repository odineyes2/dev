"""실패 관련 여부·재작업 안내와 좁은 재검사의 실행·캐시를 확인한다."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
os.environ['NTFY_TOPIC'] = ''
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import db, issues, regression, orchestrate, test_runner

db.init()
repo = Path(tempfile.mkdtemp())
(repo / 'tests').mkdir()
(repo / 'static').mkdir()
ui = repo / 'tests/test_ui.py'
other = repo / 'tests/test_other.py'
ui.write_text('\n'.join('# block ' + str(i) for i in range(40)) + '\n', encoding='utf-8')
other.write_text('# 다른 검사\n', encoding='utf-8')
js = 'function alpha() {\n return 1;\n}\nfunction beta() {\n return 2;\n}\n'
(repo / 'static/app.js').write_bytes(js.encode())
g = lambda *args: subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, check=True)
g('init', '-q', '-b', 'main')
g('config', 'core.autocrlf', 'false')
g('add', '.')
g('-c', 'user.name=test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'base')
sha = g('rev-parse', 'HEAD').stdout.strip()
data = {'generated_from': {'static/app.js': g('rev-parse', 'HEAD:static/app.js').stdout.strip()},
        'blocks': {b: {'complete': True, 'js': {'static/app.js': [f]}} for b, f in [('a', 'alpha'), ('b', 'beta'), ('c', 'beta')]}}
(repo / 'tests/ui_block_map.json').write_text(json.dumps(data), encoding='utf-8')
(repo / 'static/app.js').write_bytes(js.replace('return 1', 'return 11').encode())
selection = {'project': 'TR', 'changed_files': ['static/app.js'], 'changed_tests': [], 'blocks': {},
             'checks': {'server_completion': {'enabled': True, 'name': '완료 검사'}}}
listed = [{'id': b, 'lines': [i * 10 + 1, i * 10 + 10]} for i, b in enumerate('abc')]
actor = {'kind': 'human', 'name': 'admin'}
issues.create_project(actor, 'TR', 'recheck', '', str(repo))
ref = issues.create_issue(actor, 'TR', 'recheck')['ref']

with patch.object(regression, 'test_files', return_value=[ui, other]):
    assert regression.failure_related(repo, ui, 'a', selection) is True
    assert regression.failure_related(repo, ui, 'b', selection) is False
    assert regression.failure_related(repo, ui, 'b', {**selection, 'changed_files': ['server/app.py', 'static/app.js']}) is None
    assert regression.failure_related(repo, ui, None, selection) is None
    data['blocks']['b']['complete'] = False
    (repo / 'tests/ui_block_map.json').write_text(json.dumps(data), encoding='utf-8')
    assert regression.failure_related(repo, ui, 'b', selection) is None
    data['blocks']['b']['complete'] = True
    (repo / 'tests/ui_block_map.json').write_text(json.dumps(data), encoding='utf-8')
    (repo / 'static/app.js').write_bytes((js + 'let x = 1;\n').encode())
    assert regression.failure_related(repo, ui, 'b', selection) is None
    (repo / 'static/app.js').write_bytes(js.replace('return 1', 'return 11').encode())
print('OK — static 관련·무관 / 서버 변경·불완전·최상위 변경은 판단 보류')

def result(code, text):
    return subprocess.CompletedProcess([], code, text, '')

with patch.object(regression, 'resolve', return_value=selection), patch.object(regression, 'test_files', return_value=[ui]), patch.object(test_runner, 'list_blocks', return_value=listed), patch.object(orchestrate, 'run_test', side_effect=[result(1, 'FAIL: block:b'), result(1, 'FAIL: block:b')]):
    assert orchestrate.run_tests(str(repo), stage='server_completion', ref=ref)
rows = regression.load_record('regression-selection:' + ref + ':server_completion')['triage']
assert rows[0]['related'] is False
focus, note = regression.rework_context(repo, ref)
assert focus == {'since': sha, 'files': {'test_ui.py': ['b']}}
assert 'base에서도 실패하는지' in note and '관련이 없으면 고치지 말고' in note
print('OK — 실제 분류 결과와 재작업 안내·HEAD·실패 블록 고정')

# 재작업이 c 블록을 바꾸고 alpha 화면도 바꿨다. 실패 b와 합쳐 a,b,c만 돈다.
lines = ui.read_text(encoding='utf-8').splitlines()
lines[24] = '# 수정한 c 블록'
ui.write_text('\n'.join(lines) + '\n', encoding='utf-8')
seen = []
def fake(repo, path, **kwargs):
    seen.append((path.name, kwargs.get('args')))
    return result(0, 'OK: block:a\nOK: block:b\nOK: block:c')

regression.save_record('regression-cache:' + ref, {'test_other.py': 'old'})
with patch.object(regression, 'resolve', return_value=selection), patch.object(regression, 'test_files', return_value=[ui, other]), patch.object(regression, 'blocks_of', side_effect=lambda repo, path: listed if path == ui else None), patch.object(orchestrate, 'run_test', side_effect=fake):
    assert orchestrate.run_tests(str(repo), stage='server_completion', ref=ref, focus=focus) is None
assert set(seen[0][1][1].split(',')) == {'a', 'b', 'c'}, seen
assert seen[1:] == [('test_ui.py', ('--only', 'c'))] * 2, seen
record = regression.load_record('regression-selection:' + ref + ':server_completion')
assert record['results'] == {'test_ui.py': 'passed', 'test_other.py': 'deferred'}
cache = regression.load_record('regression-cache:' + ref)
assert 'test_ui.py' not in cache and 'test_other.py' not in cache
print('OK — 실패+재작업 검사+화면 대응 블록·R09 반복·deferred·캐시 미기록')

with patch.object(regression, 'blocks_of', side_effect=lambda repo, path: listed if path == ui else None):
    (repo / 'tests/ui_block_map.json').unlink()
    wanted, plans, _ = regression.focus_plan(repo, [ui, other], focus)
    assert wanted['test_ui.py'] is None and plans['test_ui.py']['whole_once']
    (repo / 'tests/ui_block_map.json').write_text(json.dumps(data), encoding='utf-8')
    # since 이후 커밋된 변경과 커밋 전 변경을 함께 읽는다.
    g('add', 'tests/test_ui.py')
    g('-c', 'user.name=test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'rework')
    wanted, plans, edited = regression.focus_plan(repo, [ui, other], focus)
    assert set(wanted['test_ui.py']) == {'a', 'b', 'c'} and plans['test_ui.py']['repeat'] == ['c']
print('OK — 대응표 없음은 화면 전체·재작업 커밋과 미커밋 변경 합산')

with patch.object(regression, 'resolve', return_value=selection), patch.object(regression, 'test_files', return_value=[ui, other]), patch.object(regression, 'blocks_of', side_effect=lambda repo, path: listed if path == ui else None), patch.object(test_runner, 'list_blocks', return_value=listed), patch.object(orchestrate, 'run_test', return_value=result(1, 'FAIL: block:b')):
    assert orchestrate.run_tests(str(repo), stage='server_completion', ref=ref, focus=focus)
print('OK — 재작업 뒤 진짜 실패는 계속 실패')

# 유료 호출 없이 실제 재작업 루프가 focus를 넘기고 남은 실패를 실행 실패로 만드는지 본다.
import review, execute
task = issues.create_issue(actor, 'TR', '재작업 Task', parent=ref)['ref']
real_popen = subprocess.Popen
for remaining in (None, '진짜 실패가 남음'):
    log, run_id = review.begin(actor, issues.get_issue(task), 'execute')
    prompts, checks = [], []
    def popen(command, **kwargs):
        proc = real_popen([sys.executable, '-c', 'pass'], **kwargs)
        communicate = proc.communicate
        def capture(*args, **kw):
            prompts.append(kw.get('input'))
            return communicate(*args, **kw)
        proc.communicate = capture
        return proc
    def check(cwd, **kwargs):
        checks.append(kwargs)
        return '처음 실패' if len(checks) == 1 else remaining
    with patch.object(orchestrate, 'run_tests', side_effect=check), patch.object(regression, 'rework_context', return_value=(focus, note)), patch.object(review.subprocess, 'Popen', side_effect=popen), patch.object(execute, 'register_completion') as complete:
        status = review.run_headless(actor, task, log, run_id, [sys.executable, '-p', '원래 지시'], None, None, 30, '실행')
    assert status == ('failed' if remaining else 'ok')
    assert complete.called is (remaining is None)
    assert 'focus' not in checks[0] and checks[1]['focus'] == focus
    assert note in prompts[1]
print('OK — 재작업 루프의 안내·focus 전달·남은 진짜 실패 실행 차단')
