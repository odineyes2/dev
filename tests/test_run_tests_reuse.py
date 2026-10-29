"""수동 검사 명령의 의존 기반 재사용(DEV-200) — 임시 저장소에 scripts/run_tests.py를 복사해 실제로 돌린다.
읽은 파일이 그대로면 재사용, 바뀐 파일을 읽은 검사만 다시, 실패·외부 프로그램·파일 목록 변경·--all·러너 변경은 보수적으로 다시."""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
repo = Path(tempfile.mkdtemp())


def git(*args):
    r = subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *args], cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0, (args, r.stderr)


for rel in ('scripts/run_tests.py', 'scripts/test_trace/sitecustomize.py', 'server/test_runner.py'):
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / rel, repo / rel)
(repo / '.gitignore').write_text('.test-cache/\n__pycache__/\n')
(repo / 'pkg').mkdir()
(repo / 'pkg' / '__init__.py').write_text('')
(repo / 'pkg' / 'mod_a.py').write_text('A = 1\n')
(repo / 'data.txt').write_text('b\n')
tests = {
    'test_a': 'import sys; sys.path.insert(0, ".")\nfrom pkg import mod_a\nassert mod_a.A == 1\n',                     # import로 읽음
    'test_b': 'assert open("data.txt").read() == "b\\n"\n',                                                          # open으로 읽음
    'test_c': 'import subprocess; subprocess.run(["git", "status"], check=True, capture_output=True)\n',             # 저장소에서 외부 프로그램
    'test_d': 'import subprocess, sys, os\nr = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, \'.\'); import pkg.mod_a"], env={**os.environ})\nassert r.returncode == 0\n',  # 하위 Python이 읽음
    'test_f': 'raise SystemExit(1)\n',                                                                                # 실패
}
(repo / 'tests').mkdir()
for name, body in tests.items():
    (repo / 'tests' / f'{name}.py').write_text(f'"""{name}"""\n{body}', encoding='utf-8')
git('init', '-q', '-b', 'main')
git('add', '-A')
git('commit', '-q', '-m', 'init')


def run(*args):
    r = subprocess.run([sys.executable, 'scripts/run_tests.py', '-j', '2', *args], cwd=repo, capture_output=True, text=True, encoding='utf-8', errors='replace')
    states = {line.split()[1][:-3]: line.split()[0] for line in r.stdout.splitlines() if line[:6].strip() in ('PASS', 'FAIL', 'REUSED')}
    return r.returncode, states


code, first = run()
assert code == 1 and first == {'test_a': 'PASS', 'test_b': 'PASS', 'test_c': 'PASS', 'test_d': 'PASS', 'test_f': 'FAIL'}, first
assert (repo / '.test-cache' / 'logs' / 'test_f.log').is_file()
_, again = run()
assert again == {'test_a': 'REUSED', 'test_b': 'REUSED', 'test_c': 'REUSED', 'test_d': 'REUSED', 'test_f': 'FAIL'}, again   # 실패는 재사용 안 함

(repo / 'pkg' / 'mod_a.py').write_text('A = 1  # 바뀜\n')                       # mod_a를 읽은 검사(+ 무엇을 읽었는지 모르는 test_c)만 다시
_, changed = run()
assert changed == {'test_a': 'PASS', 'test_b': 'REUSED', 'test_c': 'PASS', 'test_d': 'PASS', 'test_f': 'FAIL'}, changed
(repo / 'data.txt').write_text('b\n')                                            # 내용이 같으면 그대로 재사용
_, same = run()
assert same['test_b'] == 'REUSED' and same['test_a'] == 'REUSED', same
(repo / 'tests' / 'test_b.py').write_text('"""test_b 고침"""\nassert open("data.txt").read() == "b\\n"\n', encoding='utf-8')
_, tests_only = run()                                                            # 다른 검사 파일만 바뀌면 무엇을 읽었는지 모르는 test_c도 그대로
assert tests_only == {'test_a': 'REUSED', 'test_b': 'PASS', 'test_c': 'REUSED', 'test_d': 'REUSED', 'test_f': 'FAIL'}, tests_only

(repo / 'pkg' / 'new.py').write_text('')                                         # 파일 목록이 바뀌면 전부 다시
_, listed = run()
assert all(v != 'REUSED' for v in listed.values()), listed
_, everything = run('--all')                                                     # --all은 재사용 없음
assert all(v != 'REUSED' for v in everything.values()), everything
_, named = run('test_b')                                                         # 이름 일부로 고르기
assert named == {'test_b': 'REUSED'}, named
runner = repo / 'scripts' / 'test_trace' / 'sitecustomize.py'                    # 러너가 바뀌면 전부 다시
runner.write_text(runner.read_text(encoding='utf-8') + '\n', encoding='utf-8')
_, rerun = run()
assert all(v != 'REUSED' for v in rerun.values()), rerun
print('OK — 수동 검사 의존 기반 재사용·보수 규칙')
