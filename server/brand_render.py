"""브랜드 원본을 검사하고 결정적인 생성물과 수정 계획을 draft에 보관한다."""
import base64
from concurrent.futures import ThreadPoolExecutor
import difflib
import hashlib
import io
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid
import warnings
import xml.etree.ElementTree as ET

from PIL import Image, ImageDraw, ImageFont, ImageOps
import brand_assets as assets
import config

Image.MAX_IMAGE_PIXELS = 50_000_000
MAX_BYTES = 25 * 1024 * 1024
UPLOADS = {'icon','maskable','favicon','splash','app_splash','app_splash_wide','og','art'}
_LOCK = threading.RLock()


def svg_bytes(data):
    """자체 포함 이미지와 내부 참조만 있는 SVG를 허용한다."""
    try:
        text = data.decode('utf-8-sig')
        if re.search(r'<!DOCTYPE|<!ENTITY|<\s*(?:\w+:)?(?:script|foreignObject)\b|\bon\w+\s*=', text, re.I):
            raise ValueError()
        root = ET.fromstring(text)
        if root.tag.split('}')[-1] != 'svg':
            raise ValueError()
        for node in root.iter():
            for key,value in node.attrib.items():
                if key.split('}')[-1].lower() == 'href' and not (value.startswith('#') or re.match(r'^data:image/(?:png|jpeg|webp);base64,',value)):
                    raise ValueError()
        # CSS도 네트워크·로컬 파일 참조를 품을 수 있으므로 내부 fragment만 허용한다.
        if re.search(r'@import|url\(\s*[\"\x27]?(?!#)',text,re.I):
            raise ValueError()
        return text.encode('utf-8')
    except (UnicodeError, ET.ParseError, ValueError):
        raise ValueError('SVG에 실행 코드나 외부 참조가 있어요 — 자체 포함 SVG를 올려 주세요') from None


def browser_png(html, width, height):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            raise ValueError('이미지 렌더러를 시작하지 못했어요 — Chromium 설치를 확인해 주세요') from exc
        try:
            page = browser.new_page(viewport={'width':width,'height':height},device_scale_factor=1)
            page.route('**/*',lambda route:route.abort())
            page.set_content(html,wait_until='load')
            page.evaluate('document.fonts.ready')
            return page.screenshot(omit_background=True,animations='disabled')
        finally:
            browser.close()


def load_image(data):
    if not data or len(data)>MAX_BYTES:
        raise ValueError('이미지는 25MB 이하로 올려 주세요')
    svg = None
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        expected = 'PNG'
    elif data.startswith(b'\xff\xd8\xff'):
        expected = 'JPEG'
    elif data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        expected = 'WEBP'
    elif data.lstrip(b'\xef\xbb\xbf \r\n\t').startswith((b'<svg',b'<?xml')):
        svg = svg_bytes(data)
        encoded = base64.b64encode(svg).decode()
        data = browser_png(f'<style>html,body{{margin:0;background:transparent}}img{{width:1024px;height:1024px;object-fit:contain}}</style><img src="data:image/svg+xml;base64,{encoded}">',1024,1024)
        expected = 'PNG'
    else:
        raise ValueError('PNG·JPEG·WebP·SVG 이미지를 올려 주세요')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as im:
                if im.format != expected:
                    raise ValueError('이미지 실제 형식이 맞지 않아요')
                im.load()
                result = ImageOps.exif_transpose(im).convert('RGBA')
                result.info.clear()
                return result,svg
    except (OSError,Image.DecompressionBombError,Image.DecompressionBombWarning):
        raise ValueError('이미지를 읽을 수 없거나 너무 커요 — 다른 이미지를 올려 주세요') from None


def encode(image, fmt, background):
    out = io.BytesIO()
    fmt = fmt.lower()
    if fmt in ('jpg','jpeg','webp','ico'):
        image = flatten(image,background)
    if fmt in ('jpg','jpeg'):
        image.save(out,'JPEG',quality=88,optimize=True,progressive=False)
    elif fmt=='webp':
        image.save(out,'WEBP',quality=85,method=6)
    elif fmt=='ico':
        image.resize((48,48),Image.Resampling.LANCZOS).save(out,'ICO',sizes=[(16,16),(32,32),(48,48)])
    elif fmt=='svg':
        png = encode(image,'png',background)
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{image.width}" height="{image.height}" viewBox="0 0 {image.width} {image.height}"><image width="100%" height="100%" href="data:image/png;base64,{base64.b64encode(png).decode()}"/></svg>'.encode()
    else:
        image.save(out,'PNG',optimize=True)
    return out.getvalue()


def flatten(im,background):
    result = Image.new('RGBA',im.size,background)
    result.alpha_composite(im.convert('RGBA'))
    return result.convert('RGB')


def square(im,crop,min_size=512):
    if crop is not None:
        try:
            x,y,size = [float(crop[k]) for k in ('x','y','size')]
            if not all(math.isfinite(v) for v in (x,y,size)) or min(x,y)<0 or size<=0 or x+size>im.width or y+size>im.height:
                raise ValueError()
            im = im.crop((round(x),round(y),round(x+size),round(y+size)))
        except (KeyError,TypeError,ValueError):
            raise ValueError('자르기 범위가 원본 밖이에요') from None
    if im.width!=im.height:
        raise ValueError('정사각형으로 자른 뒤 다시 만들어 주세요')
    if im.width<min_size:
        raise ValueError('아이콘은 512px 이상이어야 해요')
    return im


def icon_image(im,size,background,padding=0,rounding=False,opaque=False,maskable=False):
    width,height = size
    factor = min(width,height)*(0.8/math.sqrt(2) if maskable else 1-2*padding/100)
    inner = max(1,round(factor))
    resized = im.resize((inner,inner),Image.Resampling.LANCZOS)
    if rounding:
        mask = Image.new('L',resized.size)
        ImageDraw.Draw(mask).rounded_rectangle((0,0,inner-1,inner-1),round(inner*.22),fill=255)
        from PIL import ImageChops
        resized.putalpha(ImageChops.multiply(resized.getchannel('A'),mask))
    result = Image.new('RGBA',size,background if opaque or maskable else (0,0,0,0))
    result.alpha_composite(resized,((width-inner)//2,(height-inner)//2))
    return flatten(result,background).convert('RGBA') if opaque or maskable else result


def cover(im,size,focal=None):
    focal = focal or {'x':.5,'y':.5}
    x,y = float(focal.get('x',.5)),float(focal.get('y',.5))
    if not all(math.isfinite(v) and 0<=v<=1 for v in (x,y)):
        raise ValueError('초점은 이미지 안에서 골라 주세요')
    scale = max(size[0]/im.width,size[1]/im.height)
    resized = im.resize((math.ceil(im.width*scale),math.ceil(im.height*scale)),Image.Resampling.LANCZOS)
    left,top = round((resized.width-size[0])*x),round((resized.height-size[1])*y)
    return resized.crop((left,top,left+size[0],top+size[1]))


def text_layer(text):
    from html import escape
    font = Path(__file__).resolve().parents[1]/'static/fonts/IBMPlexMono-600.woff2'
    text = str(text).upper()[:120]
    try:
        face = ImageFont.truetype(str(font),32)
        layer = Image.new('RGBA',(1600,64))
        draw = ImageDraw.Draw(layer)
        x=0
        for char in text:
            draw.text((x,8),char,font=face,fill='white')
            x+=draw.textlength(char,font=face)+6.4
        return layer.crop((0,0,max(1,min(1600,math.ceil(x))),64))
    except OSError:
        encoded = base64.b64encode(font.read_bytes()).decode()
        html = f'<style>@font-face{{font-family:brand;src:url(data:font/woff2;base64,{encoded})}}html,body{{margin:0;background:transparent}}span{{font:600 32px brand;color:white;letter-spacing:.2em;white-space:nowrap}}</style><span>{escape(text)}</span>'
        data = browser_png(html,1600,64)
        layer = Image.open(io.BytesIO(data)).convert('RGBA')
        box = layer.getbbox()
        return layer.crop(box) if box else Image.new('RGBA',(1,1))


def composition(im,size,background,scale=.25,label=None,og=False):
    result = Image.new('RGBA',size,background)
    side = max(1,round(min(size)*scale))
    icon = im.resize((side,side),Image.Resampling.LANCZOS)
    y = (size[1]-side)//2 - (side//5 if label else 0)
    result.alpha_composite(icon,((size[0]-side)//2,y))
    if label:
        width = min(round(size[0]*.8),round(label.width*size[0]/1200))
        height = max(1,round(label.height*width/label.width))
        layer = label.resize((max(1,width),height),Image.Resampling.LANCZOS)
        result.alpha_composite(layer,((size[0]-layer.width)//2,min(size[1]-height,y+side+max(10,side//5))))
    return flatten(result,background).convert('RGBA')


def drafts_root():
    return config.DATA_DIR/'brand-drafts'


def draft_dir(identifier):
    if not isinstance(identifier,str) or not re.fullmatch('[0-9a-f]{32}',identifier):
        raise ValueError('draft를 찾지 못했어요 — 다시 만들어 주세요')
    return drafts_root()/identifier


def read_draft(key,identifier):
    folder = draft_dir(identifier)
    try:
        metadata = json.loads((folder/'draft.json').read_text('utf-8'))
        if metadata['project']!=key or time.time()-metadata['created_at']>config.ATTACHMENT_TTL_SECONDS:
            raise ValueError()
        return folder,metadata
    except (OSError,KeyError,ValueError):
        raise ValueError('draft가 만료됐어요 — 다시 만들어 주세요') from None


def preview_file(key,identifier,path):
    folder,metadata = read_draft(key,identifier)
    allowed = {c['path'] for c in metadata['changes']} | set(metadata['unchanged'])
    return assets.safe_file(folder/'files',path) if path in allowed else None


def create_draft(key,options,uploads=None,draft_id=None):
    """동시 재생성을 직렬화하고 실패한 생성물은 기존 draft와 섞지 않는다."""
    with _LOCK:
        return _create_draft(key,options,uploads or {},draft_id)


def _create_draft(key,options,uploads,draft_id):
    if not isinstance(options,dict) or set(uploads)-UPLOADS:
        raise ValueError('조정값이나 업로드 슬롯을 확인해 주세요')
    for name in ('slots','splash','og','meta'):
        if name in options and not isinstance(options[name],dict):
            raise ValueError('조정값은 객체로 보내 주세요')
    if options.get('corners','none') not in ('none','round'):
        raise ValueError('아이콘 모서리 값을 확인해 주세요')
    if options.get('splash',{}).get('mode','compose') not in ('compose','image') or options.get('og',{}).get('mode','auto') not in ('auto','upload'):
        raise ValueError('이미지 생성 방식을 확인해 주세요')
    if sum(len(data) for data in uploads.values())>MAX_BYTES:
        raise ValueError('원본은 합계 25MB 이하로 올려 주세요')
    base = drafts_root()
    base.mkdir(parents=True,exist_ok=True)
    for folder in base.iterdir():
        if folder.is_dir() and not folder.is_symlink() and re.fullmatch('[0-9a-f]{32}',folder.name) and time.time()-folder.stat().st_mtime>config.ATTACHMENT_TTL_SECONDS:
            shutil.rmtree(folder)
    sources = {}
    previous = None
    if draft_id:
        previous,old = read_draft(key,draft_id)
        sources = {s:(previous/'sources'/s).read_bytes() for s in old['sources'] if s in UPLOADS}
    sources.update(uploads)
    if sum(len(data) for data in sources.values())>MAX_BYTES:
        raise ValueError('보관된 원본 합계가 25MB를 넘어요 — 새 draft로 만들어 주세요')
    decoded = {s:load_image(data) for s,data in sources.items()}
    for slot in ('maskable','favicon','og'):
        if options.get('slots',{}).get(slot)=='upload' and slot not in decoded:
            raise ValueError('따로 올리기를 고른 슬롯에 원본을 올려 주세요')
    root = Path(assets.project_root(key) or '')
    info = assets.inspect_repo(root,key)
    if not info['available']:
        raise ValueError(info['reason'])
    background = options.get('background',info['manifest']['values'].get('background_color') or '#14161B')
    if not re.fullmatch('#[0-9a-fA-F]{6}',background):
        raise ValueError('배경색은 #RRGGBB로 입력해 주세요')
    options = dict(options,background=background)
    theme = options.get('theme_color',background)
    if not re.fullmatch('#[0-9a-fA-F]{6}',theme):
        raise ValueError('테마색은 #RRGGBB로 입력해 주세요')
    padding = float(options.get('padding',0))
    if not 0<=padding<=30:
        raise ValueError('아이콘 여백은 0~30%로 골라 주세요')
    slots = {s['id']:s for s in info['slots']}
    chosen = options.get('slots',{})
    if not isinstance(chosen,dict):
        raise ValueError('슬롯 조정값을 확인해 주세요')
    blocked = {s['id']:s['reason'] for s in info['slots'] if not s['editable']}
    notices = []
    icon = square(decoded['icon'][0],options.get('crop')) if 'icon' in decoded else None
    if icon and icon.width<1024:
        notices.append('원본이 1024px보다 작아서 흐릴 수 있어요')
    source_info = None
    if icon:
        colors = [icon.getpixel(p)[:3] for p in [(0,0),(icon.width-1,0),(0,icon.height-1)]]
        source_info = dict(width=icon.width,height=icon.height,transparent=icon.getchannel('A').getextrema()[0]<255,suggested_colors=['#%02x%02x%02x'%c for c in colors])
    anchor = next((f for f in slots['icon']['files'] if 'apple-touch' in f['path']),None)
    anchor = anchor or next(iter(slots['icon']['files']),None)
    folder = str(Path(anchor['path']).parent).replace('\\','/') if anchor else str(Path(info['manifest']['path']).parent).replace('\\','/')
    # 실제 HTML의 URL 규칙을 유지한다.
    html = (root/info['html'][0]).read_text('utf-8')
    prefix = '/static/' if any((t['attrs'].get('href') or '').startswith('/static/') for t in assets.Tags(html).tags if t['tag']=='link') else '/'
    def url(path):
        return prefix + path.removeprefix('static/')
    jobs,urls = [],{}
    def add(slot,path,size,fmt,kind,source):
        if slot in blocked or chosen.get(slot) is False:
            return
        target = root/path
        if any(p.is_symlink() for p in (target,*target.parents)):
            raise ValueError('심볼릭 링크 경로는 브랜드 대상으로 쓸 수 없어요')
        if not assets.safe_file(root,path) and target.exists():
            raise ValueError('안전하지 않은 대상 경로예요')
        jobs.append((slot,path,size,fmt,kind,source))
    for slot in ('icon','maskable','favicon'):
        if slot in blocked:
            continue
        source = (decoded.get(slot,(None,None))[0] if chosen.get(slot)=='upload' else
                  icon if slot=='icon' or chosen.get(slot)=='auto' else decoded.get(slot,(icon,None))[0])
        if source is None or chosen.get(slot) is False:
            continue
        source = square(source,None,1 if slot=='favicon' else 512) if source is not icon else source
        files = slots.get(slot,{}).get('files',[])
        if not files and slot=='icon':
            files = [dict(path=folder+f'/icon-{n}.png',width=n,height=n,format='png') for n in (192,512)]
            urls['icons'] = [dict(src=url(f['path']),sizes=f"{f['width']}x{f['height']}",type='image/png',purpose='any') for f in files]
            files.append(dict(path=folder+'/apple-touch-icon.png',width=180,height=180,format='png'))
            urls['apple'] = url(files[-1]['path'])
        if not files and slot=='maskable':
            files = [dict(path=folder+'/icon-maskable-512.png',width=512,height=512,format='png')]
            urls.setdefault('icons',[]).append(dict(src=url(files[0]['path']),sizes='512x512',type='image/png',purpose='maskable'))
        if not files and slot=='favicon':
            files = [dict(path=folder+f'/favicon-{n}x{n}.png',width=n,height=n,format='png') for n in (16,32)]
            files.append(dict(path=folder+'/favicon.ico',width=48,height=48,format='ico'))
            urls['favicons'] = [url(f['path']) for f in files]
        for f in files:
            size = (f['width'] or 32,f['height'] or 32)
            if f['format']=='svg':
                size=(512,512)
            add(slot,f['path'],size,f['format'], 'apple' if 'apple-touch' in f['path'] else slot,source)
    splash = options.get('splash',{})
    if icon is not None or 'splash' in decoded:
        if chosen.get('splash') is not False and 'splash' not in blocked:
            files = slots['splash']['files']
            if not files:
                files = [dict(path=f'{folder}/splash/splash-{w*r}x{h*r}.png',width=w*r,height=h*r,format='png') for w,h,r in assets.SPLASH_SCREENS]
            for f in files:
                source = decoded.get('splash',(None,None))[0] if splash.get('mode','compose')=='image' else icon
                if source is None:
                    blocked['splash']='시작 화면 원본을 올려 주세요'
                    break
                add('splash',f['path'],(f['width'],f['height']),f['format'],'splash',source)
            # 기존 media를 보존하고 신규 파일만 표를 사용한다.
            if slots['splash']['files']:
                urls['splash']=[(t['attrs']['href'],t['attrs'].get('media')) for t in assets.Tags(html).tags if t['slot']=='splash']
            else:
                urls['splash']=[(url(f['path']),assets.splash_media(w,h,r)) for f,(w,h,r) in zip(files,assets.SPLASH_SCREENS)]
    for f in slots.get('app_splash',{}).get('files',[]):
        slot = 'app_splash_wide' if 'splash-wide' in f['path'] else 'app_splash'
        if slot in decoded:
            add('app_splash',f['path'],(f['width'],f['height']),f['format'],'cover',decoded[slot][0])
    if 'art' in decoded:
        path=options.get('art_path')
        f=next((f for f in slots.get('art',{}).get('files',[]) if f['path']==path),None)
        if not f:
            raise ValueError('감지된 화면 그림을 골라 주세요')
        if f['format']=='svg' and decoded['art'][1] is None:
            raise ValueError('이 화면 그림에는 SVG 원본을 올려 주세요')
        add('art',path,(f['width'] or 1024,f['height'] or 1024),f['format'],'art',decoded['art'][0])
    import db
    with db.connect() as c:
        row=c.execute('SELECT service_domain FROM projects WHERE key=?',(key,)).fetchone()
    domain = row['service_domain'] if row else ''
    if (icon is not None or 'og' in decoded) and chosen.get('og') is not False and 'og' not in blocked:
        if not domain or not re.fullmatch(r'[A-Za-z0-9.-]+(?::[0-9]+)?',domain):
            blocked['og']='서비스 도메인을 연결한 뒤 공유 이미지를 만들 수 있어요'
        else:
            f=next(iter(slots['og']['files']),dict(path=folder+'/og-image.png',width=1200,height=630,format='png'))
            source = decoded.get('og',(None,None))[0] if options.get('og',{}).get('mode')=='upload' or chosen.get('og')=='upload' else icon
            if source is None:
                blocked['og']='공유 이미지 원본을 올려 주세요'
            else:
                add('og',f['path'],(f['width'],f['height']),f['format'],'og',source)
                urls['og']=url(f['path'])
    name=options.get('meta',{}).get('name',info['manifest']['values'].get('name',''))
    text_cache = {}
    def cached_text(value):
        font = Path(__file__).resolve().parents[1]/'static/fonts/IBMPlexMono-600.woff2'
        cache_key = hashlib.sha256(str(value).encode()+font.read_bytes()).hexdigest()+'.png'
        cached = assets.safe_file(previous/'text-cache',cache_key) if previous else None
        layer = Image.open(cached).convert('RGBA') if cached else text_layer(value)
        text_cache[cache_key] = layer
        return layer
    label = cached_text(splash.get('text_value',name)) if splash.get('text') else None
    og_label = cached_text(name) if name and any(j[0]=='og' for j in jobs) else None
    scale=float(splash.get('icon_scale',.25))
    if not math.isfinite(scale) or not .05<=scale<=.8:
        raise ValueError('시작 화면 아이콘 크기는 0.05~0.8로 골라 주세요')
    def render(job):
        slot,path,size,fmt,kind,source=job
        if kind in ('icon','maskable','favicon','apple'):
            image=icon_image(source,size,background,padding,options.get('corners')=='round' and kind=='icon',kind=='apple',kind=='maskable')
        elif kind=='splash' and splash.get('mode','compose')=='compose':
            image=composition(source,size,background,scale,label)
        elif kind=='og' and options.get('og',{}).get('mode','auto')=='auto' and chosen.get('og')!='upload':
            image=composition(source,size,background,.52,og_label,True)
        else:
            image=cover(source,size,splash.get('focal') if kind=='splash' else None)
            if kind in ('splash','og'):
                image=flatten(image,background)
        if fmt=='svg' and decoded.get('art' if kind=='art' else 'favicon',decoded.get('icon',(None,None)))[1]:
            data=decoded.get('art' if kind=='art' else 'favicon',decoded.get('icon'))[1]
        else:
            data=encode(image,fmt,background)
        return path,(slot,data,size)
    with ThreadPoolExecutor(max_workers=4) as pool:
        rendered=dict(pool.map(render,jobs))
    edit_options = dict(options)
    if chosen.get('meta') is False or 'meta' in blocked:
        edit_options = {k:v for k,v in options.items() if k not in ('meta','background','theme_color')}
    edits,edit_blocked=assets.plan_edits(root,info,urls,edit_options,domain)
    blocked.update(edit_blocked)
    for path,data in edits.items():
        rendered[path]=('meta',data,None)
    identifier=draft_id or uuid.uuid4().hex
    target=draft_dir(identifier)
    staging=base/(uuid.uuid4().hex)
    staging.mkdir()
    try:
        (staging/'sources').mkdir()
        if text_cache:
            (staging/'text-cache').mkdir()
            for filename,layer in text_cache.items():
                layer.save(staging/'text-cache'/filename,'PNG',optimize=True)
        for s,data in sources.items():
            (staging/'sources'/s).write_bytes(data)
        changes,unchanged,previews,hashes=[],[],{},{}
        for path,(slot,data,size) in sorted(rendered.items()):
            parts=Path(path).parts
            if Path(path).is_absolute() or any(p in ('.','..') or ':' in p for p in parts):
                raise ValueError('대상 경로가 잘못됐어요')
            existing=assets.safe_file(root,path)
            before=existing.read_bytes() if existing else None
            hashes[path]=subprocess.run(['git','hash-object','--stdin'],input=before,capture_output=True,check=True,cwd=root).stdout.decode().strip() if before is not None else None
            output=staging/'files'/path
            output.parent.mkdir(parents=True,exist_ok=True)
            output.write_bytes(data)
            if size:
                from urllib.parse import quote
                previews.setdefault(slot,[]).append(dict(name=path,url=f'/api/projects/{quote(key,safe="")}/brand/draft/{identifier}/file?path={quote(path,safe="")}',width=size[0],height=size[1]))
            if before==data:
                unchanged.append(path)
                continue
            diff=''
            if size is None:
                old=(before or b'').decode('utf-8')
                new=data.decode('utf-8')
                if path.endswith('.html') and max(map(len,old.splitlines() or ['']))>500:
                    # 한 줄 head의 변경마다 앞뒤 80자만 보여 준다. 저장 바이트는 자르지 않는다.
                    chunks=[]
                    for op,a,b,c,d in difflib.SequenceMatcher(None,old,new,autojunk=False).get_opcodes():
                        if op!='equal':
                            chunks.append('@@ 태그 변경 @@\n-'+old[max(0,a-80):min(len(old),b+80)]+'\n+'+new[max(0,c-80):min(len(new),d+80)]+'\n')
                    diff='--- '+path+'\n+++ '+path+'\n'+''.join(chunks)
                else:
                    diff=''.join(difflib.unified_diff(old.splitlines(True),new.splitlines(True),fromfile=path,tofile=path))
            changes.append(dict(path=path,status='tags' if size is None else 'overwrite' if before is not None else 'new',width=size[0] if size else None,height=size[1] if size else None,bytes=len(data),diff=diff))
        result=dict(draft_id=identifier,source=source_info,previews=previews,changes=changes,unchanged=unchanged,warnings=notices,blocked=blocked)
        metadata=dict(result,project=key,created_at=time.time(),sources=sorted(sources),options=options,blob_hashes=hashes,slots=sorted({v[0] for v in rendered.values()}))
        (staging/'draft.json').write_text(json.dumps(metadata,ensure_ascii=False),encoding='utf-8')
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)
        return result
    finally:
        if staging.exists():
            shutil.rmtree(staging)
