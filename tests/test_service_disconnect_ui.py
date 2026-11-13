# 연결 해제 확인창의 요청·취소·진행·경고와 낮밤 화면 배치를 모의 API 브라우저로 검사한다.
import json
from functools import partial
from http.server import ThreadingHTTPServer
from threading import Thread
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
from test_project_secrets_ui import Api as BaseApi, QuietHandler

ROOT = Path(__file__).resolve().parents[1]
CAPTURES = ROOT / 'tests' / '__pycache__' / 'service-disconnect'
PROJECT = {'key':'CH', 'name':'테스트 프로젝트', 'local_path':'C:/Projects/test',
           'service_app':'main', 'service_domain':'test.example.com', 'service_port':8400,
           'service_prepare_provider':'auto', 'archived':False, 'issue_count':0}

class Api(BaseApi):
    def __init__(self):
        super().__init__()
        self.posts = []
        self.preview_error = False
        self.post_error = False
        self.preview_count = 0
        self.preview = {'available':True, 'stop_default':True, 'token':'ui',
                        'apps':[{'name':f'worker-{i}-' + 'long-name-' * 5, 'ids':[i, i+100],
                                 'cwd':'C:/Projects/test/' + 'long-directory/' * 4,
                                 'status':'stopped' if i == 1 else 'online'} for i in range(12)]}
        self.state = {'status':'connected', 'managed':True, 'steps':[], 'url':'https://test.example.com'}

    def handle(self, route):
        path = route.request.url.split('?')[0].split('/api/')[-1]
        if path == 'projects':
            return route.fulfill(json={'projects':[PROJECT]})
        if path == 'projects/CH/service/disconnect-preview':
            self.preview_count += 1
            return route.fulfill(status=503 if self.preview_error else 200,
                                 json={'detail':'앱 목록 조회에 실패했어요.'} if self.preview_error else self.preview)
        if path == 'projects/CH/service':
            if route.request.method == 'POST':
                self.posts.append(json.loads(route.request.post_data))
                return route.fulfill(status=409 if self.post_error else 200,
                                     json={'detail':'대상이 바뀌었어요. 다시 확인해 주세요.'} if self.post_error else {})
            return route.fulfill(json=self.state)
        return super().handle(route)


def dialog(page):
    page.locator('#p-service-disconnect').click()
    d = page.get_by_role('alertdialog')
    expect(d).to_be_visible()
    return d


def run():
    CAPTURES.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1',0), partial(QuietHandler, directory=str(ROOT / 'static')))
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                for theme in ('light','dark'):
                    for width in (1300,390):
                        api = Api()
                        page = browser.new_page(viewport={'width':width,'height':800}, reduced_motion='reduce')
                        page.route('**/*', api.handle)
                        page.goto(f'http://127.0.0.1:{server.server_port}/#/projects?project=CH&tab=manage')
                        expect(page.locator('#p-service-disconnect')).to_be_enabled()
                        page.evaluate('(t) => document.documentElement.dataset.theme=t',theme)
                        before = page.locator('#p-service-status').inner_text()
                        # 취소·Esc·바깥 클릭은 진단을 바꾸거나 POST하지 않는다.
                        for cancel in ('button','escape','backdrop'):
                            d = dialog(page)
                            expect(d.locator('.disconnect-apps li')).to_have_count(12)
                            expect(d.locator('.disconnect-stop')).to_be_checked()
                            expect(d.locator('.confirm-cancel')).to_be_focused()
                            page.evaluate('document.querySelector("#p-service-disconnect").click()')
                            expect(page.locator('.confirm-dialog')).to_have_count(1)
                            assert d.evaluate('''d => {const r=d.getBoundingClientRect();const c=d.querySelector('.confirm-content');const a=d.querySelector('.confirm-actions').getBoundingClientRect();return r.left>=0 && r.right<=innerWidth && r.top>=0 && r.bottom<=innerHeight && a.bottom<=r.bottom && c.scrollWidth<=c.clientWidth}''')
                            if cancel == 'button':
                                assert d.locator('.confirm-content').evaluate('e => e.scrollHeight > e.clientHeight')
                                d.locator('.disconnect-apps li').last.scroll_into_view_if_needed()
                                expect(d.locator('.disconnect-apps li').last).to_be_in_viewport()
                                d.locator('.confirm-content').evaluate('e => e.scrollTop=0')
                                page.screenshot(path=str(CAPTURES / f'{theme}-{width}.png'))
                                d.locator('.confirm-cancel').click()
                            elif cancel == 'escape': page.keyboard.press('Escape')
                            else: page.mouse.click(2,2)
                            expect(page.locator('.confirm-dialog')).to_have_count(0)
                            expect(page.locator('#p-service-disconnect')).to_be_enabled()
                            assert not api.posts
                            assert page.locator('#p-service-status').inner_text() == before
                        assert api.preview_count == 3
                        d = dialog(page); d.locator('.disconnect-stop').uncheck(); d.locator('.confirm-ok').click()
                        expect(page.locator('#p-service-disconnect')).to_be_enabled()
                        assert api.posts[-1] == {'action':'disconnect','stop_apps':False,'preview_token':'ui'}
                        d = dialog(page); d.locator('.confirm-ok').click()
                        expect(page.locator('#p-service-disconnect')).to_be_enabled()
                        assert api.posts[-1]['stop_apps'] is True
                        # service_app 없음에 대응하는 서버 기본값과 빈 목록.
                        PROJECT['service_app'] = ''
                        api.preview.update(stop_default=False, apps=[])
                        d = dialog(page); expect(d.locator('.disconnect-stop')).not_to_be_checked()
                        expect(d).to_contain_text('현재 등록된 대상 앱이 없어요.')
                        d.locator('.confirm-cancel').click()
                        PROJECT['service_app'] = 'main'
                        for http_error in (False,True):
                            api.preview_error = http_error
                            api.preview.update(available=False, reason='설정 파일을 확인하지 못했어요.')
                            d = dialog(page); expect(d.locator('.disconnect-stop')).to_be_disabled()
                            expect(d).to_contain_text('터널만 해제할 수 있어요.')
                            d.locator('.confirm-ok').click()
                            expect(page.locator('#p-service-disconnect')).to_be_enabled()
                            assert api.posts[-1]['stop_apps'] is False
                        api.preview_error = False; api.post_error = True
                        d = dialog(page); d.locator('.confirm-ok').click()
                        expect(page.locator('#p-service-status')).to_contain_text('대상이 바뀌었어요')
                        expect(page.locator('#p-service-disconnect')).to_be_enabled()
                        api.post_error = False
                        # 폴링과 같은 상태 렌더 경로에 단계/부분 실패/저장 경고를 제공한다.
                        for status, step_status, warning in (
                            ('running','running',''),
                            ('disconnected','failed','터널 연결 해제는 끝났어요. worker 앱을 멈추지 못했어요: 시간 초과예요.'),
                            ('disconnected','failed','앱은 멈췄지만 PM2 목록 저장에 실패했어요. 재부팅 뒤 다시 켜질 수 있어요.')):
                            api.state = {'status':status,'managed':False,'phase':'앱 멈추기','app_error':warning,
                                         'steps':[{'number':i,'label':label,'status':'done' if i<4 else step_status,'error':warning if i==4 else ''}
                                                  for i,label in enumerate(('사전검사','터널 전환','연결 해제','앱 멈추기'),1)]}
                            page.locator('#p-service-refresh').click()
                            expect(page.locator('.service-steps li')).to_have_count(4)
                            expect(page.locator('.service-steps li').last).to_contain_text('앱 멈추기')
                            expect(page.locator('.service-steps li').last).to_contain_text('진행 중' if status=='running' else '실패')
                            if warning:
                                expect(page.locator('#p-service-status')).to_contain_text(warning)
                                page.evaluate('Object.defineProperty(navigator, "clipboard", {value:{writeText:async t=>{window.copied=t}}, configurable:true})')
                                page.locator('#p-service-copy').click()
                                page.wait_for_function('window.copied && window.copied.includes("앱 멈추기")')
                                assert warning in page.evaluate('window.copied')
                        # 공통 반환 계약도 같은 명령에서 확인한다.
                        for opts, result in (({},True),({'input':True},'이유'),({'checklist':{'plan':[], 'always':[]}},[])):
                            page.evaluate('(o)=>{window.result="pending";confirmDialog("계약 검사",o).then(r=>window.result=r)}',opts)
                            if opts.get('input'): page.locator('.confirm-input').fill('이유')
                            page.locator('.confirm-ok').click()
                            page.wait_for_function('window.result !== "pending"')
                            assert page.evaluate('window.result') == result
                        page.close()
            finally: browser.close()
    finally:
        server.shutdown(); server.server_close(); thread.join()
    print(f'PASS: 전체 대상·기본값·요청·취소·중복·조회 오류·단계·부분 실패·저장 경고·공통 계약, 낮밤 × 1300/390. 캡처: {CAPTURES}')

if __name__ == '__main__':
    run()
