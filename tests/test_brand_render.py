"""브랜드 draft의 형식·결정성·최소 편집·입력 및 API 경계를 검사한다."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TMP=tempfile.TemporaryDirectory(prefix='brand-render-')
ROOT=Path(TMP.name)
os.environ['DEV_DATA_DIR']=str(ROOT/'data')
os.environ['DEV_DB_PATH']=str(ROOT/'data/dev.db')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
import brand_assets as assets
import brand_render as render
import db
import app
from PIL import Image
from fastapi.testclient import TestClient


def image(fmt='PNG',size=(512,512)):
    out=io.BytesIO()
    im=Image.new('RGBA',size,(255,40,60,100))
    if fmt=='JPEG':
        im=im.convert('RGB')
    im.save(out,fmt)
    return out.getvalue()


class BrandRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT)
        self.repo=Path(self.temp.name)
        (self.repo/'static').mkdir()
        subprocess.run(['git','init',str(self.repo)],check=True,capture_output=True)
        for path,size,fmt in [('apple-touch-icon.png',(180,180),'PNG'),('icon.png',(192,192),'PNG'),('favicon.ico',(48,48),'ICO'),('favicon.png',(32,32),'PNG'),('splash.jpg',(80,160),'JPEG'),('splash-wide.webp',(160,80),'WEBP')]:
            (self.repo/'static'/path).write_bytes(image(fmt,size))
        (self.repo/'static/favicon.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        self.manifest='{\n  "name": "Old",\n  "custom": {"x": 1},\n  "icons": [{"src":"/icon.png","sizes":"192x192"}]\n}\n'
        (self.repo/'static/manifest.json').write_text(self.manifest)
        self.html='<html><head><link rel="manifest" href="/manifest.json"><link rel="icon" href="/favicon.svg"><link rel="icon" href="/favicon.ico"><link rel="icon" href="/favicon.png"><link rel="apple-touch-icon" href="/apple-touch-icon.png"><link rel="stylesheet" href="{{app.css_version}}"></head><body>keep me</body></html>'
        (self.repo/'static/index.html').write_text(self.html)
        subprocess.run(['git','-C',str(self.repo),'add','.'],check=True,capture_output=True)
        self.root_patch=patch.object(assets,'project_root',return_value=str(self.repo))
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.addCleanup(self.temp.cleanup)
        with db.connect() as c:
            c.execute("INSERT OR IGNORE INTO projects(key,name,service_domain,created_at) VALUES('TT','Test','test.example','2026-01-01')")
        self.options={'background':'#123456','slots':{'og':False},'meta':{'name':'New'}}

    def make(self,options=None,uploads=None,identifier=None):
        return render.create_draft('TT',options or self.options,uploads if uploads is not None else {'icon':image()},identifier)

    def file(self,draft,path):
        return render.preview_file('TT',draft['draft_id'],path)

    def test_generation_determinism_dimensions_and_no_changes(self):
        first=self.make()
        first_bytes={c['path']:self.file(first,c['path']).read_bytes() for c in first['changes']}
        second=self.make(uploads={},identifier=first['draft_id'])
        self.assertEqual(first_bytes,{c['path']:self.file(second,c['path']).read_bytes() for c in second['changes']})
        folder,_=render.read_draft('TT',first['draft_id'])
        self.assertEqual(len(first['previews']['splash']),10)
        self.assertEqual(first['changes'],second['changes'])
        for c in first['changes']:
            path=self.file(first,c['path'])
            data=path.read_bytes()
            if c['width'] and not c['path'].endswith('.svg'):
                with Image.open(path) as im:
                    self.assertEqual(im.size,(c['width'],c['height']))
                    if 'apple-touch' in c['path'] or '/splash/' in c['path'] or 'maskable' in c['path']:
                        self.assertEqual(im.convert('RGBA').getchannel('A').getextrema(),(255,255))
            destination=self.repo/c['path']
            destination.parent.mkdir(parents=True,exist_ok=True)
            destination.write_bytes(data)
        subprocess.run(['git','-C',str(self.repo),'add','.'],check=True,capture_output=True)
        third=self.make(uploads={},identifier=first['draft_id'])
        self.assertEqual(third['changes'],[])
        self.assertTrue(third['unchanged'])
        self.assertIn('blob_hashes',json.loads((folder/'draft.json').read_text('utf-8')))

    def test_html_manifest_and_og(self):
        options=dict(self.options,slots={'splash':False},meta={'name':'A & B','description':'hello'})
        result=self.make(options)
        text=self.file(result,'static/index.html').read_text('utf-8')
        self.assertIn('<body>keep me</body>',text)
        self.assertIn('href="{{app.css_version}}"',text)
        self.assertIn('property="og:image" content="https://test.example/og-image.png"',text)
        self.assertIn('summary_large_image',text)
        self.assertIn('A &amp; B',text)
        manifest=self.file(result,'static/manifest.json').read_text('utf-8')
        self.assertIn('  "custom": {"x": 1}',manifest)
        self.assertEqual(json.loads(manifest)['name'],'A & B')
        self.assertEqual(assets.patch_json(self.manifest,{'name':'New'}),self.manifest.replace('"Old"','"New"'))
        for source in ['{"a":1}', '{\n  "a": 1\n}', '{}']:
            patched=assets.patch_json(source,{'b':2})
            self.assertEqual(json.loads(patched)['b'],2)

    def test_first_splash_tags(self):
        result=self.make()
        text=self.file(result,'static/index.html').read_text('utf-8')
        self.assertEqual(text.count('rel="apple-touch-startup-image"'),10)
        self.assertIn('name="apple-mobile-web-app-capable" content="yes"',text)
        self.assertIn(assets.splash_media(440,956,3),text)

    def test_preserve_formats_and_size(self):
        html=self.html.replace('</head>','<link rel="apple-touch-startup-image" href="/splash.jpg" media="old"></head>')
        (self.repo/'static/index.html').write_text(html)
        result=self.make(uploads={'icon':image(),'app_splash_wide':image('WEBP')})
        for path,fmt,size in [('static/splash.jpg','JPEG',(80,160)),('static/splash-wide.webp','WEBP',(160,80))]:
            with Image.open(self.file(result,path)) as im:
                self.assertEqual(im.format,fmt)
                self.assertEqual(im.size,size)
        svg=self.file(result,'static/favicon.svg').read_text()
        self.assertIn('data:image/png;base64,',svg)
        with Image.open(self.file(result,'static/favicon.ico')) as im:
            self.assertEqual(im.ico.sizes(),{(16,16),(32,32),(48,48)})

    def test_reject_svg_and_spoof(self):
        for svg in [b'<svg><script/></svg>',b'<svg><foreignObject/></svg>',b'<svg><image href="https://bad/x"/></svg>',b'<svg onload="x()"/>',b'<!ENTITY x "bad"><svg/>',b'<svg><style>@import "https://bad";</style></svg>']:
            with self.subTest(svg=svg),self.assertRaises(ValueError):
                render.load_image(svg)
        for data in [b'GIF89a...',b'\x89PNG\r\n\x1a\n'+image('JPEG'),b'not image']:
            with self.assertRaises(ValueError):
                render.load_image(data)
        self.assertIn(b'<svg',render.svg_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>'))

    def test_svg_raster_and_exif(self):
        svg=b'<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024"><rect width="1024" height="1024" fill="red"/></svg>'
        first,_=render.load_image(svg)
        second,_=render.load_image(svg)
        self.assertEqual(first.size,(1024,1024))
        self.assertEqual(first.getpixel((100,100)),(255,0,0,255))
        self.assertEqual(render.encode(first,'png','#000000'),render.encode(second,'png','#000000'))
        original=Image.new('RGB',(600,800),'blue')
        exif=Image.Exif()
        exif[274]=6
        exif[270]='private metadata'
        out=io.BytesIO()
        original.save(out,'JPEG',exif=exif)
        transposed,_=render.load_image(out.getvalue())
        self.assertEqual(transposed.size,(800,600))
        saved=Image.open(io.BytesIO(render.encode(transposed,'jpeg','#000000')))
        self.assertFalse(saved.getexif())

    def test_metadata_only_and_ttl(self):
        result=self.make({'meta':{'name':'Meta only'}},uploads={})
        self.assertFalse(result['previews'])
        self.assertTrue(all(c['status']=='tags' for c in result['changes']))
        with patch.object(render.time,'time',return_value=render.time.time()+render.config.ATTACHMENT_TTL_SECONDS+1):
            with self.assertRaises(ValueError):
                render.read_draft('TT',result['draft_id'])
            self.make({'meta':{'name':'Next'}},uploads={})
        self.assertFalse(render.draft_dir(result['draft_id']).exists())

    def test_crop_errors_and_no_domain(self):
        with self.assertRaises(ValueError):
            self.make(uploads={'icon':image(size=(600,512))})
        with self.assertRaises(ValueError):
            self.make(uploads={'icon':image(size=(256,256))})
        result=self.make(dict(self.options,crop={'x':0,'y':0,'size':512}),{'icon':image(size=(600,512))})
        self.assertEqual(result['source']['width'],512)
        with db.connect() as c:
            c.execute("UPDATE projects SET service_domain='' WHERE key='TT'")
        result=self.make(dict(self.options,slots={'splash':False}))
        self.assertIn('og',result['blocked'])
        with db.connect() as c:
            c.execute("UPDATE projects SET service_domain='test.example' WHERE key='TT'")

    def test_api_auth_and_preview_boundary(self):
        client=TestClient(app.app)
        with patch.object(app.auth,'agent_for_key',return_value={'id':1,'name':'agent','model':'test'}):
            self.assertEqual(client.post('/api/projects/TT/brand/draft',headers=[('Authorization','Bearer test')]).status_code,403)
            self.assertEqual(client.get('/api/projects/TT/brand/draft/'+'a'*32+'/file?path=x',headers=[('Authorization','Bearer test')]).status_code,403)
        result=self.make()
        self.assertIsNone(render.preview_file('TT',result['draft_id'],'../draft.json'))
        with self.assertRaises(ValueError):
            render.preview_file('OTHER',result['draft_id'],'static/icon.png')
        with patch.object(app,'human_only',return_value={'kind':'human'}),patch.object(app,'actor',return_value={'kind':'human'}):
            # 인증 미들웨어와 별개로 라우트 계약을 직접 검증한다.
            import asyncio
            from starlette.requests import Request
            from starlette.datastructures import Headers
            import httpx
            req=httpx.Request('POST','http://test',data={'options':json.dumps({'slots':{'splash':False,'og':False}})},files={'icon':('image.jpg',image(),'image/jpeg')})
            payload=req.read()
            sent=False
            async def receive():
                nonlocal sent
                if sent:
                    return {'type':'http.disconnect'}
                sent=True
                return {'type':'http.request','body':payload,'more_body':False}
            request=Request({'type':'http','method':'POST','path':'/','headers':list(Headers(dict(req.headers)).raw)},receive)
            response=asyncio.run(app.api_project_brand_draft('TT',request))
            self.assertIn('draft_id',response)


if __name__=='__main__':
    unittest.main()

