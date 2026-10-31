"""검사 파일 여러 개를 동시에 돌린다 — 서버 완료·병합 검사와 수동 명령(scripts/run_tests.py)이 같이 쓴다(DEV-200). DB를 쓰지 않는다."""
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TIMEOUT_SEC = 600
BROWSER_TIMEOUT_SEC = 1200   # 브라우저 검사는 병렬 부하에서 600초를 넘는다(test_ui.py 혼자 419초, DEV-209)
PIPE_GRACE_SEC = 30   # 검사가 끝난 뒤 출력 파이프가 닫히기를 기다리는 시간


# 검사 파일을 찾고 돌리는 규칙(DEV-212). 프로젝트가 orchestrate.json의 `tests`에 적지 않으면 이 기본값을 쓴다.
# glob은 저장소 기준 패턴, run은 명령 — {python}=dev의 Python, {file}=검사 파일 절대 경로, {module}=점으로 이은 모듈 이름.
# 선택: timeout(초), browser(true면 브라우저 검사 — 한 번에 하나, 제한 BROWSER_TIMEOUT_SEC).
DEFAULT_RULES = [
    {'glob': 'tests/test_*.py', 'run': ['{python}', '{file}']},
    {'glob': 'tests/test_*.js', 'run': ['node', '--test', '{file}']},
    {'glob': 'backend/tests/test_*.py', 'run': ['{python}', '-m', 'unittest', '{module}']},   # Moai 패키지 검사
]


class TestFile(type(Path())):
    """찾은 검사 파일 — 어떤 규칙으로 찾았는지(rule)를 같이 들고 다녀 실행할 때 같은 규칙을 쓴다."""
    rule = None


def rules(project_key=None) -> tuple[list[dict], str]:
    """(규칙 목록, 설정 오류 문구). 설정이 없으면 기본값, 잘못됐으면 기본값과 오류를 준다 — 서버 검사는 오류를 실패로 알린다."""
    if not project_key:
        return DEFAULT_RULES, ''
    import config
    try:
        cfg = (json.loads((config.DATA_DIR / 'orchestrate.json').read_text(encoding='utf-8')).get(project_key) or {}).get('tests')
    except (OSError, ValueError):
        return DEFAULT_RULES, ''
    if cfg is None:
        return DEFAULT_RULES, ''
    ok = isinstance(cfg, list) and cfg and all(
        isinstance(r, dict) and isinstance(r.get('glob'), str) and r['glob'] and not Path(r['glob']).is_absolute() and '..' not in Path(r['glob']).parts
        and isinstance(r.get('run'), list) and r['run'] and all(isinstance(x, str) for x in r['run'])
        and isinstance(r.get('timeout', 1), int) and r.get('timeout', 1) > 0 for r in cfg)
    if not ok:
        return DEFAULT_RULES, (f'orchestrate.json의 {project_key}.tests 설정이 잘못됐어요 — '
                               '[{"glob": "tests/test_*.py", "run": ["{python}", "{file}"]}] 모양이어야 해요(glob은 저장소 안 상대 경로).')
    return cfg, ''


def test_files(repo, project_key=None):
    """프로젝트 규칙(없으면 기본값 — 루트 검사와 Moai의 패키지 검사)으로 검사 파일을 찾는다. 같은 파일은 앞 규칙이 갖는다."""
    found = {}
    for rule in rules(project_key)[0]:
        for path in sorted(Path(repo).glob(rule['glob'])):
            if path not in found:
                t = TestFile(path)
                t.rule = rule
                found[path] = t
    return sorted(found.values())


def rule_for(repo, t):
    """검사 파일을 찾은 규칙. 규칙 없이 받은 경로(재현 실행 등)는 기본 규칙에서 맞는 것을 고른다."""
    if getattr(t, 'rule', None):
        return t.rule
    relative = (Path(t).relative_to(Path(repo)) if Path(t).is_relative_to(Path(repo)) else Path(t)).as_posix()
    return next((r for r in DEFAULT_RULES if Path(relative).full_match(r['glob'])), DEFAULT_RULES[0])


def command(repo, t):
    """검사 하나를 실행할 명령 — 규칙의 run에 {python}·{file}·{module}을 채운다."""
    relative = (Path(t).relative_to(Path(repo)) if Path(t).is_relative_to(Path(repo)) else Path(t)).as_posix()
    values = {'python': sys.executable, 'file': str(t), 'module': relative.rsplit('.', 1)[0].replace('/', '.')}
    return [part.format(**values) for part in rule_for(repo, t)['run']]


def kill_tree(proc) -> None:
    """검사 프로세스와 그 자식(보조 서버·브라우저)을 같이 끝낸다."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        proc.kill()


def _drain(stream, chunks):
    for line in stream:
        chunks.append(line)


def run(repo, t, args=(), env=None):
    """검사 하나를 시간 제한과 출력 보존 규칙으로 실행한다. env는 이미 비밀값을 뺀 환경이다.
    출력은 따로 읽어 모은다 — 검사가 남긴 고아 자식이 출력 파이프를 붙잡아도 시간 제한·검사 종료 뒤
    PIPE_GRACE_SEC만 기다리고 돌아온다(subprocess.run은 Windows에서 이때 영원히 멈췄다, DEV-205)."""
    proc = subprocess.Popen(command(repo, t) + list(args), cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace",
                            env={**env, "PYTHONIOENCODING": "utf-8", "PYTHONFAULTHANDLER": "1", "DEV_TEST_RUN": "1"},   # 테스트가 진짜 ntfy로 알림을 보내지 않게
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    out, err = [], []
    readers = [threading.Thread(target=_drain, args=pair, daemon=True) for pair in ((proc.stdout, out), (proc.stderr, err))]
    for reader in readers:
        reader.start()
    note, limit = '', timeout_for(t)
    try:
        code = proc.wait(timeout=limit)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try:
            proc.wait(timeout=PIPE_GRACE_SEC)
        except subprocess.TimeoutExpired:
            pass
        code, note = 124, f'\n검사 시간 제한 {limit}초 초과(표시용 종료 코드 124)'
    deadline = time.monotonic() + PIPE_GRACE_SEC
    for reader in readers:
        reader.join(max(0, deadline - time.monotonic()))
    if any(reader.is_alive() for reader in readers):   # 읽기 스레드는 daemon이라 버려 둬도 서버 종료를 막지 않는다
        note += (f'\n⚠️ 검사가 끝난 뒤에도 남은 자식 프로세스가 출력을 붙잡고 있어 {PIPE_GRACE_SEC}초만 기다리고 넘어갔어요'
                 ' — 검사가 띄운 보조 서버를 정리하지 않았는지 확인해 주세요.')
    return subprocess.CompletedProcess(proc.args, code, ''.join(list(out)), ''.join(list(err)) + note)


def timeout_for(t) -> int:
    """검사 하나의 시간 제한(초) — 규칙의 timeout, 없으면 브라우저 검사는 BROWSER_TIMEOUT_SEC, 나머지는 TIMEOUT_SEC."""
    rule = getattr(t, 'rule', None)
    if rule and rule.get('timeout'):
        return rule['timeout']
    return BROWSER_TIMEOUT_SEC if is_browser(t) else TIMEOUT_SEC


def is_browser(t) -> bool:
    """브라우저(Playwright)를 띄우는 검사 — 메모리를 많이 써서 동시에 하나만 돌린다. 규칙의 browser가 있으면 그것을 따른다."""
    rule = getattr(t, 'rule', None)
    if rule and 'browser' in rule:
        return bool(rule['browser'])
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
