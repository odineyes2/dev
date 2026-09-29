"""화면 — 임시 데이터 폴더로 dev 서버와 가짜 nightshift를 띄우고 브라우저로 돈다.
로그인 폼 → 프로젝트 만들기 → 새 이슈 → 상세(계획서·댓글·상태) → 목록·칸반(끌어서 상태) → 에이전트 키 발급 → 모바일 폭."""
import os, socket, subprocess, sys, tempfile, time
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
FAKE_NS = r'''
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
app = FastAPI()
@app.get("/api/auth/me")
def me(request: Request):
    return {"user": {"id": 1, "username": "admin", "role": "admin"} if request.cookies.get("ns_session") == "adm" else None}
@app.post("/api/auth/login")
async def login(request: Request):
    b = await request.json()
    if b.get("password") != "pw":
        return JSONResponse({"detail": "아이디 또는 비밀번호가 올바르지 않아요."}, 401)
    r = JSONResponse({"user": {"id": 1, "username": "admin", "role": "admin"}})
    r.set_cookie("ns_session", "adm", path="/")
    return r
@app.post("/api/auth/logout")
def logout():
    r = JSONResponse({}); r.delete_cookie("ns_session", path="/"); return r
'''


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def wait_port(port):
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close(); return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"port {port} not up")


tmp = Path(tempfile.mkdtemp())
(tmp / "fake_ns.py").write_text(FAKE_NS, "utf-8")
ns_port, dev_port = free_port(), free_port()
env = {**os.environ, "DEV_DATA_DIR": str(tmp / "data"), "DEV_NIGHTSHIFT_URL": f"http://127.0.0.1:{ns_port}"}
procs = [subprocess.Popen([sys.executable, "-m", "uvicorn", "fake_ns:app", "--port", str(ns_port)], cwd=tmp, stderr=subprocess.DEVNULL),
         subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--port", str(dev_port)], cwd=ROOT / "server", env=env, stderr=subprocess.DEVNULL)]
BASE = f"http://127.0.0.1:{dev_port}"
shots = ROOT / "tests" / "shots"; shots.mkdir(exist_ok=True)
try:
    wait_port(ns_port); wait_port(dev_port)
    with sync_playwright() as p:
        page = p.chromium.launch().new_page(viewport={"width": 1300, "height": 850})
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.on("dialog", lambda d: d.accept("모바일도 확인해 주세요") if d.type == "prompt" else d.accept())
        page.goto(BASE)
        page.wait_for_selector("#login-form")
        page.fill("#login-username", "admin"); page.fill("#login-password", "nope"); page.click("#login-form button")
        page.wait_for_function("document.getElementById('login-error').textContent.includes('올바르지')")
        page.fill("#login-password", "pw"); page.click("#login-form button")
        page.wait_for_selector("#shell:not([hidden])")

        # 프로젝트 없이 New issue → 안내
        page.click("text=New issue")
        page.wait_for_selector("text=먼저")
        page.goto(BASE + "/#/projects")
        page.fill("#p-key", "NS"); page.fill("#p-name", "nightshift"); page.click("#project-form button")
        page.wait_for_selector("td.ref:text('NS')")

        page.click("text=New issue")
        page.fill("#n-title", "보드 카드 복사")
        page.fill("#n-body", "## 요구\n- 우클릭 메뉴\n- `Ctrl+C`\n\n<script>alert(1)</script> NS-1 참고")
        page.select_option("#n-priority", "high")
        page.fill("#n-labels", "board, ui")
        page.click("#new-form button[type=submit]")
        page.wait_for_selector("h1#title:text('보드 카드 복사')")
        assert page.url.endswith("#/issue/NS-1")
        body_html = page.inner_html("#body")
        assert "<h2>요구</h2>" in body_html and "<code>Ctrl+C</code>" in body_html and "<script>" not in body_html and "&lt;script&gt;" in body_html

        # 계획서 쓰기 → v1, 댓글, 상태
        page.click("#edit-plan"); page.fill("#plan textarea", "1. 메뉴\n2. 붙여넣기"); page.click("#plan .save")
        page.wait_for_selector("text=v1")
        page.fill("#comment", "시작할게요"); page.click("#send-comment")
        page.wait_for_selector(".timeline >> text=시작할게요")
        page.select_option("#status", "in_review")
        page.wait_for_selector("#approve")
        page.click("#request-changes")
        page.wait_for_function("document.getElementById('status').value === 'changes_requested'")
        assert "모바일도 확인해 주세요" in page.inner_text(".timeline")
        page.screenshot(path=str(shots / "issue.png"), full_page=True)

        # 하위 Task
        page.click("text=Add task")
        page.fill("#n-title", "Task: 메뉴 UI"); page.click("#new-form button[type=submit]")
        page.wait_for_selector("text=Task of")
        assert page.url.endswith("#/issue/NS-2")

        # 목록 — 기본은 열린 것만, 검색
        page.goto(BASE + "/#/")
        page.wait_for_selector("tr.row")
        assert page.locator("tr.row").count() == 2
        page.fill("#q", "메뉴 UI"); page.wait_for_function("document.querySelectorAll('tr.row').length === 1")
        page.fill("#q", ""); page.wait_for_function("document.querySelectorAll('tr.row').length === 2")
        page.screenshot(path=str(shots / "list.png"))

        # 칸반 — 끌어서 상태 바꾸기
        page.click("[data-nav=board]")
        page.wait_for_selector('.card[data-ref="NS-2"]')
        page.drag_and_drop('.card[data-ref="NS-2"]', '.col[data-col="in_progress"] .cards')
        page.wait_for_selector('.col[data-col="in_progress"] .card[data-ref="NS-2"]')
        page.screenshot(path=str(shots / "board.png"))

        # 에이전트 — 키는 한 번만
        page.click("[data-nav=agents]")
        page.fill("#a-name", "claude"); page.fill("#a-vendor", "anthropic"); page.fill("#a-model", "claude-opus-5-5")
        page.click("#agent-form button")
        page.wait_for_selector(".keybox code")
        key = page.inner_text("#key")
        assert key.startswith("dev_")
        page.click("[data-nav=issues]"); page.click("[data-nav=agents]")
        page.wait_for_selector("td:text('claude-opus-5-5')")
        assert page.locator(".keybox").count() == 0 and key not in page.content()

        # 에이전트가 API로 계획서를 올리면 상세에 모델과 함께 보인다
        r = page.request.post(f"{BASE}/api/issues/NS-1/plans", data={"body": "v2 by agent"}, headers={"Authorization": f"Bearer {key}"})
        assert r.ok, r.text()
        page.goto(BASE + "/#/issue/NS-1"); page.wait_for_selector("text=v2 by agent")
        assert "claude-opus-5-5" in page.inner_text(".timeline")

        # 모바일 폭
        page.set_viewport_size({"width": 390, "height": 800})
        page.goto(BASE + "/#/issue/NS-1"); page.wait_for_selector("h1#title")
        assert page.evaluate("document.documentElement.scrollWidth") <= 391
        page.screenshot(path=str(shots / "issue_mobile.png"), full_page=True)

        # 로그아웃
        page.click("#logout"); page.wait_for_selector("#login:not([hidden])")
        assert not errs, errs
    print("OK")
finally:
    for pr in procs:
        pr.terminate()
