"""Moai 패키지 검사 발견·실패 차단·빌드 설정·모델 호출 전 환경 검사."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

tmp = tempfile.TemporaryDirectory()
os.environ['DEV_DATA_DIR'] = tmp.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import db, execute, orchestrate, regression
db.init()
root = Path(tmp.name, 'project')
tests = root / 'backend/tests'
tests.mkdir(parents=True)
(root / 'backend/__init__.py').touch()
(tests / '__init__.py').touch()
(tests / 'test_auth.py').write_text('raise SystemExit(3)', encoding='utf-8')
assert regression.test_files(root) == [tests / 'test_auth.py']
assert '종료 코드 3' in orchestrate.run_tests(str(root))
(tests / 'test_auth.py').write_text('import unittest\nfrom backend import __name__\nclass TestPackage(unittest.TestCase):\n def test_import(self): self.assertEqual(__name__, "backend")\n', encoding='utf-8')
failed = orchestrate.run_tests(str(root)); assert failed is None, failed
cfg = orchestrate.settings('MO')
assert orchestrate.selected_builds(cfg, ['apps/web/src/auth.tsx'])
assert not orchestrate.selected_builds(cfg, ['docs/note.md'])
with patch.object(execute, 'prompt_for', return_value='prompt'), patch('review.claude_bin', return_value='claude'):
    mo = execute.command_for('MO-13-1', 'MO-13', 'config')
    dev = execute.command_for('DEV-1-1', 'DEV-1', 'config')
assert 'Bash(npm --prefix apps/web run build)' in mo
assert 'Bash(npm --prefix apps/web run build)' not in dev
with patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)):
    try:
        execute.prepare_checks(root, {'project_key': 'MO'})
        raise AssertionError('환경 실패를 모델 호출 전에 차단해야 한다')
    except Exception as error:
        assert '모델 호출 전' in str(error)
tmp.cleanup()
print('Moai check guards passed')
