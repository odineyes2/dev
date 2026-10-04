"""dev — 코딩 에이전트용 이슈 게시판(dev.lomebrote.com). 사람은 화면, 에이전트는 REST/MCP로 쓴다."""
import sqlite3
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

import auth
import config
import db
import timing
from mcp_tools import mcp_app


@asynccontextmanager
async def lifespan(app):
    db.init()
    jobs.start_timer()   # 남은 대기열을 이어서 돌리고 60초마다 펌프(DEV-43)
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
    with timing.span("auth"):
        actor, reason = await _resolve_actor(request)
    request.state.actor = actor
    request.state.reason = reason
    if actor is None and path not in PUBLIC_API:
        if reason == "not_admin":
            return JSONResponse({"detail": "nightshift 관리자 계정만 쓸 수 있어요."}, status_code=403)
        return JSONResponse({"detail": "로그인이 필요해요." if reason == "anon" else "API 키가 맞지 않아요."}, status_code=401)
    return await call_next(request)


@app.middleware("http")   # 나중에 붙인 미들웨어가 바깥 — 인증까지 포함해 잰다
async def server_timing(request: Request, call_next):
    """요청마다 Server-Timing 헤더(구간별 ms + total)를 붙이고 느린 요청은 timing.log에(DEV-42)."""
    spans = timing.begin()
    t = time.perf_counter()
    response = await call_next(request)
    total = (time.perf_counter() - t) * 1000
    value = timing.header(spans, total)
    response.headers["Server-Timing"] = value
    timing.log_slow(request.method, request.url.path, response.status_code, value, total)
    return response


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
    return issues.create_project(actor(request), b.get("key"), b.get("name"), b.get("repo_url", ""), b.get("local_path", ""), b.get("description", ""))


@app.patch("/api/projects/{key}")
async def api_update_project(key: str, request: Request):
    return issues.update_project(actor(request), key.upper(), await json_body(request))


@app.get("/api/projects/{key}/delete-check")
def api_project_delete_check(key: str, request: Request):
    return issues.check_project_deletion(actor(request), key)


@app.delete("/api/projects/{key}")
async def api_delete_project(key: str, request: Request):
    b = await json_body(request) if await request.body() else {}
    issues.delete_project(actor(request), key, b.get("confirmation_token"))
    return Response(status_code=204)


@app.get("/api/issues")
def api_issues(project: str = "", status: str = "", assignee: int | None = None, parent: str = "", q: str = "",
               limit: int = 500, offset: int = 0, approved: bool = False):
    """offset부터 limit개 — 하나 더 읽어 보고 뒤에 더 있는지(has_more)를 알려 준다(화면의 나눠 읽기)."""
    limit = max(1, min(limit, 2000))
    items = issues.list_issues(project or None, status or None, assignee, parent or None, q or None, limit + 1, offset,
                               approved=approved)
    return {"issues": items[:limit], "has_more": len(items) > limit}

@app.post("/api/issues")
async def api_create_issue(request: Request):
    b = await json_body(request)
    a = actor(request)
    it = issues.create_issue(a, b.get("project"), b.get("title"), b.get("body", ""), b.get("priority", "none"),
                             b.get("labels"), b.get("parent"), b.get("status", "backlog"))
    return it


@app.get("/api/issues/{ref}")
def api_issue(ref: str):
    with timing.span("issue"):
        it = issues.get_issue(ref)
    with timing.span("review"):
        it["review_running"] = review.running_ref() == it["ref"]   # "Claude에게 검토 맡기기"가 도는 중(DEV-13)
        it["review_busy"] = review.running_ref() is not None
    with timing.span("execute"):
        it["execute"] = execute.panel(it)   # Task의 "Claude에게 실행 맡기기"(DEV-23)
    with timing.span("job"):
        it["job"] = jobs.job_for(it["id"])   # 대기열 자리(DEV-43)
    with timing.span("merge"):
        it["merge_state"] = orchestrate.merge_state(it)   # 병합 대기·병합됨·재시작 대기·되돌림(DEV-40-3)
    with timing.span("children"):
        evs = issues.events_for(ch["id"] for ch in it["children"])   # Task마다 다시 읽지 않고 한 번에(DEV-42-2)
        cfg = orchestrate.settings(it["project_key"]) if it["children"] else None
        queued = {j["issue_id"]: j for j in jobs.list_jobs() if j["mode"] == "execute"} if it["children"] else {}
        for ch in it["children"]:
            ch["merge_state"] = orchestrate.merge_state({**ch, "events": evs[ch["id"]]}, cfg)
            ch["job"] = queued.get(ch["id"])   # 줄에 선 Task는 표에서 "실행 대기 중"(DEV-45)
    return it


@app.get("/api/issues/{ref}/runs")
def api_runs(ref: str):
    return {"runs": review.list_runs(ref)}


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


@app.post("/api/issues/{ref}/complete-tree")
async def api_complete_tree(ref: str, request: Request):
    b = await json_body(request)
    return {"issues": issues.complete_tree(actor(request), ref, b.get("note", ""))}


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


# ---- Claude에게 검토 맡기기(DEV-13) — 홈서버에서 claude -p로 검토만 ----
import review  # noqa: E402
import execute  # noqa: E402
import orchestrate  # noqa: E402


import jobs  # noqa: E402


@app.post("/api/issues/{ref}/execute")
async def api_execute(ref: str, request: Request):
    me = actor(request)
    b = await json_body(request) if await request.body() else {}
    return jobs.enqueue(me, ref, "execute", b.get("provider", "claude"))


@app.post("/api/issues/{ref}/review")
async def api_review(ref: str, request: Request):
    me = actor(request)
    b = await json_body(request) if await request.body() else {}
    return jobs.enqueue(me, ref, "review", b.get("provider", "claude"))


@app.get("/api/jobs")
def api_jobs():
    return {"jobs": jobs.list_jobs()}


@app.delete("/api/jobs/{job_id}")
def api_cancel_job(job_id: int, request: Request):
    return jobs.cancel(actor(request), job_id)


@app.post("/api/issues/{ref}/decision")
async def api_decision(ref: str, request: Request):
    b = await json_body(request)
    running = review.running_ref()
    if running and issues.get_issue(ref)["ref"] == running:
        raise issues.StoreError("검토가 돌고 있어요 — 끝나면 결정해 주세요.", 409)
    return issues.decide(actor(request), ref, b.get("verdict"), b.get("note", ""), b.get("plan_version"))


# 화면 — API 라우트 뒤에 붙여야 /api가 가려지지 않는다(마운트는 반드시 마지막).
import hashlib  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

STATIC_DIR = config.REPO_ROOT / "static"


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def index_page():
    """index.html의 app.css/app.js 주소에 내용 해시(?v=)를 붙인다. Cloudflare가 CSS/JS에 브라우저 캐시 4시간을
    덮어씌워서(no-cache를 보내도) 고친 파일이 안 보이던 문제 — 파일이 바뀌면 주소가 바뀌어 새로 받는다.
    HTML은 Cloudflare가 no-cache를 그대로 둔다."""
    html = (STATIC_DIR / "index.html").read_text("utf-8")
    for name in ("app.css", "app.js"):
        v = hashlib.sha1((STATIC_DIR / name).read_bytes()).hexdigest()[:10]
        html = html.replace(f'"/{name}"', f'"/{name}?v={v}"')
    return HTMLResponse(html)


app.mount("/mcp", mcp_app)
app.mount("/", StaticFiles(directory=config.REPO_ROOT / "static", html=True), name="static")
