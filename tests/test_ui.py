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
        answers = []   # 입력창(prompt)에 줄 답 — 비었으면 기본값. None이면 취소.
        def on_dialog(d):
            a = answers.pop(0) if answers else "모바일도 확인해 주세요"
            d.dismiss() if a is None else d.accept(a) if d.type == "prompt" else d.accept()
        page.on("dialog", on_dialog)
        page.goto(BASE)
        page.wait_for_selector("#login-form")
        page.fill("#login-username", "admin"); page.fill("#login-password", "nope"); page.click("#login-form button")
        page.wait_for_function("document.getElementById('login-error').textContent.includes('올바르지')")
        page.fill("#login-password", "pw"); page.click("#login-form button")
        page.wait_for_selector("#shell:not([hidden])")

        # 프로젝트 없이 New issue → 안내
        page.click("a[href=\"#/new\"]")
        page.wait_for_selector("text=먼저")
        page.goto(BASE + "/#/projects")
        page.fill("#p-key", "NS"); page.fill("#p-name", "nightshift"); page.click("#project-form button")
        page.wait_for_selector("td.ref:text('NS')")
        assert page.get_attribute("#p-path", "placeholder").startswith("C:\\Users\\")
        # 고치기 — 키는 잠기고, 이름·경로가 바뀐다
        page.click('[data-edit="NS"]')
        assert page.is_disabled("#p-key") and page.input_value("#p-name") == "nightshift"
        page.fill("#p-path", r"C:\Users\Simon Lomebrote\Projects\nightshift")
        page.click("#project-form button[type=submit]")
        page.wait_for_function("p => [...document.querySelectorAll('td')].some(td => td.textContent === p)",
                               arg=r"C:\Users\Simon Lomebrote\Projects\nightshift")
        assert not page.is_disabled("#p-key")   # 저장하면 추가 폼으로 돌아온다
        page.click('[data-edit="NS"]'); page.click("#p-cancel")
        assert page.input_value("#p-name") == ""

        page.click("a[href=\"#/new\"]")
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

        # 밤/낮 — nightshift처럼 두 상태: 고른 적 없으면 운영체제를 따르고, 누르면 반대로 고정, 새로고침해도 유지
        bg = lambda: page.evaluate("getComputedStyle(document.body).backgroundColor")
        theme = lambda: page.evaluate("document.documentElement.dataset.theme || 'auto'")
        icon = lambda: page.get_attribute("#theme use", "href")
        page.set_viewport_size({"width": 1300, "height": 850})
        page.evaluate("localStorage.removeItem('dev.theme')"); page.reload(); page.wait_for_selector("h1#title")
        page.emulate_media(color_scheme="light")
        assert theme() == "auto" and icon() == "#i-moon"
        light_bg = bg()
        page.emulate_media(color_scheme="dark")
        page.wait_for_function("document.querySelector('#theme use').getAttribute('href') === '#i-sun'", timeout=3000)
        assert theme() == "auto"   # 운영체제를 따라 아이콘도
        dark_bg = bg(); assert dark_bg != light_bg
        page.click("#theme"); assert theme() == "light" and bg() == light_bg and icon() == "#i-moon"   # 밤이던 화면을 낮으로 고정
        page.reload(); page.wait_for_selector("h1#title")
        assert theme() == "light" and bg() == light_bg
        page.click("#theme"); assert theme() == "dark" and bg() == dark_bg and icon() == "#i-sun"
        page.emulate_media(color_scheme="light"); page.wait_for_timeout(400)
        page.screenshot(path=str(shots / "theme_dark.png"))
        page.click("#theme"); page.wait_for_timeout(400)

        # 헤더·탭 — 사용자 칩(이름·ADMIN), 탭 아이콘, 마우스를 올리면 제목이 펼쳐진다, 활성 탭 밑줄
        assert page.inner_text("#user-chip-name") == "admin"
        page.goto(BASE + "/#/"); page.wait_for_selector("tr.row")
        assert page.get_attribute('[data-nav="issues"]', "class") == "tab-btn active"
        label_w = lambda: page.evaluate("document.querySelector('[data-nav=board] .tab-label').getBoundingClientRect().width")
        assert label_w() < 1
        page.hover('[data-nav="board"]'); page.wait_for_timeout(400)
        assert label_w() > 20
        page.screenshot(path=str(shots / "header.png"), clip={"x": 0, "y": 0, "width": 1300, "height": 160})
        # 사용자 칩 → 메뉴 → 바깥 누르면 닫힘
        page.click("#user-chip"); assert page.is_visible("#user-menu")
        assert page.get_attribute("#open-nightshift", "href").startswith("http")
        page.screenshot(path=str(shots / "user_menu.png"), clip={"x": 900, "y": 0, "width": 400, "height": 200})
        page.mouse.click(600, 500); assert not page.is_visible("#user-menu")
        page.set_viewport_size({"width": 390, "height": 800}); page.wait_for_timeout(200)
        page.screenshot(path=str(shots / "header_mobile.png"), clip={"x": 0, "y": 0, "width": 390, "height": 300})
        assert page.evaluate("document.documentElement.scrollWidth") <= 391

        # Approve → Done이면 목록으로 돌아간다(DEV-5), 상태 칸에서 Closed를 골라도
        page.set_viewport_size({"width": 1300, "height": 850})
        page.request.post(f"{BASE}/api/issues/NS-2/status", data={"status": "in_review"}, headers={"X-Requested-With": "dev"})
        page.goto(BASE + "/#/issue/NS-2"); page.wait_for_selector("#approve")
        page.click("#approve")
        page.wait_for_function("location.hash === '#/'")
        assert "NS-2을(를) 끝냈어요" in page.inner_text("#toast")
        page.wait_for_selector("tr.row")
        assert "NS-2" not in page.inner_text("table.issues")   # 기본 목록은 끝난 것을 숨긴다
        page.goto(BASE + "/#/issue/NS-1"); page.wait_for_selector("#status")
        # Closed는 사유를 묻는다(DEV-6) — 취소·빈칸이면 닫지 않고 원래 상태로
        answers[:] = [None]
        page.select_option("#status", "closed"); page.wait_for_timeout(300)
        assert page.input_value("#status") == "changes_requested" and "사유가 없어서" in page.inner_text("#toast")
        answers[:] = ["   "]
        page.select_option("#status", "closed"); page.wait_for_timeout(300)
        assert page.input_value("#status") == "changes_requested"
        assert page.request.get(f"{BASE}/api/issues/NS-1").json()["status"] == "changes_requested"
        answers[:] = ["NS-3과 중복"]
        page.select_option("#status", "closed")
        page.wait_for_function("location.hash === '#/'")
        ev = page.request.get(f"{BASE}/api/issues/NS-1").json()["events"][-1]
        assert ev["kind"] == "status" and ev["data"]["to"] == "closed" and ev["body"] == "NS-3과 중복", ev
        page.goto(BASE + "/#/issue/NS-2"); page.wait_for_selector("#status")
        page.select_option("#status", "in_progress")   # 끝내는 게 아니면 그 자리에 머문다
        page.wait_for_function("document.getElementById('status') && document.getElementById('status').value === 'in_progress'")
        assert page.evaluate("location.hash") == "#/issue/NS-2"

        # 목록 나눠 읽기(DEV-7) — 처음 50개, 끝까지 내리면 더 붙는다
        for n in range(110):
            page.request.post(f"{BASE}/api/issues", data={"project": "NS", "title": f"많은 이슈 {n}"}, headers={"X-Requested-With": "dev"})
        page.goto(BASE + "/#/"); page.wait_for_selector("tr.row")
        rows = lambda: page.locator("tr.row").count()
        page.wait_for_timeout(500)
        assert rows() == 50, rows()
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_function("document.querySelectorAll('tr.row').length === 100")
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_function("document.querySelectorAll('tr.row').length === 111")   # NS-2(in_progress) + 110
        refs = page.evaluate("[...document.querySelectorAll('tr.row')].map(r => r.dataset.ref)")
        assert len(refs) == len(set(refs))
        # 검색하면 처음부터, 검색 글칸 포커스 유지
        page.fill("#q", "많은 이슈 10")
        page.wait_for_function("document.querySelectorAll('tr.row').length === 11")   # 10, 100~109
        assert page.evaluate("document.activeElement.id") == "q"
        page.fill("#q", "")
        page.wait_for_function("document.querySelectorAll('tr.row').length === 50")

        # 제목 없이 본문만(DEV-8) — 만들어지고 "(제목 없음…)"으로 보인다
        page.goto(BASE + "/#/new"); page.wait_for_selector("#n-body")
        page.fill("#n-body", "제목 없이 쓴 요구")
        page.click("#new-form button[type=submit]")
        page.wait_for_function("location.hash.startsWith('#/issue/NS-')")
        assert "제목 없음" in page.inner_text("h1#title")

        # Create를 연달아 두 번 눌러도 한 번만 저장(DEV-12)
        page.goto(BASE + "/#/new"); page.wait_for_selector("#n-body")
        page.fill("#n-title", "두 번 누르기 시험"); page.fill("#n-body", "본문")
        page.evaluate("() => { const b = document.querySelector('#new-form button[type=submit]'); b.click(); b.click(); b.click(); }")
        page.wait_for_function("location.hash.startsWith('#/issue/NS-')")
        page.wait_for_timeout(500)
        same = [i for i in page.request.get(f"{BASE}/api/issues?q=두 번 누르기 시험").json()["issues"]]
        assert len(same) == 1, same

        # Issues 화면의 프로젝트 필터(DEV-11) — 탭 줄 필터와 같이 움직인다
        H = {"X-Requested-With": "dev"}
        page.request.post(f"{BASE}/api/projects", data={"key": "DEV", "name": "dev"}, headers=H)
        page.request.post(f"{BASE}/api/issues", data={"project": "DEV", "title": "dev 쪽 이슈"}, headers=H)
        page.evaluate("loadProjects()"); page.goto(BASE + "/#/"); page.wait_for_selector("#list-project")
        page.wait_for_selector("tr.row")
        page.select_option("#list-project", "DEV")
        page.wait_for_function("() => [...document.querySelectorAll('tr.row')].every(r => r.dataset.ref.startsWith('DEV-')) && document.querySelectorAll('tr.row').length === 1")
        assert page.input_value("#project-filter") == "DEV"
        page.select_option("#list-project", "")
        page.wait_for_function("() => [...document.querySelectorAll('tr.row')].some(r => r.dataset.ref.startsWith('NS-'))")

        # 로그아웃(사용자 메뉴 안)
        page.click("#user-chip"); page.click("#logout"); page.wait_for_selector("#login:not([hidden])")
        assert not errs, errs
    print("OK")
finally:
    for pr in procs:
        pr.terminate()
