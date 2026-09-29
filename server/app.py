"""dev — 코딩 에이전트용 이슈 게시판(dev.lomebrote.com). 사람은 화면, 에이전트는 REST/MCP로 쓴다."""
import sqlite3
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

import auth
import config
import db
from mcp_tools import mcp_app


@asynccontextmanager
async def lifespan(app):
    db.init()
    async with mcp_app.lifespan(app):   # MCP(streamable HTTP)의 세션 관리자도 같이 띄운다
        yield


app = FastAPI(title="dev", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

PUBLIC_API = {"/api/health", "/api/auth/me", "/api/auth/login", "/api/auth/logout"}
CSRF_HEADER, CSRF_VALUE = "x-requested-with", "dev"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def _client_ip(request: Request) -> str:
    return (request.headers.get("cf-connecting-ip")
            or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else ""))


async def _resolve_actor(request: Request) -> tuple[dict | None, str]:
    """(actor, 이유) — 이유는 actor가 없을 때 "anon" | "not_admin" | "bad_key"."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        agent = auth.agent_for_key(header[7:].strip())
        if agent is None:
            return None, "bad_key"
        return {"kind": "agent", "id": agent["id"], "name": agent["name"], "model": agent["model"]}, ""
    user = await auth.nightshift_user(request.cookies.get(auth.SESSION_COOKIE, ""))
    if user is None:
        return None, "anon"
    if user.get("role") != "admin":
        return None, "not_admin"
    return {"kind": "human", "id": user.get("id"), "name": user.get("username") or "admin", "model": None}, ""


@app.middleware("http")
async def authenticate(request: Request, call_next):
    path = request.url.path
    request.state.actor = None
    bearer = request.headers.get("authorization", "").lower().startswith("bearer ")
    if path == "/mcp" or path.startswith("/mcp/"):
        # MCP는 에이전트 키만 — 쿠키로는 받지 않는다(다른 사이트가 브라우저 쿠키로 도구를 부르지 못하게).
        actor, _ = await _resolve_actor(request) if bearer else (None, "")
        if actor is None:
            return JSONResponse({"detail": "에이전트 키(Authorization: Bearer dev_…)가 필요해요."}, status_code=401)
        request.state.actor = actor
        if path == "/mcp":
            request.scope["path"] = "/mcp/"   # 끝 슬래시 없이 등록한 클라이언트도 그대로 되게
        return await call_next(request)
    if not path.startswith("/api/"):
        # 화면 파일은 매번 새 판인지 확인하게(ETag로 304) — 캐시 헤더가 없으면 Cloudflare가 브라우저 캐시 4시간을
        # 붙여서, 고친 CSS/JS가 한참 안 보인다.
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-cache"
        return response
    # 쿠키로 들어오는 쓰기 요청은 우리 화면이 보낸 것만(CSRF) — 다른 사이트의 폼은 이 헤더를 못 붙인다.
    if request.method in UNSAFE and not bearer and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        return JSONResponse({"detail": "요청 헤더가 맞지 않아요."}, status_code=403)
    actor, reason = await _resolve_actor(request)
    request.state.actor = actor
    request.state.reason = reason
    if actor is None and path not in PUBLIC_API:
        if reason == "not_admin":
            return JSONResponse({"detail": "nightshift 관리자 계정만 쓸 수 있어요."}, status_code=403)
        return JSONResponse({"detail": "로그인이 필요해요." if reason == "anon" else "API 키가 맞지 않아요."}, status_code=401)
    return await call_next(request)


def actor(request: Request) -> dict:
    return request.state.actor


def human_only(request: Request) -> dict:
    a = request.state.actor
    if not a or a["kind"] != "human":
        raise HTTPException(403, "사람(관리자)만 할 수 있어요.")
    return a


async def json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "JSON 본문이 필요해요.")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON 객체가 필요해요.")
    return body


@app.get("/api/health")
def health():
    return {"ok": True}


# ---- 로그인 — nightshift에 그대로 넘긴다 ----
@app.get("/api/auth/me")
def auth_me(request: Request):
    return {"actor": request.state.actor, "reason": getattr(request.state, "reason", ""),
            "nightshift_url": config.NIGHTSHIFT_PUBLIC_URL}


@app.post("/api/auth/login")
async def auth_login(request: Request):
    body = await json_body(request)
    try:
        resp = await auth._client.post(
            f"{config.NIGHTSHIFT_URL}/api/auth/login",
            json={"username": str(body.get("username") or ""), "password": str(body.get("password") or "")},
            headers={"X-Requested-With": "nightshift", "X-Forwarded-For": _client_ip(request),
                     "X-Forwarded-Proto": request.headers.get("x-forwarded-proto", request.url.scheme)},
        )
    except httpx.HTTPError:
        raise HTTPException(502, "nightshift 서버에 연결하지 못했어요.")
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code != 200:
        raise HTTPException(401 if resp.status_code < 500 else 502, data.get("detail") or "로그인에 실패했어요.")
    user = data.get("user") or {}
    out = JSONResponse({"ok": user.get("role") == "admin",
                        "detail": "" if user.get("role") == "admin" else "nightshift 관리자 계정만 쓸 수 있어요."})
    for k, v in resp.headers.multi_items():
        if k.lower() == "set-cookie":
            out.headers.append("set-cookie", v)
    return out


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    cookie = request.cookies.get(auth.SESSION_COOKIE, "")
    auth.forget_cookie(cookie)
    try:
        resp = await auth._client.post(f"{config.NIGHTSHIFT_URL}/api/auth/logout",
                                       headers={"Cookie": f"{auth.SESSION_COOKIE}={cookie}", "X-Requested-With": "nightshift"})
    except httpx.HTTPError:
        raise HTTPException(502, "nightshift 서버에 연결하지 못했어요.")
    out = Response(status_code=204)
    for k, v in resp.headers.multi_items():
        if k.lower() == "set-cookie":
            out.headers.append("set-cookie", v)
    return out


# ---- 에이전트 관리(사람만) ----
def _agent_fields(body: dict) -> dict:
    out = {}
    for k in ("name", "vendor", "model"):
        if k in body:
            v = str(body[k] or "").strip()
            if k == "name" and not v:
                raise HTTPException(400, "이름이 필요해요.")
            if len(v) > 100:
                raise HTTPException(400, f"{k}가 너무 길어요.")
            out[k] = v
    return out


@app.get("/api/agents")
def list_agents(request: Request):
    human_only(request)
    with db.connect() as c:
        return {"agents": [auth.public_agent(r) for r in c.execute("SELECT * FROM agents ORDER BY id")]}


@app.post("/api/agents")
async def create_agent(request: Request):
    human_only(request)
    f = _agent_fields(await json_body(request))
    if "name" not in f:
        raise HTTPException(400, "이름이 필요해요.")
    try:
        agent, key = auth.create_agent(f["name"], f.get("vendor", ""), f.get("model", ""))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "같은 이름의 에이전트가 있어요.")
    return {"agent": agent, "key": key}


@app.patch("/api/agents/{agent_id}")
async def update_agent(agent_id: int, request: Request):
    human_only(request)
    body = await json_body(request)
    f = _agent_fields(body)
    if "enabled" in body:
        f["enabled"] = 1 if body["enabled"] else 0
    if not f:
        raise HTTPException(400, "바꿀 내용이 없어요.")
    try:
        with db.connect() as c:
            n = c.execute(f"UPDATE agents SET {', '.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), agent_id)).rowcount
            row = c.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
    except sqlite3.IntegrityError:
        raise HTTPException(409, "같은 이름의 에이전트가 있어요.")
    if not n:
        raise HTTPException(404, "없는 에이전트예요.")
    return {"agent": auth.public_agent(row)}


@app.post("/api/agents/{agent_id}/rotate")
def rotate_agent_key(agent_id: int, request: Request):
    human_only(request)
    key = auth.rotate_key(agent_id)
    if key is None:
        raise HTTPException(404, "없는 에이전트예요.")
    return {"key": key}


# ---- 프로젝트·이슈 (권한 판단은 issues 모듈이 한다) ----
import issues  # noqa: E402


@app.exception_handler(issues.StoreError)
async def _store_error(request: Request, e: issues.StoreError):
    return JSONResponse({"detail": str(e)}, status_code=e.status)


@app.get("/api/projects")
def api_projects():
    return {"projects": issues.list_projects()}


@app.post("/api/projects")
async def api_create_project(request: Request):
    b = await json_body(request)
    return issues.create_project(actor(request), b.get("key"), b.get("name"), b.get("repo_url", ""), b.get("local_path", ""))


@app.patch("/api/projects/{key}")
async def api_update_project(key: str, request: Request):
    return issues.update_project(actor(request), key.upper(), await json_body(request))


@app.get("/api/issues")
def api_issues(project: str = "", status: str = "", assignee: int | None = None, parent: str = "", q: str = "", limit: int = 500):
    return {"issues": issues.list_issues(project or None, status or None, assignee, parent or None, q or None, limit)}


@app.post("/api/issues")
async def api_create_issue(request: Request):
    b = await json_body(request)
    return issues.create_issue(actor(request), b.get("project"), b.get("title"), b.get("body", ""), b.get("priority", "none"),
                               b.get("labels"), b.get("parent"), b.get("status", "backlog"))


@app.get("/api/issues/{ref}")
def api_issue(ref: str):
    return issues.get_issue(ref)


@app.patch("/api/issues/{ref}")
async def api_update_issue(ref: str, request: Request):
    return issues.update_issue(actor(request), ref, await json_body(request))


@app.delete("/api/issues/{ref}", status_code=204)
def api_delete_issue(ref: str, request: Request):
    issues.delete_issue(actor(request), ref)


@app.post("/api/issues/{ref}/status")
async def api_set_status(ref: str, request: Request):
    b = await json_body(request)
    return issues.set_status(actor(request), ref, b.get("status"), b.get("note", ""))


@app.get("/api/issues/{ref}/plans")
def api_plans(ref: str):
    return {"plans": issues.list_plans(ref)}


@app.post("/api/issues/{ref}/plans")
async def api_post_plan(ref: str, request: Request):
    return issues.post_plan(actor(request), ref, (await json_body(request)).get("body"))


@app.post("/api/issues/{ref}/comments")
async def api_comment(ref: str, request: Request):
    return issues.add_comment(actor(request), ref, (await json_body(request)).get("body"))


@app.post("/api/issues/{ref}/commits")
async def api_commit(ref: str, request: Request):
    b = await json_body(request)
    return issues.link_commit(actor(request), ref, b.get("sha"), b.get("repo", ""), b.get("message", ""))


@app.post("/api/issues/{ref}/claim")
async def api_claim(ref: str, request: Request):
    b = await json_body(request) if await request.body() else {}
    return issues.claim(actor(request), ref, b.get("minutes", 30))


@app.post("/api/issues/{ref}/release")
def api_release(ref: str, request: Request):
    return issues.release(actor(request), ref)


# 화면 — API 라우트 뒤에 붙여야 /api가 가려지지 않는다(마운트는 반드시 마지막).
from fastapi.staticfiles import StaticFiles  # noqa: E402

app.mount("/mcp", mcp_app)
app.mount("/", StaticFiles(directory=config.REPO_ROOT / "static", html=True), name="static")
