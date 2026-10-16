"""실행 이벤트 기록(DEV-133) — stream-json에서 결과 줄을 뽑아 기존 처리에 넘기고, 원본은 gzip으로 이어 붙이며, 요약 스크립트가 읽는다."""
import gzip, io, json, os, sys, tempfile
from contextlib import redirect_stdout
from pathlib import Path

os.environ["DEV_DATA_DIR"] = tempfile.mkdtemp()
ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "server"), str(ROOT / "scripts")]
import review, run_usage  # noqa: E402

usage = {"input_tokens": 5, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 20000, "output_tokens": 50}
stream = "\n".join(json.dumps(e) for e in [
    {"type": "system", "subtype": "init"},
    {"type": "assistant", "message": {"id": "m1", "usage": usage, "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]}},
    {"type": "assistant", "message": {"id": "m1", "usage": usage, "content": [{"type": "text", "text": "같은 응답의 둘째 블록"}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x" * 1234}]}},
    {"type": "result", "result": "끝", "num_turns": 2, "total_cost_usd": 0.12, "usage": usage},
])

# 기존 처리(_parse)는 결과 줄만 보면 예전 json 출력과 같게 동작한다
text, stats = review._parse(review.claude_result(stream))
assert text == "끝" and stats["input_tokens"] == 21005 and stats["cost_usd"] == 0.12
assert review.claude_result('{"result": "옛 json"}') == '{"result": "옛 json"}'   # result 줄이 없으면 원문

log = review.LOG_DIR / "NS-1-x.log"
review.LOG_DIR.mkdir(parents=True, exist_ok=True)
review.save_events(log, stream)
review.save_events(log, stream)   # 재시도는 이어 붙인다
with gzip.open(review.events_path(log), "rt", encoding="utf-8") as f:
    assert sum(1 for _ in f) == 10

buf = io.StringIO()
with redirect_stdout(buf):
    tools = run_usage.claude(run_usage.events(log.name)[:5])
out = buf.getvalue()
assert "API 호출 1회" in out and "캐시 읽기 20,000" in out, out   # 같은 id의 두 줄은 한 번만 센다
assert tools["Read"] == [1, 1234]
print("OK run usage")
