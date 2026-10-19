"""Task가 추가·수정한 검사 파일은 계획 선택과 관계없이 완료·병합 검사에 들어가고, 다른 프로젝트 계획에 dev 검사 목록이 섞이지 않는다(DEV-147)."""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
os.environ['NTFY_TOPIC'] = ''
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import db, issues, regression  # noqa: E402

db.init()
me = {'kind': 'human', 'name': 'admin'}

# Moai처럼 backend/tests/를 쓰는 프로젝트 — dev에도 같은 이름(test_capture_ui.py)이 있다
repo = Path(tempfile.mkdtemp())
(repo / 'backend' / 'tests').mkdir(parents=True)
for name in ('test_capture_ui.py', 'test_board_store.py'):
    (repo / 'backend' / 'tests' / name).write_text('"""검사"""\n', encoding='utf-8')
assert (Path(__file__).parent / 'test_capture_ui.py').exists()   # 이름 충돌 전제
issues.create_project(me, 'MX', 'moai 같은 프로젝트', '', str(repo))

choice = {'1': {'level': 'normal', 'reason': 'dev 검사는 무관', 'checks': {
    'server_completion': False, 'server_merge': False, 'agent_related': False,
    'file:test_capture_ui.py': False, 'file:test_board_store.py': False}}}
parent = issues.create_issue(me, 'MX', '화면 분리')['ref']
issues.post_plan(me, parent, '## Tasks\n1. 작성 화면 분리 | 파일: static/app.js, backend/tests/test_capture_ui.py | 확인: 브라우저 검사 | 선행: 없음\n\n'
                 '## 검사 선택\n```json\n' + json.dumps(choice) + '\n```\n')
issues.decide(me, parent, 'approve', plan_version=1)
task = issues.get_issue(parent)['children'][0]['ref']

# 1. Task가 바꾼 backend/tests 파일은 계획이 꺼도 켜지고, 완료·병합 단계도 켜진다
sel = regression.resolve(task, str(repo), ['static/app.js', 'backend/tests/test_capture_ui.py'])
c = sel['checks']
assert sel['changed_tests'] == ['test_capture_ui.py'] and sel['changed_tests_off'] == []
assert c['file:test_capture_ui.py']['enabled'] and '추가·수정' in c['file:test_capture_ui.py']['reason'], c['file:test_capture_ui.py']
assert c['server_completion']['enabled'] and c['server_merge']['enabled'] and c['agent_related']['enabled']
assert not c['file:test_board_store.py']['enabled']                      # 바뀌지 않은 파일은 계획 선택을 따른다
# 2. dev 자신의 검사 키는 다른 프로젝트 선택에 섞이지 않는다
assert not any(k in c for k in ('file:test_jobs.py', 'file:test_ui.py', 'file:test_regression_settings.py')), sorted(c)
# 3. 검사 파일을 바꾸지 않은 Task는 계획 선택(병합 검사 끔)을 그대로 따른다
c = regression.resolve(task, str(repo), ['static/app.js'])['checks']
assert not c['file:test_capture_ui.py']['enabled'] and not c['server_merge']['enabled']
# 4. 실행 시작 전(diff 없음)에는 Task 본문의 바꿀 파일로 판단한다 — 실행기에게도 그 검사를 돌리라고 알린다
c = regression.resolve(task, str(repo))['checks']   # git 저장소가 아니라 diff가 비고 본문으로 넘어간다
assert c['file:test_capture_ui.py']['enabled'] and c['agent_related']['enabled']

# 5. 계획 에이전트 안내 — 그 프로젝트 검사 파일(경로 포함)만 보여 준다
text = regression.planning_instructions(parent)
assert 'file:test_capture_ui.py=backend/tests/test_capture_ui.py' in text and 'file:test_board_store.py' in text
assert 'file:test_jobs.py' not in text and '항상 돌리므로' in text
dev_text = regression.planning_instructions()
assert 'file:test_jobs.py=' in dev_text                                   # dev(문맥 없음)는 기존 목록 그대로

# 6. dev 프로젝트에서 사람이 Settings로 끈 바뀐 검사는 예외로 두고 따로 알린다
dev_repo = Path(__file__).resolve().parents[1]
issues.create_project(me, 'DEV', 'dev', '', str(dev_repo))
dp = issues.create_issue(me, 'DEV', '대기열')['ref']
issues.post_plan(me, dp, '## Tasks\n1. 대기열 고침 | 파일: server/jobs.py, tests/test_jobs.py | 확인: 검사 | 선행: 없음\n')
issues.decide(me, dp, 'approve', plan_version=1)
dt = issues.get_issue(dp)['children'][0]['ref']
regression.update(me, {'file:test_jobs.py': 'off'})
sel = regression.resolve(dt, str(dev_repo), ['server/jobs.py', 'tests/test_jobs.py'])
assert sel['changed_tests_off'] == ['test_jobs.py'] and not sel['checks']['file:test_jobs.py']['enabled']
regression.update(me, {'file:test_jobs.py': 'auto'})
assert regression.resolve(dt, str(dev_repo), ['server/jobs.py', 'tests/test_jobs.py'])['checks']['file:test_jobs.py']['enabled']
print('changed tests always run, project-scoped test catalog OK')
