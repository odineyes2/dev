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
# 서버가 최신 main을 깨끗이 합친 병합 커밋은 연결된 작업 커밋을 이어받는다(MO-35-2 두 번째 실패).
import subprocess
repo = Path(tempfile.mkdtemp())
def git(*args):
    return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()
def commit(name, text):
    (repo / name).write_text(text, 'utf-8')
    git('add', name)
    git('commit', '-qm', name)
    return git('rev-parse', 'HEAD')
git('init', '-q', '-b', 'main')
commit('a.txt', 'a')
git('checkout', '-qb', 'relay/X-1-1')
work = commit('doc.md', 'work')
git('checkout', '-q', 'main')
commit('b.txt', 'b')
git('checkout', '-q', 'relay/X-1-1')
git('merge', '-q', '--no-edit', 'main')
merge = git('rev-parse', 'HEAD')
assert execute.BASE_BRANCH == 'main'
assert execute.base_merge_over_linked(repo, issue(work[:7]), merge) == work
assert execute.base_merge_over_linked(repo, issue(), merge) is None              # 작업 커밋이 연결되지 않음
assert execute.base_merge_over_linked(repo, issue(work), work) is None           # 병합이 아닌 커밋
# 병합에 손을 댄(자동 병합 트리와 다른) 경우는 에이전트가 직접 연결해야 한다.
(repo / 'doc.md').write_text('edited in merge', 'utf-8')
git('add', 'doc.md')
git('commit', '-q', '--amend', '--no-edit')
assert execute.base_merge_over_linked(repo, issue(work), git('rev-parse', 'HEAD')) is None
print('test_commit_linked OK')
