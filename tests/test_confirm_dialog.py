# 공통 확인창의 배지·반환값·접근성·좁은 화면 배치를 실제 브라우저로 검사한다.
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
CAPTURES = ROOT / 'tests' / '__pycache__' / 'confirm-dialog'


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def open_dialog(page, options=None, title='DEV-234를 검토 맡길까요?'):
    page.evaluate('''({title, options}) => {
      document.querySelector('#dialog-trigger')?.remove();
      const button = document.createElement('button');
      button.id = 'dialog-trigger'; button.textContent = '확인창 열기';
      document.body.append(button); button.focus();
      window.dialogResult = 'pending';
      confirmDialog(title, options).then(result => window.dialogResult = result);
    }''', {'title': title, 'options': options or {'description': '모델과 작업 내용을 확인해 주세요.'}})
    dialog = page.get_by_role('alertdialog', name=title, exact=True)
    expect(dialog).to_be_visible()
    return dialog


def closed(page, result):
    expect(page.locator('.confirm-dialog')).to_have_count(0)
    page.wait_for_function('window.dialogResult !== "pending"')
    assert page.evaluate('window.dialogResult') == result
    expect(page.locator('#dialog-trigger')).to_be_focused()


def layout(dialog):
    assert dialog.evaluate('''d => {
      const r = d.getBoundingClientRect(), a = d.querySelector('.confirm-actions').getBoundingClientRect();
      return r.left >= 0 && r.right <= innerWidth && r.top >= 0 && r.bottom <= innerHeight
        && a.top >= r.top && a.bottom <= r.bottom
        && d.scrollWidth <= d.clientWidth
        && d.querySelector('.confirm-content').scrollWidth <= d.querySelector('.confirm-content').clientWidth;
    }'''), '확인창·본문·버튼이 화면 안에 있어야 한다'


def run():
    CAPTURES.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(ROOT / 'static')))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                for theme in ('light', 'dark'):
                    for width in (1300, 390):
                        page = browser.new_page(viewport={'width': width, 'height': 800}, reduced_motion='reduce')
                        # 운영 API·로그인·외부 폰트 요청을 사용하지 않는다.
                        page.route('**/*', lambda route: route.fulfill(status=401, json={'detail': 'mock'})
                                   if '/api/' in route.request.url else route.continue_()
                                   if route.request.url.startswith('http://127.0.0.1:') else route.abort())
                        page.goto(f'http://127.0.0.1:{server.server_port}/')
                        page.wait_for_function('typeof confirmDialog === "function"')
                        page.evaluate('(t) => document.documentElement.dataset.theme = t', theme)
                        for danger in (False, True):
                            title = '정말 삭제할까요?' if danger else 'DEV-234를 검토 맡길까요?'
                            dialog = open_dialog(page, {'danger': danger, 'description': '작업 내용을 확인해 주세요.', 'focusCancel': danger}, title)
                            expect(dialog.locator('.confirm-badge use')).to_have_attribute('href', '#i-circle-alert' if danger else '#i-circle-help')
                            expect(dialog.locator('.confirm-badge svg')).to_have_attribute('aria-hidden', 'true')
                            expect(dialog.locator('.confirm-badge svg')).to_have_attribute('focusable', 'false')
                            expect(dialog).to_have_attribute('aria-labelledby', 'confirm-title')
                            expect(dialog).to_have_attribute('aria-describedby', 'confirm-description')
                            assert dialog.aria_snapshot().splitlines()[0].startswith(f'- alertdialog "{title}"')
                            expect(dialog.locator('.confirm-cancel' if danger else '.confirm-ok')).to_be_focused()
                            layout(dialog)
                            page.screenshot(path=str(CAPTURES / f'{theme}-{width}-{"danger" if danger else "normal"}.png'))
                            dialog.locator('.confirm-ok').click()
                            closed(page, True)
                        for action in ('escape', 'cancel', 'backdrop'):
                            dialog = open_dialog(page)
                            if action == 'escape':
                                page.keyboard.press('Escape')
                            elif action == 'cancel':
                                dialog.locator('.confirm-cancel').click()
                            else:
                                page.mouse.click(2, 2)
                            closed(page, False)
                        dialog = open_dialog(page, {'input': True, 'description': '닫는 이유를 적어 주세요.'})
                        expect(dialog.locator('.confirm-input')).to_be_focused()
                        dialog.locator('.confirm-ok').click()
                        expect(dialog).to_be_visible()
                        dialog.locator('.confirm-input').fill('작업을 정리했어요')
                        dialog.locator('.confirm-ok').click()
                        closed(page, '작업을 정리했어요')
                        dialog = open_dialog(page, {'input': True})
                        page.keyboard.press('Escape')
                        closed(page, None)
                        checks = [{'key': str(i), 'cmd': f'python tests/check_{i}.py', 'why': '긴 검사 설명을 확인해요. ' * 8, 'required': i == 0} for i in range(18)]
                        dialog = open_dialog(page, {'description': '검사를 선택해 주세요.', 'checklist': {'plan': checks, 'always': [{'key': 'syntax', 'label': '문법 검사', 'why': '항상 실행해요.', 'required': True}]}}, '아주 긴 작업 제목과 검사 목록에서도 버튼을 누를 수 있는지 확인할까요? ' * 3)
                        dialog.locator('details').evaluate('(d) => d.open = true')
                        expect(dialog.locator('[data-check="0"]')).to_be_disabled()
                        expect(dialog.locator('[data-always="syntax"]')).to_be_disabled()
                        dialog.locator('[data-check="1"]').uncheck()
                        layout(dialog)
                        assert dialog.locator('.confirm-content').evaluate('(d) => d.scrollHeight > d.clientHeight')
                        dialog.locator('.confirm-content').evaluate('(d) => d.scrollTop = d.scrollHeight')
                        layout(dialog)
                        page.screenshot(path=str(CAPTURES / f'{theme}-{width}-checklist.png'))
                        dialog.locator('.confirm-ok').click()
                        closed(page, [str(i) for i in range(18) if i != 1])
                        page.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    print(f'PASS: 낮·밤 × 1300·390, 일반/위험·취소·입력·검사 목록. 캡처: {CAPTURES}')


if __name__ == '__main__':
    run()
