"""MCP — 실제 서버(uvicorn)를 띄워 fastmcp Client로 부른다. 키 없음/틀린 키/꺼진 키는 거절, 쿠키로는 못 부름,
도구로 이슈 흐름(목록·읽기·잡기·계획서·Task·커밋·in_review), done은 거절, 남의 본문 수정 거절, 이벤트에 모델명."""
import asyncio, os, socket, subprocess, sys, tempfile, time
from pathlib import Path

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError

ROOT = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp())
os.environ["DEV_DATA_DIR"] = str(tmp / "data")
sys.path.insert(0, str(ROOT / "server"))
import auth, db, issues, attachments  # noqa: E402

# 서버를 띄우기 전에 데이터를 넣어 둔다(같은 DB 파일).
db.init()
HUMAN = {"kind": "human", "id": 1, "name": "admin", "model": None}
issues.create_project(HUMAN, "NS", "nightshift")
issues.create_issue(HUMAN, "NS", "보드 복사", "사람이 쓴 지시")
samples = {}
for name, data in [('x.md', b'a' * (attachments.TEXT_PREVIEW_BYTES + 10)), ('x.json', b'{"ok":true}'), ('x.png', b'\x89PNG\r\n\x1a\n' + b'x'), ('large.png', b'\x89PNG\r\n\x1a\n' + b'x' * attachments.IMAGE_CONTENT_BYTES), ('x.wav', b'RIFF0000WAVEdata')]:
    samples[name] = attachments.upload(HUMAN, name, [data])
samples['url'] = attachments.add_url(HUMAN, 'https://example.com')
with db.connect() as conn:
    attachments.link(conn, HUMAN, issues.get_issue('NS-1')['id'], [a['id'] for a in samples.values()])
temporary = attachments.upload(HUMAN, 'temp.md', [b'temp'])
agent, key = auth.create_agent("claude", "anthropic", "claude-opus-5-5")
off, off_key = auth.create_agent("old", "", "")
with db.connect() as c:
    c.execute("UPDATE agents SET enabled=0 WHERE id=?", (off["id"],))

s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--port", str(port)], cwd=ROOT / "server",
                        env={**os.environ, "DEV_NIGHTSHIFT_URL": "http://127.0.0.1:9"}, stderr=subprocess.DEVNULL)
URL = f"http://127.0.0.1:{port}/mcp/"


def client(k):
    return Client(StreamableHttpTransport(URL, headers={"Authorization": f"Bearer {k}"} if k else {}))


async def main():
    for k in (None, "dev_wrong", off_key):
        try:
            async with client(k) as c:
                await c.list_tools()
            raise AssertionError(f"{k} should be rejected")
        except AssertionError:
            raise
        except Exception:
            pass
    async with httpx.AsyncClient() as h:   # 쿠키로는 안 됨
        r = await h.post(URL, cookies={"ns_session": "x"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 401, r.status_code

    async with client(key) as c:
        names = {t.name for t in await c.list_tools()}
        assert {"whoami", "list_issues", "get_issue", "claim_issue", "post_plan", "set_status", "add_comment",
                "link_commit", "create_issue", "update_issue", "release_issue", "list_projects", "list_project_documents", "read_project_document"} <= names, names
        call = lambda tool_name, **kw: c.call_tool(tool_name, kw)
        assert {'list_issue_types', 'create_issue_type', 'update_issue_type'} <= names
        types = (await call('list_issue_types')).structured_content['result']
        assert len(types) == 4
        for name, args in [('create_issue_type', {'name': '금지'}), ('update_issue_type', {'type_id': 1, 'active': False})]:
            try:
                await call(name, **args)
                raise AssertionError('catalog mutation allowed')
            except ToolError:
                pass
        typed = (await call('create_issue', project='NS', title='복수 종류', type_ids=[1, 3, 1])).data
        assert typed['type_ids'] == [1, 3] and all(t['source'] == 'agent' for t in typed['types'])
        cleared = (await call('update_issue', ref=typed['ref'], type_ids=[])).data
        assert cleared['type_ids'] == []
        try:
            await call('update_issue', ref=typed['ref'], type_ids=[99999])
            raise AssertionError('invalid type allowed')
        except ToolError:
            pass
        docs = (await call("list_project_documents", project="NS")).data
        assert len(docs["documents"]) == 7
        projects = (await call("list_projects")).structured_content["result"]
        assert "description" in projects[0]
        try:
            await call("read_project_document", project="NS", path="../secret")
            raise AssertionError("path escape allowed")
        except ToolError:
            pass
        assert (await call("whoami")).data["model"] == "claude-opus-5-5"
        lst = (await call("list_issues", status="backlog")).structured_content["result"]
        assert 'NS-1' in [i['ref'] for i in lst] and "body" not in lst[0]
        it = (await call("get_issue", ref="NS-1")).data
        assert it["body"] == "사람이 쓴 지시"
        assert len(it['attachments']) == len(samples)
        assert all('storage_key' not in a and 'owner' not in a for a in it['attachments'])
        import json, base64
        for name in ('x.md', 'x.json', 'x.wav', 'url', 'large.png'):
            result = await call('read_attachment', ref='NS-1', attachment_id=samples[name]['id'])
            info = json.loads(result.content[0].text)
            assert info['id'] == samples[name]['id'] and 'notice' in info
            if name == 'x.md':
                assert info['truncated'] and len(info['text']) == attachments.TEXT_PREVIEW_BYTES
            elif name == 'x.json':
                assert info['text'] == '{"ok":true}' and not info['truncated']
            else:
                assert 'content_omitted' in info and len(result.content) == 1
        result = await call('read_attachment', ref='NS-1', attachment_id=samples['x.png']['id'])
        assert result.content[1].type == 'image' and result.content[1].mimeType == 'image/png'
        assert base64.b64decode(result.content[1].data).startswith(b'\x89PNG')
        for ref, aid in [('NS-1', temporary['id']), ('NS-99', samples['x.png']['id']), ('NS-1', 'missing')]:
            try:
                await call('read_attachment', ref=ref, attachment_id=aid)
                raise AssertionError('invalid attachment read allowed')
            except ToolError:
                pass
        await call("claim_issue", ref="NS-1")
        assert (await call("post_plan", ref="NS-1", body="1. 메뉴")).data["version"] == 1
        task = (await call("create_issue", project="NS", title="Task: 메뉴", parent="NS-1")).data
        assert task["ref"] == "NS-1-1"
        await call("update_issue", ref="NS-1-1", title="Task: 우클릭 메뉴")
        try:
            await call("update_issue", ref="NS-1", body="바꿔치기"); raise AssertionError("body edit allowed")
        except ToolError as e:
            assert "사람이 쓴" in str(e)
        await call("set_status", ref="NS-1", status="in_progress")
        await call("link_commit", ref="NS-1", sha="1261ce1", repo="nightshift", message="복사")
        await call("add_comment", ref="NS-1", body="진행 중")
        try:
            await call("set_status", ref="NS-1", status="done"); raise AssertionError("done allowed")
        except ToolError as e:
            assert "사람" in str(e)
        r = (await call("set_status", ref="NS-1", status="in_review", note="보드에서 우클릭해 보세요")).data
        assert r["status"] == "in_review" and r["claimed_by"] is None
        try:
            await call("get_issue", ref="NS-99"); raise AssertionError("missing issue")
        except ToolError as e:
            assert "없는" in str(e)

    full = issues.get_issue("NS-1")
    assert all(e["actor"] == f"agent:{agent['id']}" and e["data"]["model"] == "claude-opus-5-5" for e in full["events"]), full["events"]
    assert [e["kind"] for e in full["events"]] == ["claim", "plan", "status", "commit", "comment", "status"]


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
