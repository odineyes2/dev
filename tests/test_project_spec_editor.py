# 프로젝트 생성창의 기획서 직접 입력·파일 불러오기·초안 보관·요청 body를 실제 브라우저와 모의 API로 검사한다(DEV-225-2).
import json
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
CAPTURES = ROOT / '.ui-captures' / 'dev-225'
CATALOG = [
    {'id': 'blank', 'name': '빈 앱', 'description': '최소 앱', 'provides': [], 'options': {}},
    {'id': 'web-game', 'name': '웹게임', 'description': '게임', 'provides': [], 'default_genre': 'puzzle',
     'options': {'scores': {'name': '점수 기록', 'default': True}},
     'genres': [{'id': 'puzzle', 'name': '퍼즐', 'description': '격자', 'provides': [], 'options': ['scores']},
                {'id': 'novel', 'name': '노벨', 'description': '장면', 'provides': [], 'options': []}]},
]


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class Api:
    """운영 API 대신 응답하고 생성·업로드 요청을 기록한다."""

    def __init__(self):
        self.initialize, self.uploads, self.fail = [], 0, False

    def handle(self, route):
        req = route.request
        url = req.url.split('?')[0]
        if not url.startswith('http://127.0.0.1:'):
            return route.abort()
        if '/api/' not in url:
            return route.continue_()
        path = url.split('/api/', 1)[1]
        if path == 'auth/me':
            return route.fulfill(json={'actor': {'kind': 'human', 'name': 'admin'}, 'nightshift_url': '#'})
        if path == 'projects':
            return route.fulfill(json={'projects': []})
        if path == 'agents':
            return route.fulfill(json={'agents': []})
        if path == 'settings/tokens':
            return route.fulfill(json={'unit': 'auto'})
        if path == 'projects/templates':
            return route.fulfill(json=CATALOG)
        if path == 'attachments' and req.method == 'POST':
            self.uploads += 1
            return route.fulfill(json={'id': 7})
        if path == 'projects/initialize' and req.method == 'POST':
            self.initialize.append(json.loads(req.post_data))
            if self.fail:
                return route.fulfill(status=500, json={'detail': '모의 실패예요.'})
            return route.fulfill(status=202, json={'key': 'NS'})
        return route.fulfill(status=404, json={'detail': 'mock'})


def open_create(page, template='blank', genre=None):
    page.locator('#project-add').click()
    page.locator(f'[data-template-pick="{template}"]').click()
    if genre:
        page.locator(f'[data-genre-pick="{genre}"]').click()
    expect(page.locator('#p-key')).to_be_focused()
    page.locator('#p-key').fill('NS')
    page.locator('#p-name').fill('nightshift')
    page.locator('#p-folder').fill('ns')


def text_mode(page):
    page.locator('[name="p-spec-mode"][value="text"]').check()
    expect(page.locator('#p-spec-text')).to_be_visible()


def draft(page):
    return page.evaluate('localStorage.getItem("dev.projectSpecDraft")')


def behaviour(page, api):
    init, spec = page.locator('#p-initialize'), page.locator('#p-spec-text')
    open_create(page)
    expect(page.locator('#p-spec')).to_be_visible()
    text_mode(page)
    expect(init).to_be_disabled()
    # 다바이트 크기 표시와 공백·한도 차단
    spec.fill('가나다')
    expect(page.locator('#p-spec-size')).to_contain_text('3자 · 9 / 200,000바이트 · 남은 199,991바이트')
    expect(page.locator('#p-spec-size')).to_contain_text('바이트 한도가 먼저')
    expect(init).to_be_enabled()
    spec.fill('  \n\t ')
    expect(init).to_be_disabled()
    expect(page.locator('#p-spec-error')).to_have_text('기획서 내용을 입력해 주세요.')
    expect(spec).to_have_attribute('aria-invalid', 'true')
    spec.fill('a' * 199997 + '가')
    expect(page.locator('#p-spec-size')).to_contain_text('200,000 / 200,000바이트')
    expect(init).to_be_enabled()
    spec.fill('a' * 199998 + '가')
    expect(init).to_be_disabled()
    expect(page.locator('#p-spec-error')).to_contain_text('1바이트 넘었어요')
    page.evaluate('document.querySelector("#project-form").requestSubmit()')
    expect(page.locator('#project-error')).to_contain_text('바이트 넘었어요')
    assert api.initialize == [], '한도 초과는 submit 경로에서도 막아야 한다'
    # 단축키가 편집기 입력을 가로채지 않는다
    spec.fill('')
    spec.press_sequentially('# pi')
    expect(spec).to_have_value('# pi')
    assert page.evaluate('location.hash') == '#/projects'
    # 닫기·Escape·새로고침 뒤 초안 복원
    spec.fill('# 초안\n본문')
    page.locator('#project-close').click()
    expect(page.locator('#project-dialog')).not_to_be_visible()
    assert draft(page) == '# 초안\n본문'
    open_create(page)
    expect(page.locator('[name="p-spec-mode"][value="text"]')).to_be_checked()
    expect(spec).to_have_value('# 초안\n본문')
    expect(page.locator('#p-spec-draft')).to_have_text('이전에 입력한 초안을 불러왔어요.')
    spec.fill('# 초안\n본문 2')
    page.keyboard.press('Escape')
    expect(page.locator('#project-dialog')).not_to_be_visible()
    page.reload()
    page.wait_for_selector('#project-add')
    open_create(page)
    expect(spec).to_have_value('# 초안\n본문 2')
    # 장르 이동은 초안을 지우지 않는다
    page.locator('#p-step-back').click()
    page.locator('[data-template-pick="web-game"]').click()
    page.locator('[data-genre-pick="novel"]').click()
    page.locator('#p-step-back').click()
    page.locator('[data-genre-pick="puzzle"]').click()
    expect(spec).to_have_value('# 초안\n본문 2')
    page.locator('#p-key').fill('NS')
    page.locator('#p-name').fill('nightshift')
    page.locator('#p-folder').fill('ns')
    # 실패는 초안을 보존하고 같은 ID로 재시도, 수정하면 새 ID
    api.fail = True
    init.click()
    expect(page.locator('#project-error')).to_contain_text('모의 실패예요.')
    expect(init).to_be_enabled()
    init.click()
    expect(page.locator('#project-error')).to_contain_text('모의 실패예요.')
    assert len(api.initialize) == 2 and api.initialize[0] == api.initialize[1]
    body = api.initialize[0]
    assert body['spec_markdown'] == '# 초안\n본문 2' and 'attachment_id' not in body, body
    assert body['template'] == 'web-game' and body['genre'] == 'puzzle' and body['options'] == {'scores': True}, body
    assert draft(page) == '# 초안\n본문 2'
    spec.fill('# 고친 초안')
    init.click()
    expect(page.locator('#project-error')).to_contain_text('모의 실패예요.')
    assert api.initialize[2]['request_id'] != api.initialize[0]['request_id']
    # 성공하면 초안을 지우고 업로드는 하지 않는다
    api.fail = False
    init.click()
    expect(page.locator('#project-dialog')).not_to_be_visible()
    assert api.initialize[3]['spec_markdown'] == '# 고친 초안' and api.uploads == 0
    assert draft(page) is None
    open_create(page)
    expect(page.locator('[name="p-spec-mode"][value="file"]')).to_be_checked()
    # 파일 불러오기 — 잘못된 UTF-8은 거절, 올바른 파일은 편집기로
    page.locator('#p-spec').set_input_files({'name': 'bad.md', 'mimeType': 'text/markdown', 'buffer': b'# \xff\xfe'})
    page.locator('#p-spec-load').click()
    expect(page.locator('#p-spec-error')).to_contain_text('UTF-8로 읽을 수 없는')
    expect(page.locator('[name="p-spec-mode"][value="file"]')).to_be_checked()
    page.locator('#p-spec').set_input_files({'name': 'spec.md', 'mimeType': 'text/markdown', 'buffer': '﻿# 파일 기획서\n'.encode()})
    page.locator('#p-spec-load').click()
    expect(spec).to_have_value('# 파일 기획서\n')
    expect(page.locator('[name="p-spec-mode"][value="text"]')).to_be_checked()
    assert draft(page) == '# 파일 기획서\n'
    # 파일 첨부 모드는 attachment_id만 보낸다
    page.locator('[name="p-spec-mode"][value="file"]').check()
    init.click()
    expect(page.locator('#project-dialog')).not_to_be_visible()
    body = api.initialize[-1]
    assert body['attachment_id'] == 7 and 'spec_markdown' not in body and api.uploads == 1, body
    assert draft(page) == '# 파일 기획서\n', '파일로 만든 요청은 편집기 초안을 지우지 않는다'
    # 명시적 지우기
    open_create(page)
    page.locator('#p-spec-clear').click()
    page.locator('.confirm-dialog .confirm-ok').click()
    expect(spec).to_have_value('')
    assert draft(page) is None
    page.locator('#project-close').click()


def layout(page, path):
    page.locator('#project-add').click()
    page.locator('[data-template-pick="blank"]').click()
    text_mode(page)
    page.locator('#p-spec-text').fill('# 기획서\n\n' + '한글과 English가 섞인 아주 긴 줄이에요. ' * 30)
    dialog = page.locator('#project-dialog')
    assert dialog.evaluate('''d => {
      const r = d.getBoundingClientRect(), t = d.querySelector('#p-spec-text').getBoundingClientRect();
      return r.left >= 0 && r.right <= innerWidth && d.scrollWidth <= d.clientWidth
        && t.left >= r.left && t.right <= r.right && document.documentElement.scrollWidth <= innerWidth;
    }'''), '생성창과 편집기가 가로로 넘치면 안 된다'
    page.screenshot(path=str(path))
    for button in ('#p-initialize', '#p-cancel'):
        page.locator(button).scroll_into_view_if_needed()
        expect(page.locator(button)).to_be_in_viewport()
    expect(page.locator('#p-initialize')).to_be_enabled()
    page.locator('#project-close').click()
    expect(page.locator('#project-add')).to_be_focused()


def run():
    CAPTURES.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(ROOT / 'static')))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                def new_page(width, theme='light'):
                    api = Api()
                    page = browser.new_page(viewport={'width': width, 'height': 800}, reduced_motion='reduce')
                    page.route('**/*', api.handle)
                    page.goto(f'http://127.0.0.1:{server.server_port}/#/projects')
                    page.wait_for_selector('#project-add')
                    page.evaluate('(t) => document.documentElement.dataset.theme = t', theme)
                    return page, api
                page, api = new_page(1300)
                behaviour(page, api)
                page.close()
                for theme in ('light', 'dark'):
                    for width in (1300, 390):
                        page, _ = new_page(width, theme)
                        layout(page, CAPTURES / f'{theme}-{width}-editor.png')
                        page.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    print(f'PASS: 직접 입력·크기 차단·파일 불러오기·초안·재시도·장르 이동, 낮·밤 × 1300·390. 캡처: {CAPTURES}')


if __name__ == '__main__':
    run()
