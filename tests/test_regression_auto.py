"""Auto 계획 선택·서버 보강·사람 우선·동일 코드 검사 재사용을 확인한다."""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ['DEV_DATA_DIR'] = tempfile.mkdtemp()
os.environ['NTFY_TOPIC'] = ''
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import db, issues, orchestrate, regression

db.init()
me = {'kind': 'human', 'name': 'admin'}
repo = Path(tempfile.mkdtemp())
def git(*args):
    r = orchestrate._git(str(repo), *args)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()
git('init', '-q', '-b', 'main')
(repo / 'tests').mkdir()
(repo / 'tests/test_ui.py').write_text("from pathlib import Path\np=Path('count');p.write_text(p.read_text()+'x' if p.exists() else 'x')")
(repo / 'README.md').write_text('base')
git('add', '.'); git('commit', '-qm', 'base')
issues.create_project(me, 'AUTO', 'auto', local_path=str(repo))
parent = issues.create_issue(me, 'AUTO', 'parent')
plan = '## Tasks\n1. UI text | 파일: static/app.js | 확인: capture\n\n## 검사 선택\n```json\n' + json.dumps({
    '1': {'level': 'light', 'reason': '문구만 변경', 'checks': {'agent_capture': True, 'agent_api': False, 'file:test_ui.py': False}}
}, ensure_ascii=False) + '\n```'
issues.post_plan(me, parent['ref'], plan)
issues.decide(me, parent['ref'], 'approve', plan_version=1)
task = issues.get_issue(parent['ref'])['children'][0]
ref = task['ref']
ui = regression.resolve(ref, repo, ['static/app.js'])
assert ui['planned'] and ui['level'] == 'light'
assert ui['checks']['agent_capture']['enabled']
assert not ui['checks']['agent_api']['enabled'] and not ui['checks']['server_completion']['enabled']
high = regression.resolve(ref, repo, ['server/auth.py'])
assert high['level'] == 'high' and high['checks']['file:test_ui.py']['enabled']
assert '서버 보강' in high['checks']['agent_api']['reason']
regression.update(me, {'agent_api': 'off', 'file:test_ui.py': 'on'})
assert not regression.resolve(ref, repo, ['server/auth.py'])['checks']['agent_api']['enabled']
assert regression.resolve(ref, repo, ['README.md'])['checks']['server_completion']['enabled']
for bad in ('## 검사 선택\nno json', '## 검사 선택\n```json\n{"1":{"level":"light","reason":"x","checks":{"unknown":true}}}\n```'):
    try:
        issues.post_plan(me, parent['ref'], bad)
        raise AssertionError('invalid plan accepted')
    except issues.StoreError:
        pass
git('checkout', '-qb', 'relay/' + ref)
(repo / 'README.md').write_text('new')
git('add', '.'); git('commit', '-qm', 'task')
assert orchestrate.run_tests(repo, ref=ref, stage='server_completion') is None
assert (repo / 'count').read_text() == 'x'
assert orchestrate.run_tests(repo, ref=ref, stage='server_merge') is None
assert (repo / 'count').read_text() == 'x'
assert regression.load_record('regression-selection:' + ref + ':server_merge')['results']['test_ui.py'] == 'reused'
(repo / 'README.md').write_text('changed again')
git('add', '.'); git('commit', '-qm', 'changed')
assert orchestrate.run_tests(repo, ref=ref) is None
assert (repo / 'count').read_text() == 'xx'
with db.connect() as c:
    c.execute("UPDATE app_settings SET value='false' WHERE key='regression:agent_api'")
assert regression.settings()['checks'][4]['mode'] in ('on', 'off', 'auto')
assert regression.values()['agent_api'] == 'off'
print('Auto selection, override, validation, reuse and invalidation OK')
