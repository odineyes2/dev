"""실행 1건의 토큰 사용 요약(DEV-133) — 무엇이 토큰을 키우는지 본다.

    python scripts/run_usage.py <run id | 로그 파일 이름>   (예: 812, NS-75-2-20261016-….log)
    python scripts/run_usage.py --last [N]                  최근 N건(기본 5)

원본은 review.save_events가 남긴 `data/reviews/<로그>.events.jsonl.gz`. 그 전의 실행에는 없다.
Claude: API 호출(턴)마다 문맥 크기와 캐시 읽기/쓰기/새 입력, 도구별 호출 수와 결과 크기.
Codex: 명령·MCP 호출별 수와 출력 크기, 턴 합계 사용량(호출별 문맥은 CLI가 주지 않는다).
"""
import collections
import gzip
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import config  # noqa: E402
import issues  # noqa: E402

LOGS = config.DATA_DIR / "reviews"


def events(log_name):
    path = LOGS / (Path(log_name).stem + ".events.jsonl.gz")
    if not path.is_file():
        return None
    out = []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def size(content):
    """도구 결과 크기(글자) — 문자열 또는 [{"type":"text","text":…}] 목록."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(c.get("text", "")) if isinstance(c, dict) else len(str(c)) for c in content)
    return len(json.dumps(content, ensure_ascii=False)) if content else 0


def claude(evs):
    calls, names, tools = {}, {}, collections.defaultdict(lambda: [0, 0])
    result = {}
    for e in evs:
        if e.get("type") == "assistant":
            m = e.get("message") or {}
            calls[m.get("id")] = m.get("usage") or {}   # 한 응답이 블록마다 여러 줄로 오므로 id로 하나만 센다
            for b in m.get("content") or []:
                if b.get("type") == "tool_use":
                    names[b["id"]] = b.get("name", "?")
                    tools[names[b["id"]]][0] += 1
        elif e.get("type") == "user":
            for b in (e.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    tools[names.get(b.get("tool_use_id"), "?")][1] += size(b.get("content"))
        elif e.get("type") == "result":
            result = e
    ctx = [u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0) for u in calls.values()]
    total = lambda k: sum(u.get(k, 0) for u in calls.values())
    print(f"  API 호출 {len(calls)}회 · 턴 {result.get('num_turns', '?')} · 비용 ${result.get('total_cost_usd') or 0:.2f}")
    if ctx:
        print(f"  문맥 크기: 첫 호출 {ctx[0]:,} → 최대 {max(ctx):,} 토큰")
    print(f"  입력 합계 {sum(ctx):,} = 캐시 읽기 {total('cache_read_input_tokens'):,} + 캐시 쓰기 {total('cache_creation_input_tokens'):,}"
          f" + 새 입력 {total('input_tokens'):,} · 출력 {total('output_tokens'):,}")
    return tools


def codex(evs):
    tools, usage = collections.defaultdict(lambda: [0, 0]), collections.Counter()
    for e in evs:
        item = e.get("item") or {}
        if e.get("type") == "item.completed":
            kind = item.get("type")
            if kind == "command_execution":
                words = item.get("command", "").replace('"', " ").split()
                key = "명령 " + next((w for w in words if w.lower() not in ("powershell", "powershell.exe", "pwsh", "-command", "-noprofile", "cmd", "/c", "bash", "-lc")), "?")
                tools[key][0] += 1
                tools[key][1] += len(item.get("aggregated_output") or "")
            elif kind == "mcp_tool_call":
                tools["MCP " + str(item.get("tool"))][0] += 1
            elif kind:
                tools[kind][0] += 1
        elif e.get("type") == "turn.completed":
            usage.update({k: v for k, v in (e.get("usage") or {}).items() if isinstance(v, int)})
    print(f"  입력 {usage['input_tokens']:,} (그중 캐시 {usage['cached_input_tokens']:,}) · 출력 {usage['output_tokens']:,}")
    return tools


def report(row):
    rid, ref, mode, provider, log, status = row
    print(f"#{rid} {ref} {provider} {mode} ({status}) — {log}")
    evs = events(log) if log else None
    if evs is None:
        print("  이벤트 기록 없음(DEV-133 이전 실행이거나 로그가 없다)\n")
        return
    tools = (codex if provider == "codex" else claude)(evs)
    for name, (n, chars) in sorted(tools.items(), key=lambda kv: -kv[1][1]):
        print(f"    {name:28} {n:4}회  결과 {chars:>10,}자")
    print()


def main(argv):
    c = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    q = (f"SELECT r.id, {issues.ref_sql('i', 'p')}, r.mode, r.provider, r.log_file, r.status "
         "FROM runs r JOIN issues i ON i.id=r.issue_id JOIN projects p ON p.id=i.project_id ")
    if not argv or argv[0] == "--last":
        rows = c.execute(q + "ORDER BY r.id DESC LIMIT ?", (int(argv[1]) if len(argv) > 1 else 5,)).fetchall()
    elif argv[0].isdigit():
        rows = c.execute(q + "WHERE r.id=?", (int(argv[0]),)).fetchall()
    else:
        rows = c.execute(q + "WHERE r.log_file=?", (Path(argv[0]).name,)).fetchall()
    for row in rows:
        report(row)


if __name__ == "__main__":
    main(sys.argv[1:])
