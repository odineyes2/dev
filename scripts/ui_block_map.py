"""블록↔화면 대응표(DEV-233-2) — test_ui 블록마다 쓴 static의 JS 최상위 함수·CSS 선택자를 모아 tests/ui_block_map.json을 만든다.

`python scripts/ui_block_map.py`는 `tests/test_ui.py --coverage <임시 파일>`을 돌려 CDP 원본(실행된 함수·사용된 규칙의 시작 위치)을
받고, 그 위치를 지금 파일의 이름(함수·선택자)으로 바꾼다. `--raw <파일>`이면 검사를 다시 돌리지 않고 그 원본으로 만든다.
서버(DEV-233-3)는 `changed_names`로 바뀐 줄을 같은 이름으로 바꾼다 — 경계를 확신할 수 없는 줄은 "모름"(unknown)으로 돌려준다.
"""
import bisect, hashlib, json, re, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAP_PATH = ROOT / 'tests' / 'ui_block_map.json'
TOP = '<top>'   # 함수 밖의 최상위 코드
JS_FILES = ('static/app.js', 'static/font-settings.js')
CSS_FILES = ('static/app.css', 'static/family.css')
_JS_NAME = re.compile(r'(?:export\s+)?(?:async\s+)?function\s*\*?\s*([\w$]+)'
                      r'|(?:const|let|var)\s+([\w$]+)\s*=\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*=>|[\w$]+\s*=>)')
_REGEX_BEFORE = set('(,=:[!&|?{};+-*%<>~^')


def blob_sha(data):
    """git blob sha — git 없이 내용으로 계산한다(generated_from 비교용). 내용은 git에 저장되는 모양(LF)이어야 한다."""
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


def file_blob_sha(root, rel):
    """작업 폴더 파일의 git blob sha — `git hash-object`로 git과 같은 변환(core.autocrlf의 CRLF→LF)을 거친다.
    작업 폴더 바이트를 그대로 해시하면 Windows(CRLF)에서 git에 없는 sha가 되어 서버가 대응표를 늘 낡았다고 본다(DEV-233)."""
    try:
        r = subprocess.run(['git', 'hash-object', '--', rel], cwd=root, capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return blob_sha((Path(root) / rel).read_bytes().replace(b'\r\n', b'\n'))


def _utf16_lines(text):
    """줄마다 시작 위치(UTF-16 단위 — CDP·V8 위치 단위)."""
    starts, pos = [0], 0
    for line in text.split('\n')[:-1]:
        pos += len(line.encode('utf-16-le')) // 2 + 1
        starts.append(pos)
    return starts


def line_of(text, offset, _cache={}):
    """UTF-16 위치 → 1부터 세는 줄 번호."""
    starts = _cache.get(text) or _cache.setdefault(text, _utf16_lines(text))
    return bisect.bisect_right(starts, offset)


def js_segments(text):
    """최상위 문장 단위로 줄을 나눈다 → ([(첫 줄, 끝 줄, 이름)], 확신 여부).

    이름은 최상위 함수(`function f`·`async function f`·`const f = (…) =>`)면 그 이름, 그 밖의 문장은 `<top>`이다.
    문자열·템플릿·주석·정규식을 건너뛰며 괄호 깊이를 세고, 끝에서 깊이가 0이 아니거나 닫히지 않은 문자열이 있으면 확신하지 않는다.
    """
    lines = text.split('\n')
    at_line = []   # 줄 시작에서 (깊이, 문자열·주석 안인지)
    depth, mode, tmpl, prev, i, n = 0, None, [], '', 0, len(text)
    line_start = {0}
    pos = 0
    for line in lines[:-1]:
        pos += len(line) + 1
        line_start.add(pos)
    while i <= n:
        if i in line_start:
            at_line.append((depth, mode is not None))
        if i == n:
            break
        c = text[i]
        if mode == 'line':
            if c == '\n':
                mode = None
        elif mode == 'block':
            if text.startswith('*/', i):
                mode = None; i += 1
        elif mode in ('"', "'"):
            if c == '\\':
                i += 1
            elif c == mode or c == '\n':
                mode = None; prev = 'a'
        elif mode == '`':
            if c == '\\':
                i += 1
            elif c == '`':
                mode = None; prev = 'a'
            elif text.startswith('${', i):
                tmpl.append(depth); depth += 1; mode = None; i += 1
        elif mode == '/':
            if c == '\\':
                i += 1
            elif c == '[':
                mode = '/['
            elif c == '/' or c == '\n':
                mode = None; prev = 'a'
        elif mode == '/[':
            if c == '\\':
                i += 1
            elif c == ']':
                mode = '/'
        elif text.startswith('//', i):
            mode = 'line'
        elif text.startswith('/*', i):
            mode = 'block'; i += 1
        elif c in '"\'`':
            mode = c
        elif c == '/' and (prev == '' or prev in _REGEX_BEFORE):
            mode = '/'
        elif c in '{([':
            depth += 1; prev = c
        elif c in '})]':
            depth -= 1; prev = c
            if c == '}' and tmpl and depth == tmpl[-1]:
                tmpl.pop(); mode = '`'
        elif not c.isspace():
            prev = c if c in _REGEX_BEFORE else 'a'
            if c.isalnum() or c in '_$':   # return/typeof 뒤의 / 는 정규식
                j = i
                while j < n and (text[j].isalnum() or text[j] in '_$'):
                    j += 1
                word = text[i:j]
                prev = '(' if word in ('return', 'typeof', 'case', 'in', 'of', 'delete', 'void', 'throw', 'new') else 'a'
                i = j; continue
        i += 1
    certain = depth == 0 and mode in (None, 'line') and not tmpl
    segments = []
    for no, line in enumerate(lines, 1):
        d, inside = at_line[no - 1] if no - 1 < len(at_line) else (1, True)
        s = line.strip()
        if d or inside or not line or line[0].isspace() or line[0] in '})]' or s.startswith(('//', '/*', '*')):
            continue
        m = _JS_NAME.match(line)
        segments.append([no, no, (m.group(1) or m.group(2)) if m else TOP])
    for k, seg in enumerate(segments):   # 다음 문장 앞의 빈 줄·주석 줄은 빼고 끝을 정한다
        end = segments[k + 1][0] - 1 if k + 1 < len(segments) else len(lines)
        while end > seg[0] and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith(('//', '/*', '*')) and not lines[end - 1].startswith((' ', '\t', '}'))):
            end -= 1
        seg[1] = end
    return [tuple(s) for s in segments], certain


def css_rules(text):
    """CSS 규칙 → ([(시작 위치, 끝 위치, 이름)], 확신 여부). 위치는 파이썬 문자 단위.

    이름은 선택자 문자열(공백 정리)이고 감싸는 `@media`·`@supports` 등이 있으면 `@media (…) { 선택자 }`로 감싼다.
    `@keyframes`·`@font-face`는 머리말을 이름으로 한 규칙 하나다. 규칙 안의 중첩 규칙은 바깥 규칙에 속한다.
    """
    rules, stack, i, n, start = [], [], 0, len(text), None
    while i < n:
        c = text[i]
        if text.startswith('/*', i):
            j = text.find('*/', i + 2)
            if j < 0:
                return rules, False
            i = j + 2; continue
        if c in '"\'':
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == '\\' else 1
            i = j + 1; continue
        if start is None and not c.isspace() and c not in '{};':
            start = i
        if c == '{':
            pre = ' '.join(text[start if start is not None else i:i].split())
            if stack and stack[-1][0] != 'group':
                stack.append(('inner', None))
            elif re.match(r'@(media|supports|container|layer|document)\b', pre):
                stack.append(('group', pre))
            else:
                name = pre
                for kind, cond in reversed(stack):
                    name = f'{cond} {{ {name} }}'
                rules.append([start if start is not None else i, None, name])
                stack.append(('rule', len(rules) - 1))
            start = None
        elif c == '}':
            if not stack:
                return rules, False
            kind, ref = stack.pop()
            if kind == 'rule':
                rules[ref][1] = i + 1
            start = None
        elif c == ';':
            start = None
        i += 1
    return [tuple(r) for r in rules if r[1] is not None], not stack and start is None


def _css_lines(text, rules):
    """규칙마다 (첫 줄, 끝 줄, 이름)."""
    starts = [0]
    for line in text.split('\n')[:-1]:
        starts.append(starts[-1] + len(line) + 1)
    return [(bisect.bisect_right(starts, s), bisect.bisect_right(starts, e - 1), name) for s, e, name in rules]


def _utf16_to_index(text, offset):
    return len(text.encode('utf-16-le')[:offset * 2].decode('utf-16-le', 'ignore'))


def js_names_from_offsets(text, offsets):
    """실행된 함수의 시작 위치(UTF-16) → 최상위 함수 이름 집합(중첩 함수는 감싸는 최상위 함수, 함수 밖은 `<top>`)."""
    segments, _ = js_segments(text)
    firsts = [s[0] for s in segments]
    names = set()
    for off in offsets:
        if off == 0:
            names.add(TOP); continue
        line = line_of(text, off)
        k = bisect.bisect_right(firsts, line) - 1
        names.add(segments[k][2] if k >= 0 and segments[k][0] <= line <= segments[k][1] else TOP)
    return names


def css_names_from_offsets(text, offsets):
    """사용된 규칙의 시작 위치(UTF-16) → 선택자 이름 집합(그 위치를 품은 바깥 규칙)."""
    rules, _ = css_rules(text)
    names = set()
    for off in offsets:
        idx = _utf16_to_index(text, off)
        hits = [r for r in rules if r[0] <= idx < r[1]]
        if hits:
            names.add(min(hits, key=lambda r: r[1] - r[0])[2])
    return names


def changed_names(path, text, lines):
    """바뀐 줄(지금 파일 기준 1부터) → {'names': [이름…], 'unknown': [줄…]}.

    빈 줄은 무시한다. 함수·규칙 경계 밖이거나 파일 구조를 확신할 수 없으면 그 줄은 unknown이다 — 부르는 쪽은 파일 전체를 돈다.
    """
    lines = sorted(set(lines))
    src = text.split('\n')
    if path.endswith('.js'):
        spans, certain = js_segments(text)
    elif path.endswith('.css'):
        rules, certain = css_rules(text)
        spans = _css_lines(text, rules)
    else:
        return {'names': [], 'unknown': lines}
    names, unknown = set(), []
    for no in lines:
        if 1 <= no <= len(src) and not src[no - 1].strip():
            continue
        hit = [s for s in spans if s[0] <= no <= s[1]] if certain else []
        if hit:
            names.update(s[2] for s in hit)
        else:
            unknown.append(no)
    return {'names': sorted(names), 'unknown': unknown}


class Collector:
    """한 페이지에 CDP JS 정밀 커버리지와 CSS 규칙 사용 추적을 붙인다. start()~stop() 사이에 쓴 위치를 URL별로 돌려준다.

    페이지 이동 전에 CSS 사용을 걷어(앞 문서의 규칙이 사라지지 않게) goto·reload를 감싼다. 그 사이 새 탭·새 창(context)이
    열리면 그쪽은 수집하지 못하니 complete를 False로 둔다.
    """

    def __init__(self, page):
        from playwright.sync_api import Browser, BrowserContext
        self.page, self.sheets, self.css, self.other = page, {}, {}, False
        self.session = s = page.context.new_cdp_session(page)
        s.on('CSS.styleSheetAdded', lambda e: self.sheets.__setitem__(e['header']['styleSheetId'], e['header'].get('sourceURL', '')))
        for method in ('DOM.enable', 'CSS.enable', 'Profiler.enable', 'Debugger.enable'):
            s.send(method)
        page.context.on('page', lambda _p: setattr(self, 'other', True))
        for cls, name in ((Browser, 'new_context'), (Browser, 'new_page'), (BrowserContext, 'new_page')):
            orig = getattr(cls, name)
            if not getattr(orig, '_ui_map', False):
                def wrapped(obj, *a, _orig=orig, **k):
                    for c in Collector.active:
                        c.other = True
                    return _orig(obj, *a, **k)
                wrapped._ui_map = True
                setattr(cls, name, wrapped)
        for name in ('goto', 'reload'):
            orig = getattr(page, name)
            def nav(*a, _orig=orig, **k):
                self._take_css()
                return _orig(*a, **k)
            setattr(page, name, nav)
        self.tracking = False

    active = []

    def _take_css(self):
        """이동 전에 지금 문서의 사용 기록을 걷는다 — 이동하면 앞 문서의 스크립트·스타일시트 기록이 사라진다."""
        if self.tracking:
            self._take_js()
            for u in self.session.send('CSS.takeCoverageDelta')['coverage']:
                self._add_css(u)

    def _take_js(self):
        for script in self.session.send('Profiler.takePreciseCoverage')['result']:   # 걷을 때마다 횟수가 0으로 돌아간다
            for fn in script['functions']:
                r = fn['ranges'][0]
                if r['count'] > 0 and script.get('url'):
                    self.js.setdefault(script['url'], set()).add(r['startOffset'])

    def _add_css(self, u):
        if u.get('used'):
            url = self.sheets.get(u['styleSheetId'], '')
            self.css.setdefault(url, set()).add(u['startOffset'])

    def start(self):
        self.js, self.css, self.other = {}, {}, False
        Collector.active.append(self)
        self.session.send('Profiler.startPreciseCoverage', {'callCount': True, 'detailed': False})
        self.session.send('CSS.startRuleUsageTracking')
        self.tracking = True

    def stop(self):
        """수집을 끝내고 {'js': {url: [위치…]}, 'css': {url: [위치…]}, 'complete': bool}을 돌려준다."""
        self._take_css()
        js = self.js
        self.session.send('Profiler.stopPreciseCoverage')
        for u in self.session.send('CSS.stopRuleUsageTracking')['ruleUsage']:
            self._add_css(u)
        self.tracking = False
        Collector.active.remove(self)
        return {'js': {u: sorted(v) for u, v in js.items()}, 'css': {u: sorted(v) for u, v in self.css.items() if u},
                'complete': not self.other}


def _file_for(url, files):
    """수집한 URL → 저장소 상대 경로(static 파일 이름으로 맞춘다, 쿼리는 버린다). 모르는 URL은 None."""
    base = url.split('?')[0].split('#')[0].rsplit('/', 1)[-1]
    return next((f for f in files if f.rsplit('/', 1)[-1] == base), None)


def block_names(raw, texts):
    """블록 하나의 원본 → {'js': {파일: [이름…]}, 'css': {파일: [이름…]}, 'complete': bool}."""
    out = {'js': {}, 'css': {}, 'complete': bool(raw.get('complete')) and raw.get('ok', True)}
    for kind, convert in (('js', js_names_from_offsets), ('css', css_names_from_offsets)):
        for url, offsets in raw.get(kind, {}).items():
            path = _file_for(url, [f for f in texts if f.endswith('.' + kind)])
            if path:
                names = convert(texts[path], offsets)
                out[kind][path] = sorted(names | set(out[kind].get(path, [])))
    return out


def build_map(raw_blocks, block_ids, root=ROOT):
    """블록 표 전체로 대응표를 만든다 — 원본이 없는 블록(실패·미수집)은 빈 이름에 complete False."""
    texts, shas = {}, {}
    for rel in JS_FILES + CSS_FILES + ('static/index.html',):
        data = (root / rel).read_bytes()
        shas[rel] = file_blob_sha(root, rel)
        if not rel.endswith('.html'):
            texts[rel] = data.decode('utf-8')
    blocks = {}
    for bid in block_ids:
        raw = raw_blocks.get(bid)
        blocks[bid] = block_names(raw, texts) if raw else {'js': {}, 'css': {}, 'complete': False}
    return {'generated_from': shas, 'blocks': blocks}


def main(argv):
    blocks = json.loads(subprocess.run([sys.executable, str(ROOT / 'tests' / 'test_ui.py'), '--list-blocks'],
                                       capture_output=True, text=True, check=True, cwd=ROOT).stdout)
    if '--raw' in argv[:-1]:
        raw = json.loads(Path(argv[argv.index('--raw') + 1]).read_text(encoding='utf-8'))
    else:
        out = Path(tempfile.mkdtemp()) / 'coverage.json'
        code = subprocess.run([sys.executable, str(ROOT / 'tests' / 'test_ui.py'), '--coverage', str(out)], cwd=ROOT).returncode
        if not out.exists():
            sys.exit(f'test_ui.py --coverage가 원본을 남기지 않았어요(종료 코드 {code})')
        raw = json.loads(out.read_text(encoding='utf-8'))
    write_map(raw, [b['id'] for b in blocks])


def write_map(raw, block_ids):
    """대응표를 tests/ui_block_map.json에 쓰고 complete 아닌 블록을 알린다(`test_ui.py --block-map`도 이것을 부른다)."""
    result = build_map(raw, block_ids)
    MAP_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True) + '\n', encoding='utf-8')
    bad = [b for b, v in result['blocks'].items() if not v['complete']]
    print(f'{MAP_PATH.relative_to(ROOT)}: 블록 {len(result["blocks"])}개, complete 아님 {len(bad)}개' + (': ' + ','.join(bad) if bad else ''))


if __name__ == '__main__':
    main(sys.argv)
