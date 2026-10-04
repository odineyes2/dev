"""오케스트레이터 병합(DEV-40-1) — 임시 저장소로: 시험 병합 통과·충돌·실패 시 운영 이력 보존·선행 미병합.
재시작(DEV-40-2) — 가짜 pm2·health로: 서버 변경만 재시작·화면만은 생략·실행 중 작업 대기·대기 초과 on_hold·health 실패 복구."""
import os, subprocess, sys, tempfile
from pathlib import Path
from unittest.mock import patch
from contextlib import nullcontext

os.environ["DEV_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import orchestrate  # noqa: E402

repo = Path(tempfile.mkdtemp())


def git(*a):
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def branch(ref, files):
    git("checkout", "-q", "-b", f"relay/{ref}", "main")
    for name, text in files.items():
        (repo / name).parent.mkdir(exist_ok=True)
        (repo / name).write_text(text)
    git("add", "."); git("commit", "-qm", ref); git("checkout", "-q", "main")


git("init", "-q", "-b", "main")
(repo / "tests").mkdir()
(repo / "tests" / "test_ok.py").write_text("print('ok')\n")
(repo / "a.txt").write_text("a\n")
git("add", "."); git("commit", "-qm", "init")

# 기본 켜짐
assert orchestrate.settings("DEV") == {"auto_merge": True}

# 통과 — --no-ff 병합 커밋
branch("T-1", {"b.txt": "b\n"})
status, sha = orchestrate.merge(str(repo), "T-1")
assert status == "merged", sha
assert git("rev-parse", "HEAD") == sha and len(git("rev-list", "--parents", "-n1", "HEAD").split()) == 3
assert (repo / "b.txt").exists()

# 선행 미병합 → on_hold, 선행이 병합돼 있으면 통과
branch("T-2", {"c.txt": "c\n"})
branch("T-3", {"d.txt": "d\n"})
status, note = orchestrate.merge(str(repo), "T-3", ["T-2"])
assert status == "on_hold" and "T-2" in note, note
assert orchestrate.merge(str(repo), "T-2", ["T-1"])[0] == "merged"
assert orchestrate.merge(str(repo), "T-3", ["T-2"])[0] == "merged"

# 충돌 → changes_requested, main은 그대로
branch("T-4", {"a.txt": "branch\n"})
(repo / "a.txt").write_text("main\n"); git("commit", "-qam", "main edit")
before = git("rev-parse", "HEAD")
status, note = orchestrate.merge(str(repo), "T-4")
assert status == "changes_requested" and "a.txt" in note, note
assert git("rev-parse", "HEAD") == before and not git("status", "--porcelain")

# 더러운 작업 폴더 → on_hold, 병합 안 함
branch("T-5", {"e.txt": "e\n"})
(repo / "a.txt").write_text("dirty\n")
status, note = orchestrate.merge(str(repo), "T-5")
assert status == "on_hold" and "커밋 안 된" in note, note
assert git("rev-parse", "HEAD") == before
git("checkout", "--", "a.txt")

# base가 아닌 브랜치 → on_hold
git("checkout", "-q", "relay/T-5")
assert orchestrate.merge(str(repo), "T-5")[0] == "on_hold"
git("checkout", "-q", "main")

# 시험 병합 검사 실패 → 운영 이력과 내용 모두 이전 상태
branch("T-6", {"tests/test_bad.py": "raise SystemExit('boom')\n", "f.txt": "f\n"})
status, note = orchestrate.merge(str(repo), "T-6")
assert status == "changes_requested" and "test_bad.py" in note and "boom" in note, note
assert git("rev-parse", "HEAD") == before
assert not (repo / "f.txt").exists() and not git("diff", before, "HEAD", "--stat")
assert "종료 코드 1 (0x00000001)" in note and "전체 로그:" in note, note
failure_log = next((orchestrate.config.DATA_DIR / "merge-tests").glob("test_bad-*.log"))
assert "boom" in failure_log.read_text("utf-8")

# 출력 없이 종료해도 원인을 구분할 종료 코드와 로그를 남긴다.
(repo / "tests" / "test_silent.py").write_text("import os; os._exit(7)\n")
note = orchestrate.run_tests(str(repo))
assert "종료 코드 7 (0x00000007)" in note and "출력이 없어요" in note, note
(repo / "tests" / "test_silent.py").unlink()

# Task 단독 검사에는 없던 최신 main과의 상호작용도 시험 병합에서 잡는다.
branch('C-1', {'new-feature.txt': 'task\n'})
(repo / 'main-feature.txt').write_text('main\n')
(repo / 'tests/test_combination.py').write_text("from pathlib import Path\nassert not (Path('main-feature.txt').exists() and Path('new-feature.txt').exists()), 'combination failed'\n")
git('add', '.'); git('commit', '-qm', 'main regression check')
before = git('rev-parse', 'HEAD')
assert orchestrate.merge(str(repo), 'C-1')[0] == 'changes_requested'
assert git('rev-parse', 'HEAD') == before and not (repo / 'new-feature.txt').exists()

# 검사한 사본은 detached이며 운영 main은 검사 중에도 원래 SHA를 유지한다.
branch('C-2', {'safe.txt': 'safe\n'})
checked = []
def inspect_candidate(path):
    assert Path(path).resolve() != repo.resolve()
    assert git('rev-parse', 'HEAD') == before and not (repo / 'safe.txt').exists()
    assert orchestrate._git(path, 'rev-parse', '--abbrev-ref', 'HEAD').stdout.strip() == 'HEAD'
    checked.append(orchestrate._git(path, 'rev-parse', 'HEAD').stdout.strip())
with patch.object(orchestrate, 'run_tests', side_effect=inspect_candidate):
    status, sha = orchestrate.merge(str(repo), 'C-2')
assert status == 'merged' and checked == [sha] and git('rev-parse', 'HEAD') == sha

# 검사 중 외부 커밋·Task 변경·미커밋 변경은 검사한 커밋을 반영하지 않는다.
for kind in ('main', 'task', 'dirty', 'candidate'):
    ref = 'R-' + kind
    branch(ref, {ref + '.txt': 'task\n'})
    before = git('rev-parse', 'HEAD')
    def race(path):
        if kind == 'main':
            (repo / 'external.txt').write_text('keep\n'); git('add', '.'); git('commit', '-qm', 'external change')
        elif kind == 'task':
            git('update-ref', 'refs/heads/relay/' + ref, before)
        elif kind == 'dirty':
            (repo / 'a.txt').write_text('uncommitted\n')
        else:
            (Path(path) / 'a.txt').write_text('changed by test\n')
    with patch.object(orchestrate, 'run_tests', side_effect=race):
        status, note = orchestrate.merge(str(repo), ref)
    assert status == ('changes_requested' if kind == 'candidate' else 'on_hold'), note
    assert not (repo / (ref + '.txt')).exists()
    if kind != 'main':
        assert git('rev-parse', 'HEAD') == before
    else:
        assert (repo / 'external.txt').read_text() == 'keep\n'
    if kind == 'dirty':
        assert (repo / 'a.txt').read_text() == 'uncommitted\n'
        git('checkout', '--', 'a.txt')
assert len(git('worktree', 'list', '--porcelain').split('worktree ')) == 2
assert not list((orchestrate.config.DATA_DIR / 'merge-worktrees').iterdir())

# 운영 사본의 untracked 파일과 충돌하는 반영은 실패하되 그 파일을 보존한다.
branch('C-collision', {'collision.txt': 'task\n'})
(repo / 'collision.txt').write_text('local\n')
before = git('rev-parse', 'HEAD')
assert orchestrate.merge(str(repo), 'C-collision')[0] == 'on_hold'
assert git('rev-parse', 'HEAD') == before and (repo / 'collision.txt').read_text() == 'local\n'
(repo / 'collision.txt').unlink()

# 첫 실패 뒤 나머지 검사도 실행하고, 시간 제한도 실패 로그로 남긴다.
with patch.object(orchestrate.subprocess, 'run', side_effect=[
        subprocess.TimeoutExpired(['python'], 600, output=b'partial'),
        subprocess.CompletedProcess(['python'], 2, 'second failure', '')]) as run:
    with patch.object(orchestrate.Path, 'glob', return_value=[Path('tests/test_one.py'), Path('tests/test_two.py')]):
        note = orchestrate.run_tests(str(repo))
assert run.call_count == 2 and '600초' in note and 'partial' in note and 'second failure' in note

# 같은 Task의 새 커밋이 없으면 다른 main 커밋을 배포·revert 대상으로 오인하지 않는다.
assert orchestrate.merge(str(repo), 'C-2')[0] == 'on_hold'

# ---- 재시작·health(DEV-40-2) — 가짜 pm2(로그 파일에 한 줄)와 가짜 health·busy 서버 ----
import json, threading  # noqa: E401,E402
from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402

state = {"health": 200, "busy": 0}   # busy: 앞으로 몇 번 더 "바쁨"이라고 답할지


class Fake(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/busy":
            body = json.dumps({"busy": state["busy"] > 0}).encode()
            state["busy"] = max(0, state["busy"] - 1)
            code = 200
        else:
            body, code = b"{}", state["health"]
        self.send_response(code); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_port}"
log = Path(tempfile.mkdtemp()) / "pm2.log"
fake_pm2 = log.with_name("fake_pm2.py")
fake_pm2.write_text("import sys\nopen(sys.argv[1], 'a').write(sys.argv[2] + '\\n')\n")
cfg = {"pm2_app": "app", "restart_cmd": f'"{sys.executable}" "{fake_pm2}" "{log}" {{app}}',
       "health_url": url + "/health", "health_seconds": 1, "poll_seconds": 0.05}


def restarts():
    return log.read_text().count("app") if log.exists() else 0


def merged(ref, files):
    branch(ref, files)
    status, sha = orchestrate.merge(str(repo), ref)
    assert status == "merged", sha
    return sha


# 화면 파일만 → 재시작 안 함
status, note = orchestrate.deploy(str(repo), cfg, merged("T-7", {"static/x.js": "1\n"}))
assert status == "merged" and restarts() == 0, note

# 서버 파일 → 재시작 한 번, health 통과
status, note = orchestrate.deploy(str(repo), cfg, merged("T-8", {"server/x.py": "1\n"}))
assert status == "merged" and restarts() == 1 and "health" in note, note

# 실행 중 작업 → 기다렸다가 재시작, 대기 안내
said = []
state["busy"] = 3
status, note = orchestrate.deploy(str(repo), {**cfg, "busy_url": url + "/busy"}, merged("T-9", {"server/y.py": "1\n"}), said.append)
assert status == "merged" and restarts() == 2 and said and "재시작할 예정" in said[0], (note, said)

# 끝까지 안 끝남 → 재시작 없이 on_hold
state["busy"] = 10 ** 6
status, note = orchestrate.deploy(str(repo), {**cfg, "busy_url": url + "/busy", "wait_minutes": 0.002}, merged("T-10", {"server/z.py": "1\n"}))
assert status == "on_hold" and restarts() == 2, note
state["busy"] = 0

# health 실패 → revert 커밋, 다시 재시작해 복구 시도
state["health"] = 500
before = git("rev-parse", "HEAD")
sha = merged("T-11", {"server/w.py": "1\n"})
status, note = orchestrate.deploy(str(repo), cfg, sha)
assert status == "changes_requested" and "revert" in note and restarts() == 4, note
assert git("log", "-1", "--format=%s").startswith("Revert") and not git("diff", before, "HEAD", "--stat")
state["health"] = 200
srv.shutdown()

print("ok")

# 시험 병합 검사도 NTFY 비밀값 없이 돌아간다(진짜 알림이 나가지 않게)
os.environ["NTFY_TOPIC"] = "should-not-leak"
(repo / "tests" / "test_env.py").write_text("import os,sys; sys.exit(1 if os.environ.get('NTFY_TOPIC') else 0)\n")
assert orchestrate.run_tests(str(repo)) is None
print("ok env")

# 구현 완료 이후의 영속 단계와 복구는 운영 서비스 없이 검사한다.
import db, issues, execute, review, jobs, notify
db.init()
me = {'kind': 'human', 'name': 'admin'}
issues.create_project(me, 'FLOW', 'flow', local_path=str(repo))
parent = issues.create_issue(me, 'FLOW', 'parent')
issues.post_plan(me, parent['ref'], 'approved')
issues.decide(me, parent['ref'], 'approve', plan_version=1)


def ready(provider='claude', auto=True, restart=False):
    cfg = {'auto_merge': auto, 'pm2_app': 'dev' if restart else '',
           'health_url': 'http://fake/health', 'health_seconds': 0}
    orchestrate.SETTINGS.write_text(json.dumps({'FLOW': cfg}))
    task = issues.create_issue(me, 'FLOW', 'task', parent=parent['ref'])
    ref = task['ref']
    wt = execute.prepare_worktree(str(repo), ref)
    _, rid = review.begin(me, task, 'execute', provider)
    start = execute._git(wt, 'rev-parse', 'HEAD')
    with db.connect() as c:
        c.execute('UPDATE runs SET task_start_sha=? WHERE id=?', (start, rid))
    (wt / ('feature-' + str(rid) + ('.py' if restart else '.txt'))).write_text('feature')
    execute._git(wt, 'add', '.')
    execute._git(wt, '-c', 'user.name=test', '-c', 'user.email=t@t', 'commit', '-qm', 'implemented')
    sha = execute._git(wt, 'rev-parse', 'HEAD')
    issues.link_commit(me, ref, sha)
    if restart:
        cfg['restart_when'] = ['feature-*']
        orchestrate.SETTINGS.write_text(json.dumps({'FLOW': cfg}))
    with db.connect() as c:
        c.execute('INSERT INTO task_execution_results(run_id,plan_version) VALUES(?,1)', (rid,))
    execute.register_completion(me, ref, wt, rid, 'check feature')
    return ref, rid


class Interrupted(BaseException):
    pass


with patch.object(orchestrate, 'run_tests', return_value=None), patch.object(notify, 'send') as sent:
    for provider in ('claude', 'codex'):
        for auto in (True, False):
            ref, rid = ready(provider, auto)
            assert issues.get_issue(ref)['status'] == ('in_progress' if auto else 'in_review')
            before_sent = sent.call_count
            if auto:
                def inspect(path):
                    assert issues.get_issue(ref)['status'] == 'in_progress'
                    assert orchestrate.completion(ref)['phase'] == 'checking'
                    assert jobs.busy()
                with patch.object(orchestrate, 'run_tests', side_effect=inspect):
                    assert orchestrate.handle(me, ref) == 'merged'
                assert orchestrate.completion(ref)['phase'] == 'complete'
                assert issues.get_issue(ref)['status'] == 'in_review'
                with db.connect() as c:
                    receipt = c.execute('SELECT * FROM task_execution_results WHERE run_id=?', (rid,)).fetchone()
                    assert receipt['state'] == 'merged'
                    assert receipt['status_event_id'] == c.execute("SELECT MAX(id) FROM events WHERE issue_id=? AND kind='status'", (issues.get_issue(ref)['id'],)).fetchone()[0]
                assert orchestrate.handle(me, ref) is None
                orchestrate.recover()
                assert sent.call_count == before_sent + 1
            else:
                assert orchestrate.handle(me, ref) is None

    # 검사 실패·커밋 변경·사람의 상태 변경을 구분한다.
    ref, _ = ready()
    with patch.object(orchestrate, 'run_tests', return_value='failed regression'):
        assert orchestrate.handle(me, ref) == 'changes_requested'
    ref, _ = ready()
    git('update-ref', 'refs/heads/relay/' + ref, git('rev-parse', 'main'))
    assert orchestrate.handle(me, ref) == 'on_hold'
    ref, _ = ready()
    issues.set_status(me, ref, 'on_hold')
    issues.set_status(me, ref, 'in_progress')
    before = git('rev-parse', 'HEAD')
    assert orchestrate.handle(me, ref) is None
    assert git('rev-parse', 'HEAD') == before
    assert orchestrate.completion(ref)['phase'] == 'abandoned'

    # ff-only 반영 직후 종료되면 이미 반영한 SHA부터 이어간다.
    ref, _ = ready()
    save = orchestrate._save
    def die_after_apply(record, phase, **values):
        if phase == 'deployed':
            raise Interrupted()
        return save(record, phase, **values)
    try:
        with patch.object(orchestrate, '_save', side_effect=die_after_apply):
            orchestrate.handle(me, ref)
        raise AssertionError('expected interruption')
    except Interrupted:
        pass
    assert orchestrate.completion(ref)['phase'] == 'applying'
    db.init(); jobs.reconcile()
    assert issues.get_issue(ref)['status'] == 'in_progress' and jobs.busy()
    with patch.object(orchestrate, 'run_tests') as tests:
        assert orchestrate.handle(me, ref) == 'merged'
        tests.assert_not_called()

    # 자기 재시작 이후 새 서버는 명령을 재실행하지 않고 반영 health를 확인한다.
    ref, _ = ready(restart=True)
    try:
        with patch.object(orchestrate, '_restart', side_effect=Interrupted()):
            orchestrate.handle(me, ref)
        raise AssertionError('expected interruption')
    except Interrupted:
        pass
    record = orchestrate.completion(ref)
    assert record['phase'] == 'restart_requested'
    assert issues.get_issue(ref)['status'] == 'in_progress'
    db.init(); jobs.reconcile()
    assert issues.get_issue(ref)['status'] == 'in_progress'
    cfg = {**orchestrate.DEFAULTS, **json.loads(record['cfg_json'])}
    with patch.object(orchestrate, '_get', return_value=(200, json.dumps({'process_id': record['process_id'], 'revision': record['merge_sha']}))):
        assert not orchestrate._verified_health(cfg, record)
    with patch.object(orchestrate, '_get', return_value=(200, json.dumps({'process_id': 'new', 'revision': 'old'}))):
        assert not orchestrate._verified_health(cfg, record)
    with patch.object(orchestrate, 'PROCESS_ID', 'new'), patch.object(orchestrate, '_restart') as restart, patch.object(orchestrate, '_get', return_value=(200, json.dumps({'process_id': 'new', 'revision': record['merge_sha']}))):
        orchestrate.take_over()
        orchestrate.recover(); orchestrate.recover()
        restart.assert_not_called()
        assert issues.get_issue(ref)['status'] == 'in_review'

    # health 실패의 revert와 복구 재시작 정책을 유지한다.
    ref, _ = ready(restart=True)
    with patch.object(orchestrate, '_restart', return_value=None) as restart, patch.object(orchestrate, '_verified_health', side_effect=[False, True]):
        assert orchestrate.handle(me, ref) == 'changes_requested'
        assert restart.call_count == 2
    assert git('log', '-1', '--format=%s').startswith('Revert')
    assert not orchestrate.pending()

    # 새 실행이 생기면 오래된 완료 기록이 대기열을 영구 차단하지 않는다.
    ref, rid = ready()
    record = orchestrate.completion(ref)
    before = git('rev-parse', 'HEAD')
    with db.connect() as c:
        c.execute("INSERT INTO runs(issue_id,mode,status,actor,started_at) VALUES(?,'execute','ok','human:admin',?)",
                  (record['issue_id'], db.now_iso()))
    orchestrate.recover()
    assert not orchestrate.pending()
    assert git('rev-parse', 'HEAD') == before
    assert issues.get_issue(ref)['status'] == 'in_progress'
    with db.connect() as c:
        assert c.execute('SELECT phase FROM execution_completion WHERE run_id=?', (rid,)).fetchone()[0] == 'abandoned'
print('OK persistent completion')

# 재시작 경계의 중단은 새 프로세스의 DB 소유권 인계로 재현한다.
with patch.object(orchestrate, 'run_tests', return_value=None), patch.object(notify, 'send') as sent:
    for boundary in ('checking', 'applying', 'deployed', 'finalizing', 'publishing'):
        ref, rid = ready()
        before = git('rev-parse', 'HEAD')
        original_finish = orchestrate._finish
        def interrupt_save(record, phase, **values):
            result = save(record, phase, **values)
            if phase == boundary:
                raise Interrupted()
            return result
        def interrupt_finish(*args):
            raise Interrupted()
        try:
            with patch.object(orchestrate, '_save', side_effect=interrupt_save), \
                 patch.object(orchestrate, '_finish', side_effect=interrupt_finish if boundary == 'finalizing' else original_finish), \
                 (patch.object(orchestrate, '_publish', side_effect=Interrupted()) if boundary == 'publishing' else nullcontext()):
                orchestrate.handle(me, ref)
            raise AssertionError('expected interruption at ' + boundary)
        except Interrupted:
            pass
        assert issues.get_issue(ref)['status'] == ('in_review' if boundary == 'publishing' else 'in_progress')
        notices = sent.call_count
        db.init(); jobs.reconcile()
        with patch.object(orchestrate, 'PROCESS_ID', 'recovered-' + str(rid)), patch.object(orchestrate, '_restart') as restart:
            orchestrate.take_over()
            orchestrate.recover(); orchestrate.recover()
            restart.assert_not_called()
        expected = 'on_hold' if boundary == 'applying' else 'in_review'
        assert issues.get_issue(ref)['status'] == expected, boundary
        assert sent.call_count == notices + (expected == 'in_review'), boundary
        assert not orchestrate.pending()
        if boundary == 'applying':
            assert git('rev-parse', 'HEAD') == before

    # 最終 health 成功と状態保存の間に人が判断しても通知・昇格しない。
    for status in ('on_hold', 'changes_requested', 'done', 'closed'):
        ref, rid = ready(restart=True)
        notices = sent.call_count
        def human_decision(cfg, record):
            # 運用のロールバックは別検査で扱い、ここでは状態所有権を検査する。
            with db.connect() as c:
                issues._set_status(c, me, issues._find(c, ref), status, 'human decision')
            return True
        with patch.object(orchestrate, '_restart', return_value=None), patch.object(orchestrate, '_verified_health', side_effect=human_decision), patch.object(orchestrate, 'promote_parent') as promote:
            assert orchestrate.handle(me, ref) is None
            promote.assert_not_called()
        assert issues.get_issue(ref)['status'] == status
        assert orchestrate.completion(ref)['phase'] == 'abandoned'
        assert sent.call_count == notices and not orchestrate.pending()

    # revert 完了直後の中断は復旧コミット・再起動を重複させない。
    ref, rid = ready(restart=True)
    def interrupt_revert(record, phase, **values):
        if phase == 'rollback_applied':
            raise Interrupted()
        return save(record, phase, **values)
    try:
        with patch.object(orchestrate, '_save', side_effect=interrupt_revert), patch.object(orchestrate, '_restart', return_value=None), patch.object(orchestrate, '_verified_health', return_value=False):
            orchestrate.handle(me, ref)
        raise AssertionError('expected interruption after revert')
    except Interrupted:
        pass
    reverted = git('rev-parse', 'HEAD')
    assert git('log', '-1', '--format=%s').startswith('Revert')
    with patch.object(orchestrate, 'PROCESS_ID', 'rollback-recovery'), patch.object(orchestrate, '_restart', return_value=None) as restart, patch.object(orchestrate, '_verified_health', return_value=True):
        orchestrate.take_over(); orchestrate.recover(); orchestrate.recover()
        assert restart.call_count == 1
    assert git('rev-parse', 'HEAD') == reverted
    assert issues.get_issue(ref)['status'] == 'changes_requested' and not orchestrate.pending()
    # 재시작 명령 실패는 revert 복구로, busy 시간 초과는 보류로 끝난다.
    ref, rid = ready(restart=True)
    notices = sent.call_count
    with patch.object(orchestrate, '_restart', side_effect=['command denied', None]) as restart, patch.object(orchestrate, '_verified_health', return_value=True):
        assert orchestrate.handle(me, ref) == 'changes_requested'
        assert restart.call_count == 2
    assert git('log', '-1', '--format=%s').startswith('Revert')
    assert orchestrate.completion(ref)['phase'] == 'failed'
    assert sent.call_count == notices and not orchestrate.pending()
    ref, rid = ready(restart=True)
    with db.connect() as c:
        cfg = json.loads(orchestrate.completion(ref)['cfg_json'])
        cfg['wait_minutes'] = 0
        c.execute('UPDATE execution_completion SET cfg_json=? WHERE run_id=?', (json.dumps(cfg), rid))
    with patch.object(orchestrate, '_busy', return_value=True), patch.object(orchestrate, '_restart') as restart:
        assert orchestrate.handle(me, ref) == 'on_hold'
        restart.assert_not_called()
    assert orchestrate.completion(ref)['phase'] == 'held'
    assert sent.call_count == notices and not orchestrate.pending()

    # 재시작 대기 중 실제 거절 롤백이 들어오면 오래된 후처리는 되살리지 않는다.
    import rollback
    ref, rid = ready(restart=True)
    notices = sent.call_count
    try:
        with patch.object(orchestrate, '_restart', side_effect=Interrupted()):
            orchestrate.handle(me, ref)
        raise AssertionError('expected interruption before rejection')
    except Interrupted:
        pass
    with patch.object(rollback, 'start_recovery'), patch.object(orchestrate, '_restart', return_value=None), patch.object(orchestrate, '_healthy', return_value=True):
        issues.set_status(me, ref, 'closed', '반영 결과 거절')
        rollback.recover()
        assert issues.get_issue(ref)['status'] == 'closed'
        rejected_head = git('rev-parse', 'HEAD')
        with patch.object(orchestrate, 'PROCESS_ID', 'after-rejection'), patch.object(orchestrate, '_restart') as restart:
            orchestrate.take_over(); orchestrate.recover(); rollback.recover(); orchestrate.recover()
            restart.assert_not_called()
    assert git('rev-parse', 'HEAD') == rejected_head
    assert issues.get_issue(ref)['status'] == 'closed'
    assert orchestrate.completion(ref)['phase'] == 'abandoned'
    assert sent.call_count == notices and not orchestrate.pending()

    # 상태가 그대로여도 goal 보호를 새로 붙이면 후처리 소유권은 사라진다.
    ref, rid = ready()
    before = git('rev-parse', 'HEAD')
    with db.connect() as c:
        c.execute('UPDATE issues SET labels_json=? WHERE id=?', ('["goal"]', orchestrate.completion(ref)['issue_id']))
    assert orchestrate.handle(me, ref) is None
    assert git('rev-parse', 'HEAD') == before
    assert issues.get_issue(ref)['status'] == 'in_progress'
    assert orchestrate.completion(ref)['phase'] == 'abandoned'
print('OK interruption and protected completion boundaries')
