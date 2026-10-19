"""Claude 헤드리스 프롬프트를 명령행 대신 stdin으로 보내 Windows 명령행 한도(WinError 206)를 넘지 않는지 검사한다."""
import os, subprocess, sys, tempfile
from pathlib import Path

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))
import review

# Claude: "-p" 다음 프롬프트만 빠지고 나머지 인자는 그대로다.
cmd = ['claude', '-p', '프롬프트', '--permission-mode', 'acceptEdits', '--allowedTools', 'Read', 'Edit']
launch, prompt = review.stdin_prompt(cmd, 'claude')
assert launch == ['claude', '-p', '--permission-mode', 'acceptEdits', '--allowedTools', 'Read', 'Edit'] and prompt == '프롬프트'
assert cmd[2] == '프롬프트'   # 원래 명령(화면 조회용)은 바꾸지 않는다

# 이미 stdin을 쓰는 명령("-p" 뒤가 옵션)과 "-p"가 없는 명령은 건드리지 않는다.
assert review.stdin_prompt(['claude', '-p', '--restricted'], 'claude') == (['claude', '-p', '--restricted'], None)
assert review.stdin_prompt(['python', '-c', 'x', 'payload'], 'claude') == (['python', '-c', 'x', 'payload'], None)

# Codex는 마지막 인자를 "-"로 바꾼다(기존 동작).
assert review.stdin_prompt(['codex', 'exec', 'P'], 'codex') == (['codex', 'exec', '-'], 'P')

# 명령행 한도를 넘는 프롬프트(한글·따옴표·보조 평면 문자)도 실제 프로세스에 온전히 닿는다.
long_prompt = ('한글 "인용" \\ 경로\n🙂\n' * 4000) + '끝'
assert len(long_prompt) > 32767
script = ('import sys\nsys.stdin.reconfigure(encoding="utf-8")\n'
          'assert sys.argv[1:] == ["-p", "--verbose"], sys.argv\n'
          'sys.stdout.write(str(len(sys.stdin.read())))')
launch, prompt = review.stdin_prompt([sys.executable, '-c', script, '-p', long_prompt, '--verbose'], 'claude')
out = subprocess.run(launch, input=prompt, capture_output=True, text=True, encoding='utf-8', check=True).stdout
assert out == str(len(long_prompt)), out

print('test_claude_stdin OK')
