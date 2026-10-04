"""폰 알림(DEV-38) — notify._post를 가짜로 바꿔서.
- 토픽이 없으면 보내지 않는다. 검토 끝(계획서)·실행 끝·실패는 run_headless에서, on_hold·in_review는 에이전트가 바꿀 때만.
- 헤드리스 실행 중의 in_review는 건너뛴다(실행 끝 알림과 겹침). ntfy가 예외를 던져도 본 작업은 정상 끝난다."""
import os, sys, tempfile, threading, time
from pathlib import Path

os.environ["DEV_DATA_DIR"] = tempfile.mkdtemp()
os.environ["NTFY_TOPIC"] = "test-topic"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import config, db, issues, notify, review  # noqa: E402

sent = []
notify._post = sent.append
human = {"kind": "human", "id": 1, "name": "admin", "model": None}
agent = {"kind": "agent", "id": 1, "name": "claude", "model": "m"}
db.init()
issues.create_project(human, "NS", "nightshift")
# 알림 성공 검사는 분류 호출이 없는 모의 검토이므로 사람이 종류를 선택한다.
issues.create_issue(human, "NS", "알림 받을 이슈", type_ids=[1])
issues.create_issue(human, "NS", "Task 부모")
task = issues.create_issue(human, "NS", "Task 하나", parent="NS-2")["ref"]


def wait(n):
    for _ in range(50):
        if len(sent) >= n:
            break
        time.sleep(0.05)
    time.sleep(0.1)
    assert len(sent) == n, sent
    return sent[-1]


def run(ref, label, code):
    log_path, run_id = review.begin(human, issues.get_issue(ref), "review" if label == "검토" else "execute")
    review.run_headless(human, ref, log_path, run_id, [sys.executable, "-c", f"import sys; sys.exit({code})"], None, None, 30, label)


run("NS-1", "검토", 0)
p = wait(1)
assert p["topic"] == "test-topic" and "계획서가 나왔어요" in p["title"] and "NS-1 알림 받을 이슈" in p["message"]
assert p["click"] == f"{config.PUBLIC_URL}/#/issue/NS-1" and p["priority"] == 3
run(task, "실행", 0)
assert "실행 완료" in wait(2)["title"] and wait(2)["click"].endswith(f"/#/issue/{task}")
run("NS-1", "실행", 3)
p = wait(3)
assert "끝나지 못했어요" in p["title"] and p["priority"] == 4 and "종료 코드 3" in p["message"]

# 상태 — 에이전트의 on_hold·in_review만, 사람은 보내지 않음
issues.set_status(agent, "NS-1", "on_hold", "토픽을 정해 주세요")
p = wait(4)
assert "답이 필요" in p["title"] and "토픽을 정해 주세요" in p["message"]
issues.set_status(agent, "NS-1", "in_review", "테스트로 확인")
assert "in_review" in wait(5)["title"]
issues.set_status(human, "NS-1", "on_hold")
issues.set_status(agent, "NS-1", "in_progress")
wait(5)
# 헤드리스 실행 중의 in_review는 건너뜀
review.begin(human, issues.get_issue(task), "execute")
issues.set_status(agent, task, "in_review")
wait(5)
with db.connect() as c:
    c.execute("UPDATE runs SET status='ok' WHERE status='running'")

# ntfy가 실패해도 본 작업은 끝남
def boom(_):
    raise OSError("ntfy down")
notify._post = boom
assert issues.set_status(agent, "NS-1", "on_hold")["status"] == "on_hold"
run("NS-1", "검토", 0)
assert review.list_runs("NS-1")[0]["status"] == "ok"

# 토픽이 없으면 보내지 않음
for t in threading.enumerate():   # 앞 단계의 알림 스레드가 끝난 뒤에 바꾼다
    if t is not threading.current_thread() and t.daemon:
        t.join(2)
notify._post = sent.append
config.NTFY_TOPIC = ""
issues.set_status(agent, "NS-1", "in_review")
wait(5)
print("OK")
