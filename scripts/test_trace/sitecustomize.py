"""검사 의존 기록(DEV-200) — scripts/run_tests.py가 PYTHONPATH로 넣으면 Python이 시작할 때 읽혀, 저장소 안에서 읽은 파일을 기록한다.
기록 줄: `R 상대경로`(읽음), `W 이유`(무엇을 읽었는지 알 수 없는 일 — 무엇이 바뀌든 다시 돌려야 함). 하위 Python 프로세스도 환경을 물려받아 같은 파일에 덧붙인다."""
import os
import shlex
import sys

_OUT = os.environ.get('DEV_TEST_TRACE')
_ROOT = os.environ.get('DEV_TEST_TRACE_ROOT')

if _OUT and _ROOT:
    _root = os.path.normcase(os.path.abspath(_ROOT)).rstrip('\\/') + os.sep
    _fd = os.open(_OUT, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)   # 훅을 걸기 전에 연다 — 강제 종료돼도 줄마다 남는다
    _seen = set()
    _WRITE = os.O_WRONLY | os.O_RDWR
    _PY = ('python', 'pythonw', 'python3', 'py')

    def _emit(line):
        if line not in _seen:
            _seen.add(line)
            try:
                os.write(_fd, (line + '\n').encode('utf-8'))
            except OSError:
                pass

    def _inside(path):
        try:
            full = os.path.normcase(os.path.abspath(os.fsdecode(path)))
        except (TypeError, ValueError):
            return None
        full += os.sep   # 저장소 폴더 자체('')도 안쪽으로 본다
        return full[len(_root):-1].replace('\\', '/') if full.startswith(_root) else None

    def _source(rel):
        """__pycache__/x.cpython-313.pyc는 원본 x.py를 읽은 것으로 본다(캐시가 있으면 원본을 열지 않는다)."""
        head, _, name = rel.rpartition('__pycache__/')
        return head + name.split('.')[0] + '.py' if _ and name.endswith('.pyc') else rel

    def _watch(event, args):
        if event == 'open':
            path, mode, flags = args
            if isinstance(path, int) or (flags or 0) & _WRITE or (mode and any(c in mode for c in 'wax+')):
                return
            rel = _inside(path)
            if rel is not None:
                _emit('R ' + _source(rel))
        elif event == 'subprocess.Popen':
            executable, argv, cwd, env = args
            if isinstance(argv, (str, bytes)):   # Windows에서는 명령이 문자열 하나로 온다
                try:
                    words = [w.strip('"') for w in shlex.split(os.fsdecode(argv), posix=False)]
                except ValueError:   # 따옴표가 짝이 안 맞는 명령
                    words = os.fsdecode(argv).split()
            else:
                words = [os.fsdecode(a) for a in argv or () if isinstance(a, (str, bytes, os.PathLike))]
            program = os.fsdecode(executable) if executable else (words[0] if words else '')
            base = os.path.splitext(os.path.basename(program))[0].lower()
            if base in _PY or base.startswith('python'):
                if env is not None and env.get('DEV_TEST_TRACE') != _OUT:   # 기록 환경을 떼고 띄운 Python은 무엇을 읽는지 모른다
                    _emit('W 기록 환경 없이 Python 하위 프로세스')
                return
            if 'playwright' in program.replace('\\', '/').lower():         # 브라우저 드라이버 — 화면 파일은 Python 서버가 읽어 기록된다
                return
            if base == 'taskkill':                                          # 프로세스 종료만 한다
                return
            if base == 'git' and 'rev-parse' in words[1:3]:                # 커밋 번호만 읽는다(orchestrate.BOOT_SHA 등) — 작업 파일 내용과 무관
                return
            where = os.fsdecode(cwd) if cwd else os.getcwd()
            if _inside(where) is not None or any(_inside(w) is not None for w in words[1:] if os.path.isabs(w)):
                _emit('W 저장소에서 외부 프로그램: ' + base)
        elif event in ('os.system', 'os.exec', 'os.posix_spawn', 'os.spawn', 'os.startfile'):
            _emit('W ' + event)

    def _hook(event, args):
        try:
            _watch(event, args)
        except Exception:   # 기록 실패가 검사를 깨뜨리지 않게 — 대신 무엇을 읽었는지 모르는 검사로 둔다
            _emit('W 기록 오류: ' + event)

    sys.addaudithook(_hook)
