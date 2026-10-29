"""검사 파일 여러 개를 동시에 돌린다 — 서버 완료·병합 검사와 수동 명령(scripts/run_tests.py)이 같이 쓴다(DEV-200). DB를 쓰지 않는다."""
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TIMEOUT_SEC = 600


def test_files(repo):
    """루트 검사와 Moai의 패키지 검사를 모두 찾는다."""
    return sorted({path for folder in ('tests', 'backend/tests') for pattern in ('test_*.py', 'test_*.js')
                   for path in Path(repo, folder).glob(pattern) if pattern.endswith('py') or folder == 'tests'})


def command(repo, t):
    """검사 하나를 실행할 명령 — JS는 node:test, Moai 패키지 검사는 unittest 모듈, 나머지는 파일 그대로."""
    relative = (t.relative_to(Path(repo)) if t.is_relative_to(Path(repo)) else t).as_posix()
    return (['node', '--test', str(t)] if relative.endswith('.js') else
            [sys.executable, '-m', 'unittest', relative[:-3].replace('/', '.')]
            if relative.startswith('backend/tests/') else [sys.executable, str(t)])


def run(repo, t, args=(), env=None):
    """검사 하나를 시간 제한과 출력 보존 규칙으로 실행한다. env는 이미 비밀값을 뺀 환경이다."""
    try:
        return subprocess.run(command(repo, t) + list(args), cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env={**env, "PYTHONIOENCODING": "utf-8", "PYTHONFAULTHANDLER": "1", "DEV_TEST_RUN": "1"}, timeout=TIMEOUT_SEC,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)   # 테스트가 진짜 ntfy로 알림을 보내지 않게
    except subprocess.TimeoutExpired as error:
        def decoded(value):
            return value.decode('utf-8', 'replace') if isinstance(value, bytes) else value or ''
        return subprocess.CompletedProcess(error.cmd, 124, decoded(error.stdout), decoded(error.stderr) + f'\n검사 시간 제한 {TIMEOUT_SEC}초 초과(표시용 종료 코드 124)')


def is_browser(t) -> bool:
    """브라우저(Playwright)를 띄우는 검사 — 메모리를 많이 써서 동시에 하나만 돌린다."""
    try:
        return t.suffix == '.py' and 'playwright' in t.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return False


def run_all(files, run_one, jobs=1, durations=None) -> dict:
    """run_one(검사)을 files마다 한 번 돌려 {검사: (결과, 초)}를 돌려준다.
    jobs=1이면 주어진 순서대로 하나씩(지금까지와 같다). 그 이상이면 지난 시간(durations, 이름별 초)이 긴 것부터 —
    처음 보는 검사도 먼저 — 동시에 jobs개까지 돌리되, 브라우저 검사는 그중 한 자리에서 하나씩 차례로 돈다."""
    files = list(files)
    results = {}

    def timed(t):
        start = time.monotonic()
        value = run_one(t)
        results[t] = (value, round(time.monotonic() - start, 1))

    if int(jobs) <= 1 or len(files) <= 1:
        for t in files:
            timed(t)
        return results
    known = durations or {}
    longest = sorted(files, key=lambda t: (-known.get(t.name, float('inf')), t.name))
    browser = [t for t in longest if is_browser(t)]
    rest = [t for t in longest if t not in browser]
    errors = []

    def lane():
        try:
            for t in browser:
                timed(t)
        except BaseException as error:   # 예외는 호출자에게 그대로 넘긴다
            errors.append(error)
    thread = threading.Thread(target=lane, daemon=True) if browser else None
    if thread:
        thread.start()
    with ThreadPoolExecutor(max(1, int(jobs) - (1 if browser else 0))) as pool:
        for future in [pool.submit(timed, t) for t in rest]:
            future.result()
    if thread:
        thread.join()
    if errors:
        raise errors[0]
    return results
