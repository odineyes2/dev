"""프로젝트별 검사 규칙(DEV-212) — orchestrate.json `tests`로 찾을 파일·명령·시간 제한을 정하고, 서버 검사·진행 n/N·파일 안 항목 진행(DEV-213)이 그 규칙을 따르는지."""
import json
import time
import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
os.environ['NTFY_TOPIC'] = ''
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))

import config, db, issues, orchestrate, regression, run_progress, test_runner  # noqa: E402,E401

db.init()
me = {'kind': 'human', 'name': 'admin'}
settings_file = config.DATA_DIR / 'orchestrate.json'


def write_settings(data):
    settings_file.write_text(json.dumps(data), encoding='utf-8')


# 1. 설정이 없으면 기본 규칙 — 지금까지와 같은 파일·명령.
repo = Path(tempfile.mkdtemp())
(repo / 'tests').mkdir()
(repo / 'backend' / 'tests').mkdir(parents=True)
(repo / 'checks').mkdir()
for rel, body in {'tests/test_a.py': 'pass', 'tests/test_b.js': '', 'backend/tests/test_c.py': 'pass',
                  'checks/one.check.py': 'print("ONE")', 'checks/two.check.py': 'import sys; sys.exit(3)'}.items():
    (repo / rel).write_text(f'# {rel}\n{body}\n', encoding='utf-8')
assert test_runner.rules('PX') == (test_runner.DEFAULT_RULES, '')
names = [t.relative_to(repo).as_posix() for t in test_runner.test_files(repo, 'PX')]
assert names == ['backend/tests/test_c.py', 'tests/test_a.py', 'tests/test_b.js'], names
by = {t.name: t for t in test_runner.test_files(repo)}
assert test_runner.command(str(repo), by['test_a.py']) == [sys.executable, str(by['test_a.py'])]
assert test_runner.command(str(repo), by['test_b.js']) == ['node', '--test', str(by['test_b.js'])]
assert test_runner.command(str(repo), by['test_c.py']) == [sys.executable, '-m', 'unittest', '-v', 'backend.tests.test_c']   # -v: 항목마다 결과 줄(DEV-213)
# 규칙 없이 받은 경로(재현 실행 등)도 기본 규칙에서 맞는 명령을 고른다.
assert test_runner.command(str(repo), repo / 'backend' / 'tests' / 'test_c.py')[1:3] == ['-m', 'unittest']
print('OK — 설정이 없으면 기본 규칙')

# 2. 프로젝트 규칙 — 그 규칙으로만 찾고, {python}·{file}·{module}을 채우고, timeout·browser를 따른다.
write_settings({'PX': {'tests': [{'glob': 'checks/*.check.py', 'run': ['{python}', '{file}', '--from', '{module}'], 'timeout': 77}]},
                'PB': {'tests': [{'glob': 'checks/one.check.py', 'run': ['{python}', '{file}'], 'browser': True}]}})
files = test_runner.test_files(repo, 'PX')
assert [t.name for t in files] == ['one.check.py', 'two.check.py'], files
assert regression.test_files(repo, 'PX') == files
one = files[0]
assert test_runner.command(str(repo), one) == [sys.executable, str(one), '--from', 'checks.one.check']
assert test_runner.timeout_for(one) == 77 and not test_runner.is_browser(one)
assert test_runner.is_browser(test_runner.test_files(repo, 'PB')[0])
assert test_runner.test_files(repo, 'DEV')[0].name == 'test_c.py'   # 다른 프로젝트 설정은 번지지 않는다
r = test_runner.run(str(repo), one, env=dict(os.environ))
assert r.returncode == 0 and 'ONE' in r.stdout, r
print('OK — 프로젝트 규칙으로 찾고 명령·시간 제한을 채운다')

# 3. 잘못 적은 규칙은 기본값으로 몰래 돌리지 않고 서버 검사 실패로 알린다.
for bad in ([], [{'glob': '../x/*.py', 'run': ['{python}', '{file}']}], [{'glob': 'tests/*.py', 'run': 'python {file}'}],
            [{'glob': 'tests/*.py', 'run': ['{python}'], 'timeout': 0}], {'glob': 'x'}):
    write_settings({'PX': {'tests': bad}})
    rules, error = test_runner.rules('PX')
    assert rules == test_runner.DEFAULT_RULES and 'PX.tests 설정이 잘못됐어요' in error, (bad, error)
subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=repo, check=True)
subprocess.run(['git', 'add', '-A'], cwd=repo, check=True)
subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'init'], cwd=repo, check=True)
issues.create_project(me, 'PX', 'PX', '', str(repo))
parent = issues.create_issue(me, 'PX', '부모')['ref']
task = issues.create_issue(me, 'PX', '작업', parent=parent)['ref']
failed = orchestrate.run_tests(str(repo), 'server_completion', task)
assert failed and 'PX.tests 설정이 잘못됐어요' in failed, failed
print('OK — 잘못된 규칙은 서버 검사 실패')

# 4. 서버 검사가 프로젝트 규칙으로 돌고, 진행 n/N도 그 파일들로 센다.
write_settings({'PX': {'tests': [{'glob': 'checks/*.check.py', 'run': ['{python}', '{file}']}]}})
run_progress.set_phase(task, 'checks', limit=1800)
failed = orchestrate.run_tests(str(repo), 'server_completion', task)
assert failed and 'two.check.py: 종료 코드 3' in failed and 'one.check.py' not in failed and 'test_a.py' not in failed, failed
v = run_progress.view(task)
assert v['short'] == '전체 검사 중 (2/2 · 실패 1)' and '실패: two.check.py' in v['long'], v
run_progress.clear(task)
print('OK — 서버 검사·진행 n/N이 프로젝트 규칙을 따른다')

# 5. 임시 DB를 켠 단계는 띄우는 동안 "준비 중: 임시 DB"로 보이고, 끝나면 준비 표시를 남기지 않는다.
from contextlib import contextmanager  # noqa: E402
from unittest.mock import patch  # noqa: E402
import isolated_pg  # noqa: E402
seen = []


@contextmanager
def fake_cluster(project, stage, label=''):
    seen.append(run_progress.view(task)['short'])
    yield None, None


run_progress.set_phase(task, 'checks', limit=1800)
with patch.object(isolated_pg, 'wanted', return_value={'stages': ['server_completion']}), patch.object(isolated_pg, 'cluster', fake_cluster):
    orchestrate.run_tests(str(repo), 'server_completion', task)
assert seen == ['준비 중: 임시 DB'], seen
assert run_progress.view(task)['short'].startswith('전체 검사 중 (') and '준비 중' not in run_progress.view(task)['long'], run_progress.view(task)   # 통과한 검사는 재사용돼 1/1일 수 있다
run_progress.clear(task)
print('OK — 임시 DB 준비 표시')

# 6. 파일 안 검사 항목 진행(DEV-213) — 출력의 항목 줄을 세어 ⓘ "지금: …"에 지난 항목 수와 마지막 항목을 보인다.
import threading  # noqa: E402
(repo / 'checks' / 'slow.check.py').write_text(
    'import sys, time, pathlib\nprint("OK — 첫 단계", flush=True)\nprint("ok: 둘째 단계", flush=True)\nprint("그냥 출력", flush=True)\n'
    'pathlib.Path(sys.argv[1]).write_text("go")\nwhile not pathlib.Path(sys.argv[2]).exists(): time.sleep(0.05)\n', encoding='utf-8')
flag_dir = Path(tempfile.mkdtemp())
write_settings({'PX': {'tests': [{'glob': 'checks/slow.check.py', 'item': test_runner.SCRIPT_ITEM,
                                  'run': ['{python}', '{file}', str(flag_dir / 'started'), str(flag_dir / 'release')]}]}})
run_progress.set_phase(task, 'checks', limit=1800)
result = {}
worker = threading.Thread(target=lambda: result.update(failed=orchestrate.run_tests(str(repo), 'server_completion', task)))
worker.start()
for _ in range(200):
    long = (run_progress.view(task) or {}).get('long', '')
    if (flag_dir / 'started').exists() and '2개 항목 지남' in long:
        break
    time.sleep(0.05)
assert '지금: slow.check.py · 2개 항목 지남 — 마지막: 둘째 단계' in long, long
(flag_dir / 'release').write_text('go')
worker.join(60)
assert not worker.is_alive() and result['failed'] is None, result
assert '항목 지남' not in run_progress.view(task)['long']   # 끝난 검사는 항목 진행을 지운다
run_progress.clear(task)
# 항목 패턴을 잘못 적으면 규칙 오류다.
write_settings({'PX': {'tests': [{'glob': 'checks/*.py', 'run': ['{python}', '{file}'], 'item': '(unclosed'}]}})
assert 'PX.tests 설정이 잘못됐어요' in test_runner.rules('PX')[1]
print('OK — 파일 안 검사 항목 진행')
