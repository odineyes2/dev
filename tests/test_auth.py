"""로그인과 에이전트 키 — 가짜 nightshift(httpx MockTransport)로.
- 쿠키 없음 → 401, 일반 회원 → 403, admin → 통과. 쿠키로 오는 쓰기 요청은 X-Requested-With: dev가 있어야.
- 로그인은 nightshift로 넘기고 Set-Cookie를 그대로 전한다.
- 에이전트 키: 발급 때 한 번만, DB엔 해시만, 끄면 401, 재발급하면 옛 키 401. 에이전트는 에이전트 관리 불가."""
import os, sys, tempfile
from pathlib import Path

os.environ["DEV_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as A, auth, db  # noqa: E402

USERS = {"adm": {"id": 1, "username": "admin", "role": "admin"}, "mem": {"id": 2, "username": "bob", "role": "user"}}
seen = []


def fake_nightshift(req: httpx.Request):
    seen.append((req.method, req.url.path, dict(req.headers)))
    if req.url.path == "/api/auth/me":
        token = req.headers.get("cookie", "").split("=", 1)[-1]
        return httpx.Response(200, json={"user": USERS.get(token)})
    if req.url.path == "/api/auth/login":
        import json
        b = json.loads(req.content)
        if b["password"] != "pw":
            return httpx.Response(401, json={"detail": "아이디 또는 비밀번호가 올바르지 않아요."})
        return httpx.Response(200, json={"user": USERS["adm"]}, headers={"set-cookie": "ns_session=adm; Domain=lomebrote.com; Path=/; HttpOnly"})
    if req.url.path == "/api/auth/logout":
        return httpx.Response(200, json={}, headers={"set-cookie": "ns_session=; Max-Age=0; Path=/"})
    return httpx.Response(404)


auth._client = httpx.AsyncClient(transport=httpx.MockTransport(fake_nightshift))
H = {"X-Requested-With": "dev"}

with TestClient(A.app) as c:
    assert c.get("/api/health").json() == {"ok": True}
    assert c.get("/api/agents").status_code == 401
    assert c.get("/api/auth/me").json()["reason"] == "anon"
    assert c.get("/api/agents", cookies={"ns_session": "mem"}).status_code == 403
    assert c.get("/api/auth/me", cookies={"ns_session": "mem"}).json()["reason"] == "not_admin"
    c.cookies.set("ns_session", "adm")
    me = c.get("/api/auth/me").json()["actor"]
    assert me["kind"] == "human" and me["name"] == "admin", me
    assert c.get("/api/agents").json() == {"agents": []}
    # CSRF: 쿠키로 오는 쓰기 요청은 헤더 필요
    assert c.post("/api/agents", json={"name": "claude"}).status_code == 403
    r = c.post("/api/agents", json={"name": "claude", "vendor": "anthropic", "model": "claude-opus-5-5"}, headers=H).json()
    key, agent = r["key"], r["agent"]
    assert key.startswith("dev_") and "key_hash" not in agent and key.startswith(agent["key_prefix"])
    with db.connect() as conn:
        assert key not in str([tuple(x) for x in conn.execute("SELECT * FROM agents")])   # 키 원문은 DB에 없다
    assert c.post("/api/agents", json={"name": "claude"}, headers=H).status_code == 409
    c.cookies.clear()

    bearer = {"Authorization": f"Bearer {key}"}
    me = c.get("/api/auth/me", headers=bearer).json()["actor"]
    assert me == {"kind": "agent", "id": agent["id"], "name": "claude", "model": "claude-opus-5-5"}, me
    assert c.get("/api/agents", headers=bearer).status_code == 403            # 에이전트는 에이전트 관리 불가
    assert c.post("/api/agents", json={"name": "x"}, headers=bearer).status_code == 403   # CSRF 헤더 없이도 bearer는 통과, 권한에서 막힘
    assert c.get("/api/agents", headers={"Authorization": "Bearer dev_wrong"}).status_code == 401
    with db.connect() as conn:
        assert conn.execute("SELECT last_seen_at FROM agents").fetchone()[0]

    c.cookies.set("ns_session", "adm")
    assert c.patch(f"/api/agents/{agent['id']}", json={"enabled": False}, headers=H).json()["agent"]["enabled"] is False
    c.cookies.clear()
    assert c.get("/api/auth/me", headers=bearer).json()["actor"] is None
    c.cookies.set("ns_session", "adm")
    c.patch(f"/api/agents/{agent['id']}", json={"enabled": True}, headers=H)
    new_key = c.post(f"/api/agents/{agent['id']}/rotate", headers=H).json()["key"]
    c.cookies.clear()
    assert c.get("/api/auth/me", headers=bearer).json()["actor"] is None           # 옛 키는 끝
    assert c.get("/api/auth/me", headers={"Authorization": f"Bearer {new_key}"}).json()["actor"]["name"] == "claude"

    # 로그인 넘기기
    assert c.post("/api/auth/login", json={"username": "admin", "password": "x"}, headers=H).status_code == 401
    r = c.post("/api/auth/login", json={"username": "admin", "password": "pw"}, headers={**H, "CF-Connecting-IP": "1.2.3.4"})
    assert r.status_code == 200 and r.json()["ok"] and "Domain=lomebrote.com" in r.headers["set-cookie"], r.headers
    login_req = [s for s in seen if s[1] == "/api/auth/login"][-1]
    assert login_req[2]["x-forwarded-for"] == "1.2.3.4" and login_req[2]["x-requested-with"] == "nightshift"
    assert c.post("/api/auth/login", json={"username": "admin", "password": "pw"}).status_code == 403   # CSRF
    r = c.post("/api/auth/logout", headers=H)
    assert r.status_code == 204 and "Max-Age=0" in r.headers["set-cookie"]
print("OK")

# 화면 파일은 no-cache — Cloudflare가 브라우저 캐시 4시간을 붙이지 않게(DEV-1: 고친 CSS가 안 보이던 문제)
with TestClient(A.app) as c:
    for path in ("/", "/app.css", "/app.js"):
        r = c.get(path)
        assert r.status_code == 200 and r.headers.get("cache-control") == "no-cache", (path, r.headers)
    etag = c.get("/app.css").headers["etag"]
    assert c.get("/app.css", headers={"If-None-Match": etag}).status_code == 304   # 안 바뀌었으면 다시 받지 않는다
print("OK static")

# index.html의 CSS/JS 주소에 내용 해시 — 파일이 바뀌면 주소가 바뀐다
import hashlib, re
with TestClient(A.app) as c:
    html = c.get("/").text
    for name in ("app.css", "app.js"):
        v = hashlib.sha1((A.STATIC_DIR / name).read_bytes()).hexdigest()[:10]
        assert f'"/{name}?v={v}"' in html, (name, re.findall(r'/app\.\w+[^"]*', html))
    assert c.get("/index.html").text == html and c.get("/").headers["cache-control"] == "no-cache"
print("OK version")
