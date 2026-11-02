"""불안정 검사 대책(DEV-160-3) — 바뀐 검사 3회·병합 실패 재실행 분류·troubleshoot flaky 분류·공통 하네스 문구를 임시 git 저장소로 검사한다."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
os.environ['NTFY_TOPIC'] = ''
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
import db, issues, orchestrate, troubleshoot  # noqa: E402

db.init()
me = {'kind': 'human', 'name': 'admin'}
counter = Path(tempfile.mkdtemp()) / 'count.txt'

FLAKY = ('"""홀수 번째 실행만 실패"""\nimport sys\nfrom pathlib import Path\n'
         f'p = Path({str(counter)!r})\nn = int(p.read_text()) + 1 if p.exists() else 1\np.write_text(str(n))\n'
         'sys.exit(1 if n % 2 else 0)\n')
ALWAYS = '"""늘 실패"""\nimport sys\nsys.exit(1)\n'
PASS = '"""늘 통과"""\n'


def git(cwd, *args):
    r = subprocess.run(['git', *args], cwd=str(cwd), capture_output=True, text=True, encoding='utf-8')
    assert r.returncode == 0, (args, r.stderr)


def make_repo(body):
    repo = Path(tempfile.mkdtemp())
    git(repo, 'init', '-q', '-b', 'main')
    (repo / 'tests').mkdir()
    (repo / 'tests' / 'test_x.py').write_text(body, encoding='utf-8')
    git(repo, 'add', '-A')
    git(repo, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'init')
    return repo


def new_ref(repo, key):
    issues.create_project(me, key, key, '', str(repo))
    return issues.create_issue(me, key, '작업')['ref']


def reset():
    counter.unlink(missing_ok=True)


CHANGED = ['tests/test_x.py']

# 1. 완료 검사 — 바뀐 검사가 섞이면 불안정으로 실패한다
reset()
repo = make_repo(FLAKY)
out = orchestrate.run_tests(str(repo), stage='server_completion', ref=new_ref(repo, 'FA'), changed=CHANGED)
assert out and orchestrate.FLAKY_MARK in out and '3번 중 2번 실패' in out and '기다리기·순서' in out, out
assert counter.read_text() == '4'

# 2. 바뀌지 않은 검사는 재실행 통과하면 완료를 막지 않는다
reset()
out = orchestrate.run_tests(str(repo), stage='server_completion', ref=new_ref(repo, 'FB'), changed=[])
assert out is None and counter.read_text() == '2', out

# 3. 늘 실패하면 코드 실패(불안정 표시 없음), 늘 통과하면 한 번만
repo = make_repo(ALWAYS)
out = orchestrate.run_tests(str(repo), stage='server_completion', ref=new_ref(repo, 'FC'), changed=CHANGED)
assert out and orchestrate.FLAKY_MARK not in out, out
repo = make_repo(PASS)
assert orchestrate.run_tests(str(repo), stage='server_completion', ref=new_ref(repo, 'FD'), changed=CHANGED) is None

# 4. 병합 검사 — 실패한 검사만 2번 더 돌려 섞이면 불안정, 막는다
reset()
repo = make_repo(FLAKY)
out = orchestrate.run_tests(str(repo), stage='server_merge', ref=new_ref(repo, 'FE'))
assert out and orchestrate.FLAKY_MARK in out and '3번 중 2번 실패' in out, out
repo = make_repo(ALWAYS)
out = orchestrate.run_tests(str(repo), stage='server_merge', ref=new_ref(repo, 'FF'))
assert out and orchestrate.FLAKY_MARK not in out, out

# 5. troubleshoot — flaky는 따로 분류되어 자동 복구(code만)에 안 걸린다
assert troubleshoot.classify(out := 'x ' + orchestrate.FLAKY_MARK + ' test_x.py 검사 실패 AssertionError', 'failed')[0] == 'flaky'
assert troubleshoot.classify('검사 실패 AssertionError', 'failed')[0] == 'code'

# 6. 재현 3회와 공통 하네스
import reproduce  # noqa: E402
assert reproduce.REPEAT == 3
common = (ROOT / 'harness' / 'COMMON.md').read_text(encoding='utf-8')
assert 'expect_response' in common and 'wait_for_function' in common and 'id로 찾는다' in common

print('test_flaky_checks OK')
