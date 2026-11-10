# 브랜드 탭의 URL·드롭·조정·적용 상태·빈 변경·사용 불가와 낮밤 배치를 검사한다.
import base64
import json
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
CAPTURES=ROOT/'.ui-captures'/'dev-254-4'
PROJECT={'key':'NS','name':'nightshift','local_path':'C:/Projects/nightshift','service_domain':'nightshift.example','repo_url':'https://example.com/ns','archived':False,'issue_count':0}
PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aDnsAAAAASUVORK5CYII=')
class Quiet(SimpleHTTPRequestHandler):
    def log_message(self,*args): pass
    def copyfile(self,source,outputfile):
        try: super().copyfile(source,outputfile)
        except (ConnectionAbortedError,ConnectionResetError,BrokenPipeError): pass
class Api:
    def __init__(self): self.drafts=[];self.empty=False;self.phase='idle';self.applied=[];self.polls=0
    def handle(self,route):
        req=route.request;path=req.url.split('?')[0].split('/api/',1)
        if len(path)<2:return route.continue_()
        path=path[1]
        if path=='auth/me':return route.fulfill(json={'actor':{'kind':'human','name':'admin'},'nightshift_url':'#'})
        if path=='projects':return route.fulfill(json={'projects':[PROJECT,dict(PROJECT,key='TO',name='없음',local_path='')]})
        if path.endswith('/initialization'):return route.fulfill(status=404,json={'detail':'not initialized'})
        if path=='agents':return route.fulfill(json={'agents':[]})
        if path=='settings/tokens':return route.fulfill(json={'unit':'auto'})
        if path.endswith('/brand'):
            if '/TO/' in path:return route.fulfill(json={'available':False,'reason':'로컬 폴더가 없어요 — 프로젝트 경로를 확인해 주세요'})
            phase=self.phase
            if phase in ('committing','merging'):
                self.polls+=1
                if self.polls>1:self.phase='done'
                else:self.phase='merging'
            return route.fulfill(json={'available':True,'reason':'','html':['static/index.html'],'manifest':{'values':{'name':'nightshift','short_name':'NS','description':'작업실','background_color':'#14161B','theme_color':'#14161B'}},'slots':[{'id':s,'label':s,'editable':True,'files':[]} for s in ('icon','maskable','favicon','splash','og','meta')],'history':[],'apply':{'phase':phase,'sha':'abc123456789' if phase=='done' else ''}})
        if path.endswith('/draft'):
            self.drafts.append(req.post_data_buffer)
            return route.fulfill(json={'draft_id':'mock-draft','source':{'width':1024,'height':1024,'transparent':True,'suggested_colors':['#112233','#334455','#556677']},'previews':{s:[{'name':s+'.png','url':'/api/preview.png','width':512,'height':512}] for s in ('icon','maskable','favicon','splash','og')},'changes':[] if self.empty else [{'path':'static/icon-512.png','status':'overwrite','width':512,'height':512,'bytes':1024},{'path':'static/index.html','status':'tags','bytes':200,'diff':'- old\n+ new'}],'unchanged':[],'warnings':[],'blocked':{}})
        if path=='preview.png':return route.fulfill(body=PNG,content_type='image/png')
        if path.endswith('/apply'):
            self.applied.append(json.loads(req.post_data));self.phase='committing';self.polls=0
            return route.fulfill(status=202,json={'apply':{'phase':'waiting'}})
        return route.fulfill(json={'issues':[],'items':[]})
def run():
    CAPTURES.mkdir(parents=True,exist_ok=True)
    server=ThreadingHTTPServer(('127.0.0.1',0),partial(Quiet,directory=str(ROOT/'static')))
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch()
            def new(width=1300,theme='light',key='NS'):
                api=Api();page=browser.new_page(viewport={'width':width,'height':900},reduced_motion='reduce');page.route('**/*',api.handle)
                page.goto(f'http://127.0.0.1:{server.server_port}/#/projects?project={key}&tab=brand')
                page.evaluate('(t)=>document.documentElement.dataset.theme=t',theme)
                return page,api
            try:
                page,api=new();expect(page.locator('#brand-drop')).to_be_visible();page.reload();expect(page.locator('#brand-drop')).to_be_visible();assert 'tab=brand' in page.url; expect(page.locator('[data-project-tab=brand]')).to_have_text('\ube0c\ub79c\ub4dc')
                # 실제 파일 드롭 이벤트와 서버 생성 PNG 표시를 확인한다.
                page.locator('#brand-drop').evaluate('''async el=>{const c=document.createElement('canvas');c.width=c.height=1024;const b=await new Promise(r=>c.toBlob(r));const dt=new DataTransfer();dt.items.add(new File([b],'icon.png',{type:'image/png'}));el.dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:dt}));}''')
                expect(page.locator('#brand-new-models img')).to_have_count(5)
                assert b'name="icon"' in api.drafts[-1]
                # ???? ??? ??? ??? ???? ??? ??? ? ??.
                page.locator('#brand-drop').evaluate('''async el=>{const c=document.createElement('canvas');c.width=1200;c.height=1024;const b=await new Promise(r=>c.toBlob(r));const dt=new DataTransfer();dt.items.add(new File([b],'wide.png',{type:'image/png'}));el.dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:dt}));}''')
                expect(page.locator('.brand-crop-dialog')).to_be_visible()
                page.locator('.brand-crop-stage').press('Shift+ArrowRight')
                page.locator('[data-ok]').click()
                expect(page.locator('.brand-crop-dialog')).to_have_count(0)
                expect(page.locator('#brand-apply')).to_be_enabled()
                assert b'"crop":{"x":98,' in api.drafts[-1]
                page.locator('[data-option="padding"]').fill('12');expect(page.locator('#brand-apply')).to_be_disabled();expect(page.locator('#brand-apply')).to_be_enabled();assert b'name="draft_id"' in api.drafts[-1]
                page.locator('#brand-apply').click();expect(page.locator('#brand-progress')).to_contain_text('다른 작업 병합');expect(page.locator('#brand-apply')).to_be_disabled()
                expect(page.locator('#brand-progress')).to_contain_text('커밋 중',timeout=6000)
                expect(page.locator('#brand-progress')).to_contain_text('반영 중',timeout=6000)
                expect(page.locator('#brand-progress')).to_contain_text('완료',timeout=6000);assert api.applied==[{'draft_id':'mock-draft'}]
                api.empty=True;page.locator('#brand-rebuild').click();expect(page.locator('#brand-changes')).to_contain_text('바뀐 파일이 없어요');expect(page.locator('#brand-apply')).to_be_disabled();page.close()
                page,_=new(key='TO');expect(page.locator('[data-project-tab="brand"]')).to_be_disabled();expect(page.locator('.brand-panel')).to_contain_text('로컬 폴더가 없어요');page.close()
                for theme in ('light','dark'):
                    for width in (1300,390):
                        page,api=new(width,theme);expect(page.locator('#brand-drop')).to_be_visible();page.locator('#brand-rebuild').click();expect(page.locator('#brand-new-models img')).to_have_count(5)
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),f'가로 넘침: {theme} {width}'
                        assert page.locator('.brand-panel').evaluate('(p)=>p.scrollWidth<=p.clientWidth'),f'패널 넘침: {theme} {width}'
                        expect(page.locator('#project-initialization')).to_be_hidden()
                        page.evaluate('async()=>{scrollTo(0,0);await new Promise(requestAnimationFrame);await new Promise(requestAnimationFrame)}')
                        page.screenshot(path=str(CAPTURES/f'{theme}-{width}.png'),full_page=True);page.close()
            finally:browser.close()
    finally:server.shutdown();server.server_close();thread.join()
    print(f'PASS: 브랜드 URL·드롭·재생성·적용 단계·변경 없음·TO 비활성·낮밤 1300/390. 캡처: {CAPTURES}')
if __name__=='__main__':run()
