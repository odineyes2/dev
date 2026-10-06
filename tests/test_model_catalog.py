"""모델 카탈로그 — 형식·vendor→provider 매핑, 실제 서버(uvicorn)의 GET /api/model-catalog와 MCP list_models가 같은 목록을 준다."""
import asyncio, os, socket, subprocess, sys, tempfile, time
from pathlib import Path

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

ROOT = Path(__file__).resolve().parent.parent
os.environ["DEV_DATA_DIR"] = str(Path(tempfile.mkdtemp()) / "data")
sys.path.insert(0, str(ROOT / "server"))
import auth, db, model_catalog  # noqa: E402

cat = model_catalog.catalog()
assert cat["providers"] == {"anthropic": "claude", "openai": "codex"}, cat["providers"]
ids = {v["vendor"]: [m["id"] for m in v["models"]] for v in cat["vendors"]}
assert ids["anthropic"] == ["claude-fable-5-1", "claude-fable-5", "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8",
                            "claude-sonnet-5-5", "claude-sonnet-5", "claude-sonnet-4-6", "claude-haiku-4-5"], ids
assert ids["openai"] == ["gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna"], ids
for v in cat["vendors"]:
    assert v["name"] and v["provider"] in ("claude", "codex")
    assert all(set(m) == {"id", "name", "note"} and m["id"] and m["name"] for m in v["models"]), v
all_ids = sum(ids.values(), [])
assert len(all_ids) == len(set(all_ids))
assert not {"claude-mythos-5-1", "claude-mythos-5", "claude-opus-4-1"} & set(all_ids)

db.init()
agent, key = auth.create_agent("claude", "anthropic", "claude-opus-5-5")

# DEV-89-5: 착수 이벤트·프롬프트에 모델, 도는 run이 있으면 에이전트 이벤트 model은 runs.model
import issues, review  # noqa: E402
HUMAN = {"kind": "human", "id": 1, "name": "admin", "model": None}
AGENT = {"kind": "agent", "id": agent["id"], "name": "claude", "model": "claude-opus-5-5"}
issues.create_project(HUMAN, "MC", "model catalog")
it = issues.create_issue(HUMAN, "MC", "model run")
_, rid = review.begin(HUMAN, it, "execute", "claude", "claude-sonnet-5-5")
ev = issues.get_issue(it["ref"])["events"]
assert ev[-1]["kind"] == "status" and ev[-1]["data"]["model"] == "claude-sonnet-5-5", ev[-1]
issues.claim(AGENT, it["ref"])
assert issues.get_issue(it["ref"])["events"][-1]["data"]["model"] == "claude-sonnet-5-5"   # Agent 행(opus)이 아니라 실제 실행 모델
assert review.list_runs(it["ref"])[0]["model"] == "claude-sonnet-5-5"
with db.connect() as c:
    c.execute("UPDATE runs SET status='ok' WHERE id=?", (rid,))
issues.release(AGENT, it["ref"])
assert issues.get_issue(it["ref"])["events"][-1]["data"]["model"] == "claude-opus-5-5"   # 도는 실행이 없으면 Agent 행 값
assert "이 실행의 모델: gpt-6-sol" in review.with_model(["codex", "exec", "P"], "codex", "gpt-6-sol")[-1]

s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--port", str(port)], cwd=ROOT / "server",
                        env={**os.environ, "DEV_NIGHTSHIFT_URL": "http://127.0.0.1:9"}, stderr=subprocess.DEVNULL)
BASE = f"http://127.0.0.1:{port}"
H = {"Authorization": f"Bearer {key}"}


async def main():
    async with httpx.AsyncClient() as h:
        r = await h.get(f"{BASE}/api/model-catalog")
        assert r.status_code == 401, r.status_code   # 로그인·키 없으면 못 본다
        r = await h.get(f"{BASE}/api/model-catalog", headers=H)
        assert r.status_code == 200 and r.json() == cat, r.text
    async with Client(StreamableHttpTransport(f"{BASE}/mcp/", headers=H)) as c:
        assert "list_models" in {t.name for t in await c.list_tools()}
        assert (await c.call_tool("list_models", {})).structured_content == cat
        withp = (await c.call_tool("list_models", {"project": "MC"})).structured_content
        assert withp["vendors"] == cat["vendors"] and withp["agent_orders"]["provider_order"][0]["model"] == "claude-opus-5-5", withp


try:
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close(); break
        except OSError:
            time.sleep(0.1)
    asyncio.run(main())
    print("OK")
finally:
    proc.terminate()
