# DEV-258 임시 검사 — 커밋 경계·계획 승인·검사 보관을 확인한다.
import os
import sys
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
with tempfile.TemporaryDirectory() as tmp:
    os.environ['DEV_DATA_DIR'] = tmp
    os.environ['DEV_DB_PATH'] = str(Path(tmp) / 'dev.db')
    import execute as ex
    import task_checks as tc
    repo = Path(tmp) / 'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)
    git('init', '-b', 'main')
    git('config', 'user.email', 'check@example.invalid')
    git('config', 'user.name', 'Check')
    (repo / 'tests').mkdir()
    (repo / 'tests/old.py').write_text('old')
    git('add', '.')
    git('commit', '-m', 'base')
    git('checkout', '-b', 'task')
    (repo / 'tests/old.py').unlink()
    (repo / 'tests/test_x.py').write_text('x')
    (repo / 'tests/guard').mkdir()
    (repo / 'tests/guard/test_y.py').write_text('y')
    git('add', '.')
    git('commit', '-m', 'checks')
    with patch.object(ex, 'BASE_BRANCH', 'main'):
        assert ex.unapproved_checks({'body': '**바꿀 파일**: tests/guard/test_y.py'}, repo) == ['tests/test_x.py']
        assert set(ex.unapproved_checks({'body': ''}, repo)) == {'tests/test_x.py', 'tests/guard/test_y.py'}
        checks = repo / '.dev-checks'
        checks.mkdir()
        (checks / 'check.py').write_bytes(b'print(1)\n')
        git('add', '.')
        git('commit', '-m', 'temporary')
        try:
            ex.reject_capture_changes(repo, 'main...HEAD')
        except ex.issues.StoreError:
            pass
        else:
            raise AssertionError('temporary commit accepted')
    def plan(path, extra=''):
        return f'## Tasks\n1. x | 파일: {path} | 선행: 없음\n## 검사\n- 1: 없음 — 확인\n' + extra
    for path in ('tests/test_x.py', '.dev-checks/check.py', 'tests/guard/test_y.py'):
        try:
            tc.validate_plan(plan(path))
        except tc.CheckFormatError:
            pass
        else:
            raise AssertionError(path)
    tc.validate_plan(plan('tests/guard/test_y.py', '## 지킴이 변경\n- tests/guard/test_y.py: 추가 — 유출 방지\n'))
    tc.validate_plan(plan('server/a.py'))
    rows = tc.stash_scripts('DEV-258-1', 2, repo)
    assert rows[0][0] == 'check.py' and rows[0][2] == 9
    dest = Path(tmp) / 'restore'
    tc.restore_scripts('DEV-258-1', dest)
    assert (dest / '.dev-checks/check.py').read_bytes() == b'print(1)\n'
    (dest / '.dev-checks/check.py').write_text('preserved')
    tc.restore_scripts('DEV-258-1', dest)
    assert (dest / '.dev-checks/check.py').read_text() == 'preserved'
    issue = {'ref': 'DEV-258-1'}
    with patch.object(tc.issues, 'get_issue', return_value=issue), patch.object(tc, 'run_keys', return_value=None), patch.object(tc, 'selected', return_value=[]), patch.object(tc, 'run_all', return_value=('ok', True)), patch.object(tc.issues, 'add_comment') as comment:
        assert tc.verify('DEV-258-1', repo, 3) is None
        assert '임시 검사 보관' in comment.call_args.args[2]
print('DEV-258 checks passed')
