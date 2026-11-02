"""블록↔화면 대응표 도구(scripts/ui_block_map.py, DEV-233-2) — 가짜 페이지 수집→이름 변환, 중첩 함수·@media, 바뀐 줄→이름·모름을 확인한다."""
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import ui_block_map as m

JS = """'use strict';
// 머리 주석
const LIMIT = 3;
function clicked() {
  const inner = () => { document.body.dataset.x = '한글 ✓'; };
  inner();
}

async function never(a) {
  return `템플릿
${a}
끝`;
}
const arrow = (x) => {
  return x / 2;
};
document.getElementById('go').addEventListener('click', clicked);
"""

CSS = """/* 공용 */
.used { color: red; }
.unused { color: blue; }
@media (min-width: 1px) {
  .wide,
  .wide2 { margin: 0; }
}
@keyframes spin { from { opacity: 0; } to { opacity: 1; } }
"""

HTML = "<!doctype html><link rel=stylesheet href='/static/app.css?v=1'><div class='used wide'></div><button id=go>go</button><script src='/static/app.js?v=1'></script>"


def check_segments():
    segs, certain = m.js_segments(JS)
    assert certain, segs
    names = {s[2]: (s[0], s[1]) for s in segs}
    assert names['clicked'] == (4, 7) and names['never'] == (9, 13) and names['arrow'] == (14, 16), segs
    assert m.changed_names('static/app.js', JS, [5]) == {'names': ['clicked'], 'unknown': []}   # 중첩 함수 → 최상위
    assert m.changed_names('static/app.js', JS, [11, 8]) == {'names': ['never'], 'unknown': []}   # 템플릿 안 줄, 빈 줄은 무시
    assert m.changed_names('static/app.js', JS, [3, 17])['names'] == ['<top>']
    assert m.changed_names('static/app.js', JS, [2]) == {'names': [], 'unknown': [2]}   # 함수 밖 주석 줄은 모름
    broken = JS.replace('  inner();\n}', '  inner();\n')   # 닫는 괄호가 빠지면 경계를 확신하지 않는다
    assert m.changed_names('static/app.js', broken, [5]) == {'names': [], 'unknown': [5]}
    assert m.changed_names('static/index.html', HTML, [1]) == {'names': [], 'unknown': [1]}
    print('OK — JS 경계·중첩 함수·모름')


def check_css():
    rules, certain = m.css_rules(CSS)
    assert certain
    assert [r[2] for r in rules] == ['.used', '.unused', '@media (min-width: 1px) { .wide, .wide2 }', '@keyframes spin'], rules
    assert m.changed_names('static/app.css', CSS, [6]) == {'names': ['@media (min-width: 1px) { .wide, .wide2 }'], 'unknown': []}
    assert m.changed_names('static/app.css', CSS, [4]) == {'names': [], 'unknown': [4]}   # @media 머리말 자체는 모름
    assert m.changed_names('static/app.css', CSS + '.x {', [2])['unknown'] == [2]   # 닫히지 않은 규칙
    print('OK — CSS 선택자·@media·모름')


def check_collect():
    from playwright.sync_api import sync_playwright
    files = {'app.js': (JS, 'application/javascript'), 'app.css': (CSS, 'text/css'), 'index.html': (HTML, 'text/html')}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        def serve(route):
            name = route.request.url.split('?')[0].rsplit('/', 1)[-1] or 'index.html'
            body, kind = files[name]
            route.fulfill(body=body, content_type=kind + '; charset=utf-8')
        page.route('http://fake.test/**', serve)
        c = m.Collector(page)
        c.start()
        page.goto('http://fake.test/')
        page.click('#go')
        page.goto('http://fake.test/')   # 이동 앞의 CSS 사용도 남는다
        raw = c.stop()
        assert raw['complete'], raw
        texts = {'static/app.js': JS, 'static/app.css': CSS}
        names = m.block_names(json.loads(json.dumps(raw)), texts)
        assert set(names['js']['static/app.js']) == {'<top>', 'clicked'}, names   # never·arrow는 안 불렸다
        assert set(names['css']['static/app.css']) == {'.used', '@media (min-width: 1px) { .wide, .wide2 }'}, names
        c.start()   # 새 탭을 열면 그 블록은 불완전
        page.context.browser.new_context().close()
        assert not c.stop()['complete']
        browser.close()
    print('OK — 가짜 페이지 수집→이름')


def check_build():
    built = m.build_map({'a': {'js': {}, 'css': {}, 'complete': True, 'ok': True}, 'b': {'js': {}, 'css': {}, 'complete': True, 'ok': False}}, ['a', 'b', 'c'])
    assert {k: v['complete'] for k, v in built['blocks'].items()} == {'a': True, 'b': False, 'c': False}
    assert set(built['generated_from']) == set(m.JS_FILES + m.CSS_FILES + ('static/index.html',))
    saved = json.loads(m.MAP_PATH.read_text(encoding='utf-8'))   # 커밋된 대응표가 블록 표 전부를 담는다
    ids = json.loads(__import__('subprocess').run([sys.executable, str(ROOT / 'tests' / 'test_ui.py'), '--list-blocks'], capture_output=True, text=True, check=True).stdout)
    assert set(saved['blocks']) == {b['id'] for b in ids}, set(saved['blocks']) ^ {b['id'] for b in ids}
    print('OK — 대응표 만들기')


def check_git_shas():
    """대응표의 generated_from은 git이 아는 blob이어야 한다 — 작업 폴더 바이트(CRLF)를 해시하면 git에 없는 sha가 되어
    서버(regression.ui_map_pick)가 기준 파일을 못 찾고 화면 변경마다 전체 1회로 떨어진다(DEV-233)."""
    import subprocess
    saved = json.loads((ROOT / 'tests' / 'ui_block_map.json').read_text(encoding='utf-8'))['generated_from']
    for rel, sha in saved.items():
        assert subprocess.run(['git', 'cat-file', '-e', sha], cwd=ROOT).returncode == 0, (rel, sha)
    for rel in saved:
        expected = subprocess.run(['git', 'hash-object', '--', rel], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
        assert m.file_blob_sha(ROOT, rel) == expected, rel
    print('OK — map shas are git blobs')


check_segments()
check_css()
check_collect()
check_build()
check_git_shas()
print('OK')
