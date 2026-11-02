"""검사 병렬 실행(DEV-200) — 순서·동시성·브라우저 한 자리·예외 전달, 서버 검사의 병렬 결과가 순차와 같은지."""
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
os.environ['NTFY_TOPIC'] = ''
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import db, issues, orchestrate, regression, test_runner  # noqa: E402

db.init()
me = {'kind': 'human', 'name': 'admin'}
tmp = Path(tempfile.mkdtemp())


def files(*names, browser=()):
    out = []
    for n in names:
        p = tmp / f'{n}.py'
        p.write_text('import playwright\n' if n in browser else '"""x"""\n', encoding='utf-8')
        out.append(p)
    return out


# 1. jobs=1은 주어진 순서대로 하나씩 — 지금까지와 같다.
seen = []
fs = files('s1', 's2', 's3')
out = test_runner.run_all(fs, lambda t: seen.append(t.stem) or t.stem.upper(), 1, {'s3.py': 99})
assert seen == ['s1', 's2', 's3'] and {t.stem: v for t, (v, _) in out.items()} == {'s1': 'S1', 's2': 'S2', 's3': 'S3'}
assert all(isinstance(sec, float) for _, sec in out.values())

# 2. 동시에 돈다 — 0.5초짜리 4개를 jobs=4로 돌리면 2초보다 훨씬 빠르다.
start = time.monotonic()
test_runner.run_all(files('p1', 'p2', 'p3', 'p4'), lambda t: time.sleep(0.5), 4)
assert time.monotonic() - start < 1.5, time.monotonic() - start

# 3. 오래 걸린 것부터(처음 보는 것은 맨 앞) — 마지막에 긴 검사가 혼자 남지 않게.
lock, order = threading.Lock(), []
def record(t):
    with lock:
        order.append(t.stem)
    time.sleep(0.2)
test_runner.run_all(files('short', 'long', 'fresh'), record, 2, {'short.py': 1, 'long.py': 50})
assert set(order[:2]) == {'fresh', 'long'} and order[2] == 'short', order

# 4. 브라우저 검사는 한 자리에서 하나씩, 그동안 다른 검사는 같이 돈다.
active = {'browser': 0, 'all': 0, 'max_browser': 0, 'max_all': 0}
def busy(t):
    kind = 'browser' if test_runner.is_browser(t) else 'other'
    with lock:
        active['all'] += 1
        active['max_all'] = max(active['max_all'], active['all'])
        if kind == 'browser':
            active['browser'] += 1
            active['max_browser'] = max(active['max_browser'], active['browser'])
    time.sleep(0.3)
    with lock:
        active['all'] -= 1
        if kind == 'browser':
            active['browser'] -= 1
out = test_runner.run_all(files('ui1', 'ui2', 'ui3', 'n1', 'n2', 'n3', browser=('ui1', 'ui2', 'ui3')), busy, 4)
assert len(out) == 6 and active['max_browser'] == 1 and active['max_all'] > 1, active

# 5. 예외는 호출자에게 그대로 간다(일반·브라우저 자리 모두).
for target in ('e1', 'eb'):
    def boom(t, target=target):
        if t.name.startswith(target):
            raise RuntimeError(target)
    try:
        test_runner.run_all(files('e1', 'eb', 'ok', browser=('eb',)), boom, 3)
        raise AssertionError('예외가 사라짐')
    except RuntimeError as error:
        assert str(error) == target
print('OK — run_all 순서·동시성·오래 걸린 것부터·브라우저 한 자리·예외 전달')


# 6. 서버 검사: 병렬과 순차의 통과/실패·재실행 결과가 같고, 벽시계·파일별 시간이 남는다.
def git(cwd, *args):
    r = subprocess.run(['git', *args], cwd=str(cwd), capture_output=True, text=True, encoding='utf-8')
    assert r.returncode == 0, (args, r.stderr)


repo = Path(tempfile.mkdtemp())
git(repo, 'init', '-q', '-b', 'main')
(repo / 'tests').mkdir()
for name, body in {'test_ok1': 'import time; time.sleep(0.4)', 'test_ok2': 'import time; time.sleep(0.4)',
                   'test_ok3': 'import time; time.sleep(0.4)', 'test_bad': 'import sys; print("깨짐"); sys.exit(3)',
                   'test_ok4': 'import time; time.sleep(0.4)'}.items():
    (repo / 'tests' / f'{name}.py').write_text(f'"""{name}"""\n{body}\n', encoding='utf-8')
git(repo, 'add', '-A')
git(repo, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'init')
issues.create_project(me, 'PX', 'PX', '', str(repo))
assert orchestrate.test_jobs('PX') == 1                                        # 다른 프로젝트 기본은 순차
assert orchestrate.test_jobs('DEV') == min(4, os.cpu_count() or 1)

summary = {}
for jobs in (1, 4):
    ref = issues.create_issue(me, 'PX', f'jobs {jobs}')['ref']                # 이슈마다 재사용 기록이 따로라 매번 실제로 돈다
    with patch.object(orchestrate, 'test_jobs', return_value=jobs):
        failed = orchestrate.run_tests(str(repo), 'server_merge', ref, ['tests/test_ok1.py'])
    record = regression.load_record('regression-selection:' + ref + ':server_merge')
    summary[jobs] = (failed.split('\n')[0], record['results'], sorted(record['durations']), record['wall'])
assert summary[1][:3] == summary[4][:3], summary
assert 'test_bad.py' in summary[4][0] and summary[4][1]['test_bad.py'] == 'failed'
assert list(summary[4][1]) == sorted(summary[4][1])                           # 결과는 파일 순서대로
assert summary[4][3] < summary[1][3], summary                                 # 병렬이 더 빠르다(병합 실패 검사는 2번 더 돈다)
durations = regression.load_record('regression-durations:PX')
assert set(durations) == {'test_ok1.py', 'test_ok2.py', 'test_ok3.py', 'test_ok4.py', 'test_bad.py'}, durations
note = [e['body'] for e in issues.get_issue(ref)['events'] if e['body'].startswith('⏱️ 병합')][-1]
assert note.endswith(orchestrate.slowest_line(regression.load_record('regression-selection:' + ref + ':server_merge')['durations'],
                                              wall=summary[4][3]).split(', ')[-1]), note
assert orchestrate.slowest_line({'a.py': 30, 'b.py': 40}, wall=50).endswith('전체 50초')

# 7. 격리 PG 환경이 있으면 한 클러스터를 같이 쓰므로 순차로 돈다.
seen_jobs = []
real = test_runner.run_all
with patch.object(orchestrate, 'test_jobs', return_value=4), \
        patch.object(test_runner, 'run_all', side_effect=lambda f, one, jobs, timing: seen_jobs.append(jobs) or real(f, one, jobs, timing)):
    orchestrate._run_tests(str(repo), 'server_merge', issues.create_issue(me, 'PX', 'pg')['ref'], ['tests/test_ok1.py'], {'PATH': os.environ['PATH']})
assert seen_jobs == [1], seen_jobs
print('OK — 서버 검사 병렬 결과가 순차와 같고 시간·벽시계가 남으며 격리 PG는 순차')


# 8. 검사가 남긴 고아 자식이 출력 파이프를 붙잡아도 멈추지 않는다(DEV-205).
def alive(pid):
    if os.name == 'nt':
        return str(pid) in subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/NH'], capture_output=True, text=True).stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


spawn = 'import subprocess, sys\nchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])\nprint("child", child.pid, flush=True)\n'
(repo / 'tests' / 'test_orphan.py').write_text(spawn, encoding='utf-8')                       # 자식을 남기고 바로 끝난다
(repo / 'tests' / 'test_slow.py').write_text(spawn + 'import time; time.sleep(120)\n', encoding='utf-8')   # 자식과 함께 시간 제한을 넘긴다
env = {k: v for k, v in os.environ.items()}
with patch.object(test_runner, 'PIPE_GRACE_SEC', 2), patch.object(test_runner, 'TIMEOUT_SEC', 4):
    start = time.monotonic()
    r = test_runner.run(str(repo), repo / 'tests' / 'test_orphan.py', env=env)
    took = time.monotonic() - start
    orphan = int(r.stdout.split()[1])
    # Windows 병렬 검사 부하의 프로세스 시작 시간을 허용하되 자식의 120초 대기는 잡는다.
    assert r.returncode == 0 and took < 10 and '보조 서버' in r.stderr, (r.returncode, took, r.stderr)
    start = time.monotonic()
    r = test_runner.run(str(repo), repo / 'tests' / 'test_slow.py', env=env)
    took = time.monotonic() - start
    child = int(r.stdout.split()[1])
    assert r.returncode == 124 and took < 10 and '시간 제한' in r.stderr, (r.returncode, took, r.stderr)
time.sleep(0.5)
assert not alive(child), '시간 초과 때 검사의 자식까지 끝내야 한다'
if os.name == 'nt':
    subprocess.run(['taskkill', '/F', '/PID', str(orphan)], capture_output=True)
else:
    os.kill(orphan, 9)
(repo / 'tests' / 'test_env.py').write_text('import os\nprint(os.environ.get("DEV_TEST_RUN"), os.environ.get("PYTHONIOENCODING"))\n', encoding='utf-8')
r = test_runner.run(str(repo), repo / 'tests' / 'test_env.py', env=env)
assert r.returncode == 0 and r.stdout.split() == ['1', 'utf-8'], r   # 검사에는 DEV_TEST_RUN을 넘겨 진짜 알림을 막는다
# 브라우저 검사만 시간 제한이 길다(DEV-209) — 병렬 부하에서 test_ui.py가 600초를 넘었다.
ui_check, plain = files('limit_ui', 'limit_plain', browser=('limit_ui',))
assert test_runner.timeout_for(ui_check) == test_runner.BROWSER_TIMEOUT_SEC == 1200
assert test_runner.timeout_for(plain) == test_runner.TIMEOUT_SEC == 600
with patch.object(test_runner, 'PIPE_GRACE_SEC', 2), patch.object(test_runner, 'timeout_for', return_value=3):
    r = test_runner.run(str(repo), repo / 'tests' / 'test_slow.py', env=env)
assert r.returncode == 124 and '시간 제한 3초' in r.stderr, r.stderr   # run이 검사별 제한을 쓴다
print('OK — 고아 자식이 파이프를 붙잡아도·시간 초과여도 정해진 시간 안에 돌아오고, 시간 초과는 자식까지 끝낸다')
