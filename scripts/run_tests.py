"""수동 전체 검사(DEV-200) — 서버와 같은 명령·병렬 규칙으로 돌리고, 검사가 읽은 파일이 그대로면 지난 통과를 재사용한다.
사용: python scripts/run_tests.py [-j N] [--all] [이름 일부 ...]   (예: run_tests.py -j 2 mcp restart)
재사용 규칙(보수적): 실패·JS·기록 없음은 항상 다시 / 저장소 파일 목록(추가·삭제)이나 러너·Python이 바뀌면 전부 다시 /
무엇을 읽었는지 모르는 검사(저장소에서 외부 프로그램 실행 등)는 저장소 내용이 조금이라도 바뀌면 다시. 결과·로그는 .test-cache/."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
import test_runner  # noqa: E402  (DB를 쓰지 않는 모듈만 쓴다)

TRACE_DIR = Path(__file__).resolve().parent / 'test_trace'
SECRET_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GIT_ASKPASS", "SSH_ASKPASS", "NTFY_TOPIC", "NTFY_TOKEN")   # execute.SECRET_ENV와 같게 둔다


def listed(repo):
    """저장소의 파일 목록(추적 + 무시되지 않은 새 파일). git이 없으면 None — 그때는 재사용하지 않는다."""
    r = subprocess.run(['git', 'ls-files', '-co', '--exclude-standard', '-z'], cwd=repo, capture_output=True)
    return sorted(p for p in r.stdout.decode('utf-8', 'replace').split('\0') if p) if r.returncode == 0 else None


def digest(repo, rel, memo):
    if rel not in memo:
        try:
            memo[rel] = hashlib.sha256((repo / rel).read_bytes()).hexdigest()
        except OSError:
            memo[rel] = None
    return memo[rel]


def runner_stamp():
    parts = [sys.version, sys.executable] + [hashlib.sha256(p.read_bytes()).hexdigest()
                                             for p in (Path(__file__), TRACE_DIR / 'sitecustomize.py', Path(test_runner.__file__))]
    return hashlib.sha256('\n'.join(parts).encode()).hexdigest()


def other_test(rel):
    """검사 파일(tests/test_*, backend/tests/test_*) — 다른 검사의 하위 서버가 읽을 일이 없는 파일."""
    folder, _, name = rel.rpartition('/')
    return folder in ('tests', 'backend/tests') and name.startswith('test_')


def reusable(entry, t, files, everything, stamp, repo, memo):
    """지난 기록으로 이 검사를 건너뛸 수 있으면 그 이유, 없으면 None."""
    if not entry or entry.get('status') != 'passed' or entry.get('runner') != stamp or t.suffix != '.py':
        return None
    if entry.get('listing') != hashlib.sha256('\n'.join(files).encode()).hexdigest():
        return None
    if entry.get('whole'):
        own = t.relative_to(repo).as_posix()
        return 'reused' if entry.get('tree') == everything(own, entry.get('own_reads') or ()) else None
    reads = entry.get('reads') or {}
    if not reads or any(digest(repo, rel, memo) != sha for rel, sha in reads.items()):
        return None
    return 'reused'


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('-j', '--jobs', type=int, default=min(4, os.cpu_count() or 1), help='동시에 돌릴 검사 수(1이면 순차)')
    parser.add_argument('--all', action='store_true', help='재사용 없이 전부 돌린다')
    parser.add_argument('names', nargs='*', help='파일 이름에 이 글자가 들어간 검사만')
    opts = parser.parse_args()
    repo = ROOT
    cache_dir = repo / '.test-cache'
    (cache_dir / 'logs').mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / 'results.json'
    try:
        cache = json.loads(cache_file.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        cache = {}
    files = listed(repo)
    stamp = runner_stamp()
    memo = {}

    def everything(own, reads=()):
        """무엇을 읽었는지 모르는 검사용 저장소 내용 — 다른 검사 파일(tests/test_*)은 빼되, 이 검사가 직접 읽은 것은 넣는다."""
        if not files:
            return None
        keep = set(reads) | {own}
        return hashlib.sha256('\n'.join(f'{f} {digest(repo, f, memo)}' for f in files
                                        if f in keep or not other_test(f)).encode()).hexdigest()

    tests = [t for t in test_runner.test_files(repo) if not opts.names or any(n in t.name for n in opts.names)]
    results, todo = {}, []
    for t in tests:
        if not opts.all and files is not None and reusable(cache.get(t.name), t, files, everything, stamp, repo, memo):
            results[t.name] = ('REUSED', 0.0)
        else:
            todo.append(t)
    trace_tmp = Path(tempfile.mkdtemp(prefix='dev-test-trace-'))
    base_env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}

    def one(t):
        trace = trace_tmp / (t.name + '.trace')
        env = {**base_env, 'DEV_TEST_TRACE': str(trace), 'DEV_TEST_TRACE_ROOT': str(repo),
               'PYTHONPATH': os.pathsep.join([str(TRACE_DIR)] + ([base_env['PYTHONPATH']] if base_env.get('PYTHONPATH') else []))}
        r = test_runner.run(str(repo), t, env=env)
        lines = trace.read_text(encoding='utf-8', errors='replace').splitlines() if trace.exists() else []
        return r, lines

    durations = {k: v.get('seconds', 0) for k, v in cache.items() if isinstance(v, dict)}
    started = time.monotonic()
    ran = test_runner.run_all(todo, one, opts.jobs, durations)
    wall = time.monotonic() - started
    listing = hashlib.sha256('\n'.join(files).encode()).hexdigest() if files is not None else None
    failed = []
    for t in todo:
        (r, lines), seconds = ran[t]
        ok = r.returncode == 0
        results[t.name] = ('PASS' if ok else 'FAIL', seconds)
        log = cache_dir / 'logs' / f'{t.stem}.log'
        if not ok:
            log.write_text(f'검사: {t}\n종료 코드: {r.returncode}\n\n--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}', encoding='utf-8')
            failed.append((t, r, log))
        reads = sorted({line[2:] for line in lines if line.startswith('R ')} | {t.relative_to(repo).as_posix()})
        whole = any(line.startswith('W ') for line in lines) or not lines
        own_reads = sorted(r for r in reads if other_test(r))
        cache[t.name] = {'status': 'passed' if ok else 'failed', 'seconds': seconds, 'runner': stamp, 'listing': listing,
                         'whole': whole, 'own_reads': own_reads if whole else [],
                         'tree': everything(t.relative_to(repo).as_posix(), own_reads) if whole and ok else None,
                         'reads': {rel: digest(repo, rel, memo) for rel in reads if rel in set(files or ())} if ok and not whole else {}}
    cache_file.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding='utf-8')
    for name, (state, seconds) in sorted(results.items()):
        print(f'{state:6} {name}' + (f' {seconds:.0f}s' if state != 'REUSED' else ''))
    reused = sum(1 for s, _ in results.values() if s == 'REUSED')
    print(f'\n{len(results)}개 중 실행 {len(todo)} · 재사용 {reused} · 실패 {len(failed)} — 벽시계 {wall / 60:.1f}분 (동시 {opts.jobs})')
    for t, r, log in failed:
        print(f'\n--- {t.name} 실패(종료 코드 {r.returncode}) 전체 로그: {log}\n' + ((r.stdout + r.stderr).strip()[-1500:] or '출력 없음'))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
