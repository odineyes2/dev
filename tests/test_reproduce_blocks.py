"""실패 검사 재현(R16)의 UI 실행 인자(DEV-233) — 블록 표가 있으면 실패한 검사 함수의 블록만 `--only`로, 옛 커밋은 `--x-only` 분기로 고른다."""
import os, sys, tempfile
from pathlib import Path

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'server'))
import reproduce

BLOCKS = '''import sys
def check_a(page, shots): pass
def check_b(page, shots): pass
def check_inner(page, shots): pass
def block_flow():
    check_inner(page, shots)
BLOCKS = [
    _block('a', 'A', check_a),
    _block('b', 'B', check_b, before=_list_page),
    _block('flow', 'flow', source='block_flow', run=lambda: block_flow()),
]
'''
options = reproduce.ui_options(BLOCKS)
assert options['check_a'] == ['--only', 'a'] and options['check_b'] == ['--only', 'b'], options
assert options['check_inner'] == ['--only', 'flow'] and options['block_flow'] == ['--only', 'flow'], options
print('OK — block table → --only block')

OLD = '''import sys
def check_progress_refresh(page, shots): pass
if '--progress-only' in sys.argv:
    check_progress_refresh(page, shots)
'''
assert reproduce.ui_options(OLD) == {'check_progress_refresh': ['--progress-only']}
print('OK — old --x-only branch')

# 지금 저장소의 test_ui.py도 블록으로 풀린다(흐름 블록 안의 검사 포함)
real = reproduce.ui_options((ROOT / 'tests' / 'test_ui.py').read_text(encoding='utf-8'))
assert real['check_issue_table'] == ['--only', 'issue-table'], real.get('check_issue_table')
assert real['check_board_pan'] == ['--only', 'board-flow'], real.get('check_board_pan')
assert real['check_progress_refresh'] == ['--only', 'progress'], real.get('check_progress_refresh')
assert real['check_chat_connections'] == ['--only', 'management-agents'], real.get('check_chat_connections')   # 중첩 호출
print('OK: reproduce block options')
