"""선택 실행: 실제 Windows Codex sandbox의 읽기·쓰기 경계를 검사한다(모델/API 호출 없음)."""
import os, sys, subprocess, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'server'))
import review

if os.name != 'nt':
    raise SystemExit('Windows only')

with tempfile.TemporaryDirectory(prefix='dev-codex-sandbox-') as task_tmp:
    root = Path(task_tmp).resolve()
    assert root.is_relative_to(Path(tempfile.gettempdir()).resolve())
    workspace = root / 'workspace'
    workspace.mkdir()
    (workspace / 'input.txt').write_text('readable', 'utf-8')
    script = workspace / 'probe.py'
    script.write_text('''from pathlib import Path
import sys
assert Path('input.txt').read_text('utf-8') == 'readable'
mode = sys.argv[1]
if mode == 'workspace-write':
    Path('inside.txt').write_text('writable', 'utf-8')
else:
    try:
        Path('readonly.txt').write_text('must fail', 'utf-8')
    except PermissionError:
        pass
    else:
        raise AssertionError('read-only permitted a write')
try:
    Path('../outside.txt').write_text('must fail', 'utf-8')
except PermissionError:
    pass
else:
    raise AssertionError('outside write permitted')
print('OK actual Windows sandbox: ' + mode)
''', 'utf-8')
    built = review.codex_command('probe', [], 'workspace-write')
    launcher = built[:built.index('exec')]
    native = next(x for x in built if x.startswith('windows.sandbox='))
    for mode in ('workspace-write', 'read-only'):
        profile = ':workspace' if mode == 'workspace-write' else ':read-only'
        cmd = launcher + ['sandbox', '-P', profile, '-C', str(workspace), '-c', native,
                          '-c', 'sandbox_mode="' + mode + '"',
                          '-c', 'sandbox_workspace_write.network_access=false',
                          sys.executable, str(script), mode]
        result = subprocess.run(cmd, cwd=workspace, text=True, encoding='utf-8', errors='replace', capture_output=True)
        print(result.stdout.strip())
        if result.returncode:
            print(result.stderr.strip())
            raise SystemExit(result.returncode)
