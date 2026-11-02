"""완료 검사 재실행 분류·기록·병합 분리를 가짜 검사로 확인한다."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
os.environ['NTFY_TOPIC'] = ''
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import db, issues, orchestrate, regression, test_runner

db.init()
repo = Path(tempfile.mkdtemp())
(repo / 'tests').mkdir()
test = repo / 'tests' / 'test_fake.py'
test.write_text('"""가짜 검사."""', encoding='utf-8')
actor = {'kind': 'human', 'name': 'admin'}
issues.create_project(actor, 'TR', 'triage', '', str(repo))


def result(code, text=''):
    return subprocess.CompletedProcess(['fake'], code, text, '')


def run(sequence, blocks=True, changed=False, stage='server_completion'):
    ref = issues.create_issue(actor, 'TR', 'triage case')['ref']
    seen = []
    policy = {k: {'enabled': True, 'name': k} for k in ('server_completion', 'server_merge')}
    selection = {'checks': policy, 'changed_tests': [test.name] if changed else [],
                 'blocks': {test.name: {'only': ['a', 'b'], 'repeat': ['a']}} if blocks else {}}

    def fake(*args, **kwargs):
        seen.append(kwargs.get('args', ()))
        return sequence[len(seen) - 1]

    with patch.object(regression, 'resolve', return_value=selection), patch.object(regression, 'test_files', return_value=[test]), patch.object(test_runner, 'list_blocks', return_value=[{'id': 'a'}, {'id': 'b'}] if blocks else None), patch.object(orchestrate, 'run_test', side_effect=fake):
        out = orchestrate.run_tests(str(repo), stage=stage, ref=ref)
    record = regression.load_record('regression-selection:' + ref + ':' + stage)
    return out, record.get('triage', []), seen, ref


out, rows, seen, ref = run([result(1, 'FAIL: block:a'), result(0, 'OK: block:a')])
assert out is None and seen[-1] == ('--only', 'a') and rows[0]['class'] == 'flaky'
assert regression.load_record('regression-flaky:TR')['test_fake.py#a']['flaky'] == 1
with db.connect() as c:
    assert c.execute("SELECT COUNT(*) FROM events WHERE kind='comment' AND body LIKE '%재실행 분류%'").fetchone()[0]
print('OK — 블록 복구·댓글·선택 기록·누적 기록')

out, rows, seen, _ = run([result(124, 'OK: block:a'), result(0, 'OK: block:b')])
assert out is None and seen[-1] == ('--only', 'b') and rows[0]['class'] == 'timeout'
print('OK — 시간 초과 미완료 블록만 재실행')

out, rows, seen, _ = run([result(1, 'FAIL: block:a\nFAIL: block:b'), result(1, 'OK: block:a\nFAIL: block:b')])
assert [r['class'] for r in rows] == ['flaky', 'real'] and '진짜 실패 블록: b' in out and 'FAIL: block:a' not in out
print('OK — 진짜 실패만 재작업 출력')

out, rows, seen, _ = run([result(1, 'FAIL: block:a'), result(0, 'OK: block:a'), result(0, 'OK: block:a'), result(0, 'OK: block:a')], changed=True)
assert out and orchestrate.FLAKY_MARK in out and rows[0]['class'] == 'real' and rows[0]['related'] is True
print('OK — Task 변경 블록은 복구되어도 진짜 실패')

for blocks in (False, True):
    out, rows, seen, _ = run([result(1), result(0)], blocks=blocks)
    assert out is None and seen[-1] == () and rows[0]['block'] is None
print('OK — 블록 없음·표식 없음은 파일 전체 재실행')

out, rows, seen, _ = run([result(1), result(0), result(0)], stage='server_merge')
assert out and not rows and len(seen) == 3
print('OK — 병합은 기존 R07 유지')

# 실제 프로세스의 통과·실패 표식도 같은 방식으로 읽는다.
test.write_text('print("OK: block:a")\nprint("FAIL: block:b")\nraise SystemExit(1)\n', encoding='utf-8')
actual = test_runner.run(str(repo), test, env=dict(os.environ))
assert actual.block_status == {'a': 'OK', 'b': 'FAIL'}
assert 'FAIL: block:b' in test_runner.block_output(actual, 'b')
assert regression.triage_line([{'file': 'x.py', 'block': 'a', 'class': 'flaky', 'rerun': 'pass'}]) == 'x.py#a 불안정(재실행 통과)'
print('OK — 실제 프로세스 표식·완료 메모 줄')

# 재실행 시간 초과와 표식 없는 시간 초과는 실제 실패로 남긴다.
out, rows, seen, _ = run([result(1, 'FAIL: block:a'), result(124)])
assert out and rows[0]['class'] == 'real' and rows[0]['rerun'] == 'timeout'
out, rows, seen, _ = run([result(124), result(0)])
assert out is None and seen[-1] == () and rows[0]['class'] == 'timeout'
print('OK — 재실행 시간 초과·표식 없는 파일 재실행')

# 블록이 끝난 뒤 공용 정리 코드가 실패하면 통과로 넘기지 않는다.
out, rows, seen, _ = run([result(1, 'FAIL: block:a'), result(1, 'OK: block:a')])
assert out and rows[0]['class'] == 'real' and rows[0]['rerun'] == 'fail'
print('OK — 블록 종료 뒤 공용 실패도 보존')
