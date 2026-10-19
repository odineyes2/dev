"""Task 완료 검사가 짧은 해시로 연결된 커밋도 같은 커밋으로 인정하는지 검사한다(MO-35-2 실패 재발 방지)."""
import os, sys, tempfile
from pathlib import Path

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))
import execute

full = 'cc5f074de83e1e84caa1ab63313c4c544d216ebc'
def issue(*shas):
    return {'events': [{'kind': 'comment', 'data': {}}] + [{'kind': 'commit', 'data': {'sha': s}} for s in shas]}

assert execute.commit_linked(issue(full), full)
assert execute.commit_linked(issue('cc5f074'), full)            # 에이전트가 짧은 해시로 연결
assert not execute.commit_linked(issue('cc5f07'), full)         # 7자 미만은 인정하지 않는다
assert not execute.commit_linked(issue('bab9362'), full)        # 다른 커밋
assert not execute.commit_linked(issue(), full)
assert not execute.commit_linked(issue('cc5f074'), '')
print('test_commit_linked OK')
