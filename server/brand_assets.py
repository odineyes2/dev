"""브랜드 자원의 추적 경로·태그 위치·현재 파일 정보를 읽는다."""
import hashlib
import io
import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit, urlunsplit, parse_qsl, urlencode

SPLASH_SCREENS = [(440,956,3),(402,874,3),(430,932,3),(393,852,3),(428,926,3),(390,844,3),(375,812,3),(414,896,3),(414,896,2),(375,667,2)]


def splash_media(width, height, ratio):
    return f'(device-width: {width}px) and (device-height: {height}px) and (-webkit-device-pixel-ratio: {ratio}) and (orientation: portrait)'


class Tags(HTMLParser):
    """태그 원문과 문자 오프셋을 보존한다."""
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.text, self.tags = text, []
        self.lines = [0] + [m.end() for m in re.finditer('\n', text)]
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        rel = (a.get('rel') or '').lower().split()
        name = (a.get('name') or a.get('property') or '').lower()
        slot = None
        if tag == 'link':
            for key, value in [('manifest','meta'), ('icon','favicon'), ('apple-touch-icon','icon'), ('apple-touch-startup-image','splash')]:
                if key in rel:
                    slot = value
                    break
        elif tag == 'meta' and (name.startswith('og:') or name in ('theme-color','apple-mobile-web-app-capable','apple-mobile-web-app-title','twitter:card')):
            slot = 'og' if name.startswith('og:') or name == 'twitter:card' else 'meta'
        elif tag == 'img':
            slot = 'art'
        if slot:
            line, col = self.getpos()
            start = self.lines[line - 1] + col
            raw = self.get_starttag_text()
            self.tags.append(dict(tag=tag, attrs=a, slot=slot, start=start, end=start+len(raw), raw=raw))

    handle_startendtag = handle_starttag


def safe_file(root, path):
    """저장소 밖·숨김 파일·실행 데이터 경로를 허용하지 않는다."""
    if not path or '\\' in path or '\x00' in path:
        return None
    parts = PurePosixPath(path).parts
    if any(p in ('.', '..', 'data') or p.startswith('.') or ':' in p for p in parts) or path.startswith('/'):
        return None
    try:
        candidate = root / path
        if any(p.is_symlink() for p in (candidate, *candidate.parents) if p != root.parent):
            return None
        target = candidate.resolve()
        target.relative_to(root.resolve())
        return target if target.is_file() else None
    except (OSError, ValueError):
        return None


def resolve_url(root, tracked, url, base):
    try:
        value = urlsplit(url or '')
    except ValueError:
        return None
    if value.scheme or value.netloc:
        return None
    path = unquote(value.path)
    if not path or '\\' in path or '..' in PurePosixPath(path).parts:
        return None
    candidates = ([path.lstrip('/'), 'static/' + path.lstrip('/')] if path.startswith('/') else
                  [str(PurePosixPath(base).parent / path), 'static/' + path, path])
    return next((p for p in candidates if p in tracked and safe_file(root, p)), None)


def inspect_repo(local_path, key=''):
    root = Path(local_path or '')
    result = dict(available=False, reason='', html=[], manifest=None, slots=[], history=[],
                  apply=dict(phase='idle', sha=None, error=None, log='', warnings=[]))
    try:
        if not local_path or not root.is_dir():
            raise ValueError()
        proc = subprocess.run(['git','-C',str(root),'ls-files','-z'], capture_output=True, check=True, timeout=15)
        tracked = set(proc.stdout.decode('utf-8').split('\0')) - {''}
    except (OSError, ValueError, subprocess.SubprocessError):
        result['reason'] = '로컬 폴더가 없거나 git 저장소가 아니에요 — 프로젝트 경로를 확인해 주세요'
        return result
    entries = []
    for path in sorted(tracked):
        file = safe_file(root, path)
        if not path.endswith('.html') or not file:
            continue
        try:
            tags = Tags(file.read_text('utf-8')).tags
        except (UnicodeError, OSError):
            continue
        if any(t['tag'] == 'link' and 'manifest' in (t['attrs'].get('rel') or '').split() for t in tags):
            entries.append((path, tags))
    if not entries:
        result['reason'] = 'manifest를 가리키는 HTML을 찾지 못했어요'
        return result
    signatures = [{(t['slot'], t['attrs'].get('href','')) for t in ts if t['tag'] == 'link'} for _,ts in entries]
    if any(s != signatures[0] for s in signatures):
        result['reason'] = 'HTML 진입점이 여러 개라 자동으로 고칠 수 없어요'
        return result
    result['html'] = [p for p,_ in entries]
    labels = dict(icon='앱 아이콘', maskable='마스커블 아이콘', favicon='파비콘', splash='iOS 시작 화면', app_splash='앱 시작 그림', og='공유 이미지', meta='이름·색', art='화면 그림')
    slots = {s:dict(id=s,label=labels[s],editable=True,reason='',files=[]) for s in ('icon','favicon','splash','og','meta')}

    def slot(s):
        return slots.setdefault(s,dict(id=s,label=labels[s],editable=True,reason='',files=[]))

    def add(s, path):
        if not path or any(f['path'] == path for f in slot(s)['files']):
            return
        file = safe_file(root,path)
        if not file:
            return
        width = height = None
        fmt = file.suffix.lstrip('.').lower()
        try:
            from PIL import Image
            with Image.open(io.BytesIO(file.read_bytes())) as im:
                width,height = im.size
                fmt = im.format.lower()
        except (ImportError, OSError, ValueError):
            pass
        slot(s)['files'].append(dict(path=path,url=f'/api/projects/{quote(key, safe="")}/brand/file?path={quote(path, safe="")}',width=width,height=height,bytes=file.stat().st_size,format=fmt))

    manifest_path = None
    for html, tags in entries:
        for t in tags:
            s, a = t['slot'], t['attrs']
            if '{{' in t['raw'] or '{%' in t['raw']:
                slot(s).update(editable=False, reason='브랜드 태그에 서버 자리표시가 있어 자동으로 고칠 수 없어요')
                continue
            url = a.get('src') if t['tag'] == 'img' else a.get('href') if t['tag'] == 'link' else a.get('content') if s == 'og' and a.get('property') == 'og:image' else None
            path = resolve_url(root,tracked,url,html) if url else None
            if t['tag'] == 'link' and 'manifest' in (a.get('rel') or '').split():
                manifest_path = path
            elif path:
                add(s,path)
    if not manifest_path:
        result['reason'] = 'manifest 경로를 확인할 수 없어요'
        return result
    try:
        manifest = json.loads(safe_file(root,manifest_path).read_text('utf-8'))
        if not isinstance(manifest,dict) or not isinstance(manifest.get('icons',[]),list):
            raise ValueError()
    except (OSError, ValueError, TypeError):
        result['reason'] = 'manifest를 읽을 수 없어요 — JSON 형식을 확인해 주세요'
        return result
    result['manifest'] = dict(path=manifest_path, values={k:manifest.get(k,'') for k in ('name','short_name','description','theme_color','background_color')})
    for icon in manifest.get('icons',[]):
        if not isinstance(icon,dict):
            continue
        purposes = (icon.get('purpose') or 'any').split()
        for purpose, s in [('any','icon'),('maskable','maskable')]:
            if purpose in purposes:
                add(s,resolve_url(root,tracked,icon.get('src'),manifest_path))
    for path in sorted(tracked):
        if path.startswith('static/') and re.search(r'/(?:splash|splash-wide)\.(?:webp|png|jpg)$',path):
            add('app_splash',path)
    result.update(available=True,slots=list(slots.values()))
    return result


def project_root(key):
    import db
    import issues
    with db.connect() as c:
        row = c.execute('SELECT local_path FROM projects WHERE key=?',(key,)).fetchone()
    if not row:
        raise issues.StoreError('프로젝트를 찾지 못했어요',404)
    return row['local_path']


def status(key):
    return inspect_repo(project_root(key),key)


def current_file(key,path):
    root = Path(project_root(key) or '')
    info = status(key)
    allowed = {f['path'] for s in info['slots'] for f in s['files']}
    if info['manifest']:
        allowed.add(info['manifest']['path'])
    return safe_file(root,path) if path in allowed else None


def version_links(html, static_dir):
    """DEV에서 실제 존재하는 아이콘·manifest URL에 내용 해시를 붙인다."""
    for t in reversed(Tags(html).tags):
        a = t['attrs']
        if t['tag'] != 'link' or not set((a.get('rel') or '').split()) & {'icon','apple-touch-icon','manifest'}:
            continue
        href = a.get('href') or ''
        parsed = urlsplit(href)
        if parsed.scheme or parsed.netloc:
            continue
        path = unquote(parsed.path).lstrip('/')
        file = safe_file(static_dir,path)
        if not file:
            continue
        query = [(k,v) for k,v in parse_qsl(parsed.query) if k != 'v']
        query.append(('v',hashlib.sha1(file.read_bytes()).hexdigest()[:10]))
        url = urlunsplit(('', '',parsed.path,urlencode(query),parsed.fragment))
        from html import escape
        raw = re.sub(r'(\bhref\s*=\s*)([\"\x27])(.*?)\2', lambda m:m[1]+m[2]+escape(url,quote=True)+m[2],t['raw'],count=1,flags=re.I|re.S)
        html = html[:t['start']] + raw + html[t['end']:]
    return html
