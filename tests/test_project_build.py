"""격리 빌드와 실패 시 운영 산출물·데이터 보존을 검사한다."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import project_build as b


def git(repo, *args):
    r = subprocess.run(['git', '-c', 'user.name=test', '-c', 'user.email=test@local', *args], cwd=repo, env=dict(os.environ), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def fails(phase, fn):
    try:
        fn()
    except b.BuildError as error:
        assert error.phase == phase, (error.phase, str(error))
    else:
        raise AssertionError('실패해야 한다: ' + phase)


with tempfile.TemporaryDirectory() as temp:
    repo = Path(temp) / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'main')
    (repo / '.gitignore').write_text('out/\ndata/\n')
    (repo / 'source').write_text('source')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'initial')
    sha = git(repo, 'rev-parse', 'HEAD')
    (repo / 'out').mkdir()
    (repo / 'out/index.html').write_text('old')
    (repo / 'data').mkdir()
    (repo / 'data/live.db').write_bytes(b'precious database')
    preserved = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (repo / 'source', repo / 'data/live.db')}
    spec = {'cwd': '.', 'run': [sys.executable, '-c', "from pathlib import Path; p=Path('out'); p.mkdir(); (p/'index.html').write_text('new')"], 'out': 'out', 'expect': ['index.html']}
    def build(entry=spec):
        return b.build(repo, sha, [entry], 'TEST', Path(temp) / 'staging')
    for out in ('.', '../outside', 'source', 'notignored', 'data', 'C:/outside'):
        fails('config', lambda out=out: b.declarations(repo, [dict(spec, out=out)]))
    fails('config', lambda: b.declarations(repo, [dict(spec, run='npm run build')]))
    fails('config', lambda: b.declarations(repo, [dict(spec, timeout=0)]))
    fails('install', lambda: build(dict(spec, install=[sys.executable, '-c', 'raise SystemExit(2)'])))
    fails('build', lambda: build(dict(spec, run=[sys.executable, '-c', 'raise SystemExit(3)'])))
    fails('build', lambda: build(dict(spec, run=[sys.executable, '-c', 'import time; time.sleep(2)'], timeout=.01)))
    fails('expect', lambda: build(dict(spec, expect=['missing.html'])))
    empty_environment = [k for k, v in os.environ.items() if v == '']
    with patch.dict(os.environ, {'MOAI_SECRET': 'sensitive-value', 'NEXT_PUBLIC_VALUE': 'sensitive-public', 'DATABASE_URL': 'sensitive-db'}):
        assert not any(k in b.environment() for k in ('MOAI_SECRET', 'NEXT_PUBLIC_VALUE', 'DATABASE_URL'))
        fails('secret', lambda: build(dict(spec, run=[sys.executable, '-c', "from pathlib import Path; p=Path('out'); p.mkdir(); (p/'index.html').write_text('sensitive-value')"])))
        clean = build(dict(spec, run=[sys.executable, '-c', "import os; assert 'MOAI_SECRET' not in os.environ; from pathlib import Path; p=Path('out'); p.mkdir(); (p/'index.html').write_text('new')"]))
    for key in empty_environment:
        os.environ[key] = ''
    assert (repo / 'out/index.html').read_text() == 'old'
    with patch.object(b, '_rename', side_effect=OSError('locked')):
        fails('swap', lambda: b.deploy(repo, clean))
    assert (repo / 'out/index.html').read_text() == 'old'
    original = b._rename
    def fail_incoming(source, target):
        if '.incoming-' in source.name:
            raise OSError('locked incoming')
        return original(source, target)
    with patch.object(b, '_rename', side_effect=fail_incoming):
        fails('swap', lambda: b.deploy(repo, clean))
    assert (repo / 'out/index.html').read_text() == 'old'
    class Interrupted(BaseException):
        pass
    def interrupt_swap(source, target):
        original(source, target)
        if target.name == 'out.prev':
            raise Interrupted()
    with patch.object(b, '_rename', side_effect=interrupt_swap):
        try:
            b.deploy(repo, clean)
        except Interrupted:
            pass
        else:
            raise AssertionError('교체 도중 중단해야 한다')
    assert not (repo / 'out').exists()
    assert (repo / 'out.prev/index.html').read_text() == 'old'
    b.deploy(repo, clean)
    b.deploy(repo, clean)
    assert (repo / 'out/index.html').read_text() == 'new'
    assert json.loads((repo / 'out/dev-release.json').read_text())['revision'] == sha
    b.restore(repo, clean)
    b.restore(repo, clean)
    assert (repo / 'out/index.html').read_text() == 'old'
    assert git(repo, 'rev-parse', 'HEAD') == sha
    assert len(git(repo, 'worktree', 'list', '--porcelain').split('worktree ')) == 2
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in preserved.items())
    import shutil
    shutil.rmtree(repo / 'out')
    b.deploy(repo, clean)
    b.restore(repo, clean)
    assert not (repo / 'out').exists()
print('project build checks passed')
