"""검사 결과 재사용(DEV-160-1) — 커밋 전 완료 검사의 지문이 커밋 뒤 병합 사본에서도 맞아 재사용되고, 안 될 때 사유가 남는다."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
os.environ['NTFY_TOPIC'] = ''
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import db, issues, orchestrate, regression  # noqa: E402

db.init()
me = {'kind': 'human', 'name': 'admin'}


def git(cwd, *args):
    r = subprocess.run(['git', *args], cwd=str(cwd), capture_output=True, text=True, encoding='utf-8')
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout


def commit_all(cwd, msg):
    git(cwd, 'add', '-A')
    git(cwd, 'reset', '-q', '--', '.ui-captures', 'tests/shots')
    git(cwd, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', msg)


def make_repo():
    repo = Path(tempfile.mkdtemp())
    git(repo, 'init', '-q', '-b', 'main')
    (repo / 'tests').mkdir()
    (repo / 'tests' / 'test_a.py').write_text('"""a"""\n', encoding='utf-8')
    (repo / 'tests' / 'test_b.py').write_text('"""b"""\n', encoding='utf-8')
    (repo / 'app.py').write_text('x = 1\n', encoding='utf-8')
    commit_all(repo, 'init')
    return repo


def merge_copy(repo, branch):
    copy = Path(tempfile.mkdtemp())
    git(copy, 'clone', '-q', str(repo), '.')
    git(copy, 'fetch', '-q', 'origin', branch)
    git(copy, '-c', 'user.name=t', '-c', 'user.email=t@t', 'merge', '--no-ff', '-q', '-m', 'merge', 'FETCH_HEAD')
    return copy


def new_ref(repo, key):
    issues.create_project(me, key, key, '', str(repo))
    return issues.create_issue(me, key, '작업')['ref']


CHANGED = ['tests/test_a.py']
summary = lambda ref, stage: regression.load_record('regression-selection:' + ref + ':' + stage)  # noqa: E731

# 1. Codex 흐름 — 커밋 안 된 변경으로 완료 검사 → 같은 규칙으로 커밋 → 병합 사본에서 전부 reused
repo = make_repo()
git(repo, 'checkout', '-q', '-b', 'relay/x')
(repo / 'app.py').write_text('x = 2\n', encoding='utf-8')
(repo / 'new_mod.py').write_text('y = 1\n', encoding='utf-8')                 # 추적 안 되는 새 코드 파일
(repo / '.ui-captures').mkdir()
(repo / '.ui-captures' / 'a.png').write_bytes(b'png')
before = git(repo, 'status', '--porcelain', '--untracked-files=all')
ref = new_ref(repo, 'CA')
assert orchestrate.run_tests(str(repo), 'server_completion', ref, CHANGED) is None
assert git(repo, 'status', '--porcelain', '--untracked-files=all') == before     # 작업 폴더·인덱스 불변
assert summary(ref, 'server_completion')['cache']['used'] is False
commit_all(repo, 'task')
git(repo, 'checkout', '-q', 'main')
copy = merge_copy(repo, 'relay/x')
assert orchestrate.run_tests(str(copy), 'server_merge', ref, CHANGED) is None
rec = summary(ref, 'server_merge')
assert set(rec['results'].values()) == {'reused'}, rec
assert rec['cache']['used'] is True

# 2. Claude 흐름 — 먼저 커밋한 폴더도 그대로 재사용
repo = make_repo()
git(repo, 'checkout', '-q', '-b', 'relay/y')
(repo / 'app.py').write_text('x = 3\n', encoding='utf-8')
commit_all(repo, 'task')
ref = new_ref(repo, 'CB')
assert orchestrate.run_tests(str(repo), 'server_completion', ref, CHANGED) is None
git(repo, 'checkout', '-q', 'main')
copy = merge_copy(repo, 'relay/y')
assert orchestrate.run_tests(str(copy), 'server_merge', ref, CHANGED) is None
assert set(summary(ref, 'server_merge')['results'].values()) == {'reused'}

# 3. 검사 뒤 파일을 더 바꾸고 커밋하면 재사용하지 않는다
repo = make_repo()
git(repo, 'checkout', '-q', '-b', 'relay/z')
(repo / 'app.py').write_text('x = 4\n', encoding='utf-8')
ref = new_ref(repo, 'CC')
assert orchestrate.run_tests(str(repo), 'server_completion', ref, CHANGED) is None
(repo / 'app.py').write_text('x = 5\n', encoding='utf-8')
commit_all(repo, 'task')
git(repo, 'checkout', '-q', 'main')
copy = merge_copy(repo, 'relay/z')
assert orchestrate.run_tests(str(copy), 'server_merge', ref, CHANGED) is None
rec = summary(ref, 'server_merge')
assert 'reused' not in rec['results'].values() and rec['cache']['used'] is False
assert '트리가 다름' in rec['cache']['reason'], rec['cache']

# 4. main이 바뀌어 병합 트리가 다르면 사유는 "트리가 다름"이고 경고 댓글은 없다
repo = make_repo()
git(repo, 'checkout', '-q', '-b', 'relay/w')
(repo / 'app.py').write_text('x = 6\n', encoding='utf-8')
ref = new_ref(repo, 'CD')
assert orchestrate.run_tests(str(repo), 'server_completion', ref, CHANGED) is None
commit_all(repo, 'task')
git(repo, 'checkout', '-q', 'main')
(repo / 'other.py').write_text('o = 1\n', encoding='utf-8')
commit_all(repo, 'main moved')
copy = merge_copy(repo, 'relay/w')
assert orchestrate.run_tests(str(copy), 'server_merge', ref, CHANGED) is None
rec = summary(ref, 'server_merge')
assert '트리가 다름' in rec['cache']['reason'], rec['cache']
with db.connect() as c:
    notes = [r['body'] for r in c.execute("SELECT body FROM events WHERE kind='comment'")]
assert not any('재사용하지 못했어요' in b for b in notes), notes

# 5. 캡처 경로의 파일은 지문에 영향 없음
repo = make_repo()
base = regression.work_tree_hash(str(repo))
(repo / '.ui-captures').mkdir()
(repo / '.ui-captures' / 'a.png').write_bytes(b'1')
(repo / 'tests' / 'shots').mkdir()
(repo / 'tests' / 'shots' / 'b.png').write_bytes(b'2')
assert regression.work_tree_hash(str(repo)) == base
assert regression.tree_identity(str(repo)) is not None
assert regression.tree_identity(str(repo / 'missing')) is None
print('ok')
