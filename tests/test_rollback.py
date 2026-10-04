"""거절 롤백 — 운영 사본 보존, 후손 범위, 후속 변경, 실패 및 재시작 복구."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
os.environ['NTFY_TOPIC'] = ''
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))
import db, issues, orchestrate, rollback

db.init()
human = {'kind': 'human', 'name': 'admin'}
sequence = 0


def git(repo, *args):
    result = subprocess.run(['git', '-c', 'user.name=test', '-c', 'user.email=test@local', *args], cwd=repo, capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def case():
    global sequence
    sequence += 1
    repo = Path(tempfile.mkdtemp())
    git(repo, 'init', '-q', '-b', 'main')
    (repo / 'base.txt').write_text('base\n')
    git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'base')
    key = f'R{sequence}'
    issues.create_project(human, key, 'rollback', local_path=str(repo))
    parent = issues.create_issue(human, key, title='검토 중')
    issues.post_plan(human, parent['ref'], '## Tasks\n1. 첫째\n2. 둘째')
    issues.decide(human, parent['ref'], 'approve', plan_version=1)
    children = issues.get_issue(parent['ref'])['children']
    merges = []
    for child, file in zip(children, ('first.txt', 'second.txt')):
        git(repo, 'checkout', '-qb', 'relay/' + child['ref'], 'main')
        (repo / file).write_text('original\n')
        git(repo, 'add', '.'); git(repo, 'commit', '-qm', child['ref']); git(repo, 'checkout', '-q', 'main')
        status, sha = orchestrate.merge(str(repo), child['ref'])
        assert status == 'merged', sha
        merges.append(sha)
        issues.set_status(human, child['ref'], 'in_review')
    issues.set_status(human, parent['ref'], 'in_review')
    return repo, parent['ref'], [child['ref'] for child in children], merges


def fails(action):
    try:
        action()
        raise AssertionError('거절해야 한다')
    except issues.StoreError as error:
        assert error.status == 409, str(error)


with patch.object(orchestrate, 'run_tests', return_value=None), patch.object(rollback, 'start_recovery'):
    # 부모 거절은 역순으로 두 병합을 되돌리고 뒤에 들어온 독립 변경은 유지한다.
    repo, parent, children, merges = case()
    (repo / 'later.txt').write_text('keep\n')
    git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'unrelated')
    issues.set_status(human, parent, 'closed', '방향이 달라요')
    assert not (repo / 'first.txt').exists() and not (repo / 'second.txt').exists()
    assert (repo / 'later.txt').read_text() == 'keep\n'
    assert all(issues.get_issue(ref)['status'] == 'closed' for ref in [parent, *children])
    rollback.recover()
    before = git(repo, 'rev-parse', 'HEAD')
    issues.set_status(human, parent, 'closed', '재시도')
    assert git(repo, 'rev-parse', 'HEAD') == before
    assert not rollback.active_merges(str(repo), set(children))
    assert orchestrate.merge_state(issues.get_issue(children[0])) == '되돌림'

    # Task 하나만 거절하면 형제 Task의 병합은 남는다.
    repo, parent, children, merges = case()
    issues.set_status(human, children[0], 'closed', '이 Task만 거절')
    assert not (repo / 'first.txt').exists() and (repo / 'second.txt').exists()
    assert issues.get_issue(parent)['status'] == 'in_review'
    rollback.recover()

    # 후속 변경과 충돌하거나 검사가 실패하면 main 및 이슈 상태를 유지한다.
    repo, parent, children, merges = case()
    (repo / 'first.txt').write_text('later changed\n')
    git(repo, 'commit', '-qam', 'dependent change')
    before = git(repo, 'rev-parse', 'HEAD')
    fails(lambda: issues.set_status(human, parent, 'closed', '충돌'))
    assert git(repo, 'rev-parse', 'HEAD') == before and issues.get_issue(parent)['status'] == 'in_review'
    assert '롤백 실패' in issues.get_issue(parent)['events'][-1]['body']
    repo, parent, children, merges = case()
    before = git(repo, 'rev-parse', 'HEAD')
    with patch.object(orchestrate, 'run_tests', return_value='regression failed'):
        fails(lambda: issues.decide(human, parent, 'reject', '검사 실패', plan_version=1))
    assert git(repo, 'rev-parse', 'HEAD') == before
    assert issues.get_issue(parent)['approval']['verdict'] == 'approve'
    assert issues.get_issue(parent)['status'] == 'in_review'

    # 더러운 사본과 실행 중 Agent가 있으면 Git을 변경하지 않는다.
    repo, parent, children, merges = case()
    (repo / 'base.txt').write_text('dirty\n')
    fails(lambda: issues.set_status(human, parent, 'closed', 'dirty'))
    assert (repo / 'base.txt').read_text() == 'dirty\n'
    git(repo, 'checkout', '--', 'base.txt')
    with db.connect() as c:
        child = issues._find(c, children[0])
        c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at) VALUES(?, 'execute', 'running', 'human:admin', ?)", (child['id'], db.now_iso()))
    before = git(repo, 'rev-parse', 'HEAD')
    fails(lambda: issues.set_status(human, parent, 'closed', 'running'))
    assert git(repo, 'rev-parse', 'HEAD') == before
    with db.connect() as c:
        c.execute("UPDATE runs SET status='failed' WHERE issue_id=?", (child['id'],))

    # 수동 revert는 중복 적용하지 않고, revert 취소로 다시 적용된 병합은 찾는다.
    repo, parent, children, merges = case()
    git(repo, 'revert', '-m', '1', '--no-edit', merges[0])
    reverted = git(repo, 'rev-parse', 'HEAD')
    assert [ref for _, ref in rollback.active_merges(str(repo), set(children))] == [children[1]]
    git(repo, 'revert', '--no-edit', reverted)
    assert {ref for _, ref in rollback.active_merges(str(repo), set(children))} == set(children)
    issues.decide(human, parent, 'reject', '계획도 거절', plan_version=1)
    assert issues.get_issue(parent)['status'] == 'closed' and not (repo / 'first.txt').exists()
    rollback.recover()

    # 서버 재시작 실패 시 Closed가 되지 않고, 프로세스 재기동 후 이어서 종결한다.
    repo, parent, children, merges = case()
    cfg = {'pm2_app': 'test', 'restart_when': ['*'], 'health_url': 'http://test.invalid'}
    with patch.object(orchestrate, 'settings', return_value=cfg):
        issues.set_status(human, parent, 'closed', '운영도 되돌림')
        assert all(issues.get_issue(ref)['status'] == 'on_hold' for ref in [parent, *children])
        before = git(repo, 'rev-parse', 'HEAD')
        with patch.object(orchestrate, '_restart', return_value='denied'):
            rollback.recover()
        assert issues.get_issue(parent)['status'] == 'on_hold'
        with patch.object(orchestrate, '_restart', return_value=None), patch.object(orchestrate, '_healthy', return_value=True):
            rollback.recover()
        assert all(issues.get_issue(ref)['status'] == 'closed' for ref in [parent, *children])
        assert git(repo, 'rev-parse', 'HEAD') == before

    # Git 반영 후 DB 커밋이 끊겨도 저장된 의도로 종결을 복구한다.
    repo, parent, children, merges = case()
    with patch.object(issues, '_set_status', side_effect=issues.StoreError('DB interruption', 409)):
        fails(lambda: issues.set_status(human, parent, 'closed', '중단 복구'))
    assert issues.get_issue(parent)['status'] == 'in_review' and not (repo / 'first.txt').exists()
    rollback.recover()
    assert issues.get_issue(parent)['status'] == 'closed'

    # 자기 서버 재시작으로 중단된 기록은 다시 재시작하지 않고 health를 확인한다.
    repo, parent, children, merges = case()
    with patch.object(orchestrate, 'settings', return_value=cfg):
        issues.set_status(human, parent, 'closed', '자기 재시작')
        record = next(r for r in rollback._records() if r['ref'] == parent)
        record['phase'] = 'restarting'; rollback._save(record)
        with patch.object(orchestrate, '_restart', side_effect=AssertionError('중복 재시작')), patch.object(orchestrate, '_healthy', return_value=True):
            rollback.recover()
        assert issues.get_issue(parent)['status'] == 'closed'

    # 기다리는 동안 사람이 상태를 바꿨다가 돌려도 그 결정을 덮어쓰지 않는다.
    repo, parent, children, merges = case()
    with patch.object(orchestrate, 'settings', return_value=cfg):
        issues.set_status(human, parent, 'closed', '사람 판단 보호')
        issues.set_status(human, parent, 'in_review')
        with patch.object(orchestrate, '_restart', return_value=None), patch.object(orchestrate, '_healthy', return_value=True):
            rollback.recover()
        assert issues.get_issue(parent)['status'] == 'in_review'

    # 병합 이력이 없는 일반 폴더도 정상적으로 거절할 수 있다.
    issues.create_project(human, 'PLAIN', '일반 폴더', local_path=tempfile.mkdtemp())
    plain = issues.create_issue(human, 'PLAIN', '미구현')
    assert issues.set_status(human, plain['ref'], 'closed')['status'] == 'closed'

print('OK: rollback')
