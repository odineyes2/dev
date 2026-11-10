"""브랜드 경로 감지·사람 전용 API·DEV 캐시 해시를 임시 저장소로 검사한다."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory(prefix='dev-brand-')
ROOT = Path(TMP.name)
os.environ['DEV_DATA_DIR'] = str(ROOT / 'data')
os.environ['DEV_DB_PATH'] = str(ROOT / 'data/dev.db')
sys.path.insert(0,str(Path(__file__).resolve().parents[1] / 'server'))
import brand_assets as brand
import db
import issues
import auth
import app
from fastapi.testclient import TestClient
from PIL import Image


def git(repo,*args):
    return subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True)


def fixture(key):
    repo = ROOT / key
    repo.mkdir()
    git(repo,'init')
    prefix = '/static/' if key == 'CH' else '/'
    icons = 'icons/' if key == 'MO' else ''
    manifest = 'manifest.webmanifest' if key == 'MO' else 'manifest.json'
    name = 'moai' if key == 'MO' else 'icon'
    paths = [f'{icons}{name}-192.png', f'{icons}{name}-512.png', f'{icons}{name}-maskable-512.png', f'{icons}apple-touch-icon.png','favicon.svg','favicon-32.png']
    for path in paths:
        target = repo / 'static' / path
        target.parent.mkdir(parents=True,exist_ok=True)
        if path.endswith('.svg'):
            target.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        else:
            Image.new('RGB',(192,192),'red').save(target)
    data = dict(name=key,theme_color='#14161B',icons=[dict(src=f'{icons}{name}-192.png',sizes='192x192'),dict(src=f'{icons}{name}-512.png',purpose='any'),dict(src=f'{icons}{name}-maskable-512.png',purpose='maskable')])
    (repo/'static'/manifest).write_text(json.dumps(data))
    tags = f'<link rel="manifest" href="{prefix}{manifest}"><link rel="icon" href="{prefix}favicon.svg"><link rel="icon" href="{prefix}favicon-32.png"><link rel="apple-touch-icon" href="{prefix}{icons}apple-touch-icon.png">'
    if key in ('CH','MO'):
        for w,h,r in brand.SPLASH_SCREENS:
            path = f'{icons if key == "MO" else "splash/"}splash-{w*r}x{h*r}.{"png" if key == "MO" else "jpg"}'
            file = repo/'static'/path
            file.parent.mkdir(parents=True,exist_ok=True)
            Image.new('RGB',(20,40),'blue').save(file)
            tags += f'<link rel="apple-touch-startup-image" href="{prefix}{path}" media="{brand.splash_media(w,h,r)}">'
    if key == 'CH':
        (repo/'static/images').mkdir()
        for path in ('splash.webp','splash-wide.webp'):
            Image.new('RGB',(10,20)).save(repo/'static/images'/path)
    if key == 'DEV':
        (repo/'static/login-workspace.svg').write_text('<svg/>')
        tags += '<img src="/login-workspace.svg">'
    html = '<html><head>'+tags+'<link rel="stylesheet" href="{{app.css_version}}"></head></html>'
    (repo/'static/index.html').write_text(html)
    (repo/'templates').mkdir()
    (repo/'templates/manifest.json').write_text('{"templates":["trap"]}')
    (repo/'templates/index.html').write_text('<html>template</html>')
    git(repo,'add','.')
    return repo


class BrandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        cls.repos = {key:fixture(key) for key in ('DEV','NS','CH','MO')}
        for key,repo in cls.repos.items():
            issues.create_project({'kind':'human','name':'admin'},key,key,local_path=str(repo))
        issues.create_project({'kind':'human','name':'admin'},'TO','TO',local_path=str(ROOT/'missing'))

    def test_structures(self):
        for key,repo in self.repos.items():
            with self.subTest(key=key):
                info = brand.status(key)
                self.assertTrue(info['available'],info['reason'])
                self.assertEqual(info['html'],['static/index.html'])
                self.assertTrue(info['manifest']['path'].startswith('static/'))
                slots = {s['id']:s for s in info['slots']}
                self.assertEqual(len(slots['icon']['files']),3)
                self.assertEqual(len(slots['maskable']['files']),1)
                self.assertEqual(len(slots['favicon']['files']),2)
                self.assertEqual(len(slots['splash']['files']),10 if key in ('CH','MO') else 0)
                self.assertTrue(all(s['editable'] for s in slots.values()))
                if key == 'CH':
                    self.assertEqual(len(slots['app_splash']['files']),2)
                if key == 'MO':
                    self.assertTrue(all(f['path'].startswith('static/icons/') for f in slots['icon']['files']))
                if key == 'DEV':
                    self.assertEqual(slots['art']['files'][0]['path'],'static/login-workspace.svg')
                for s in slots.values():
                    for f in s['files']:
                        self.assertNotIn('templates/',f['path'])
                        self.assertGreater(f['bytes'],0)
        self.assertFalse(brand.status('TO')['available'])
        self.assertFalse(brand.inspect_repo(str(ROOT))['available'])

    def test_tags_and_ambiguity(self):
        repo = self.repos['NS']
        file = repo/'static/index.html'
        text = file.read_text()
        for t in brand.Tags(text).tags:
            self.assertEqual(text[t['start']:t['end']],t['raw'])
        try:
            file.write_text(text.replace('/favicon.svg','{{favicon}}'))
            slots = {s['id']:s for s in brand.status('NS')['slots']}
            self.assertFalse(slots['favicon']['editable'])
            self.assertTrue(slots['icon']['editable'])
        finally:
            file.write_text(text)
        second = repo/'static/second.html'
        try:
            second.write_text(text)
            git(repo,'add','static/second.html')
            self.assertEqual(len(brand.status('NS')['html']),2)
            second.write_text(text.replace('/favicon.svg','/other.svg'))
            self.assertFalse(brand.status('NS')['available'])
        finally:
            second.unlink()

    def test_security_and_api(self):
        async def admin(cookie):
            return {'id':1,'username':'admin','role':'admin'}
        with patch.object(auth,'nightshift_user',admin), patch.object(auth,'agent_for_key',return_value={'id':1,'name':'agent','model':'test'}):
            client = TestClient(app.app)
            self.assertEqual(client.get('/api/projects/DEV/brand').status_code,200)
            path = '/api/projects/DEV/brand/file'
            self.assertEqual(client.get(path,params={'path':'static/favicon.svg'}).status_code,200)
            for value in ('../static/favicon.svg','static/../static/favicon.svg','static\\favicon.svg','templates/manifest.json','static/index.html','/static/favicon.svg','.env','%2e%2e/secret'):
                self.assertEqual(client.get(path,params={'path':value}).status_code,404,value)
            agent_key = 'test-only'
            for url in ('/api/projects/DEV/brand',path+'?path=static/favicon.svg'):
                self.assertEqual(client.get(url,headers={'Authorization':f'Bearer {agent_key}'}).status_code,403)
            self.assertEqual(client.get('/api/projects/NONE/brand').status_code,404)
        root = self.repos['DEV']
        self.assertIsNone(brand.resolve_url(root,{'static/favicon.svg'},'https://example.com/favicon.svg','static/index.html'))

    def test_index_hash(self):
        response = app.index_page().body.decode()
        for t in brand.Tags(response).tags:
            if t['tag'] == 'link' and set(t['attrs'].get('rel','').split()) & {'icon','apple-touch-icon','manifest'}:
                href = t['attrs']['href']
                name = href.split('?')[0].lstrip('/')
                digest = hashlib.sha1((app.STATIC_DIR/name).read_bytes()).hexdigest()[:10]
                self.assertIn('v='+digest,href)
        source = (app.STATIC_DIR/'index.html').read_text('utf-8')
        self.assertNotIn('?v=',source.split('<meta name="theme-color"')[0])
        changed = brand.version_links('<link rel="icon" href="/favicon.svg?x=1&amp;v=old#part">',app.STATIC_DIR)
        self.assertIn('x=1&amp;v=',changed)
        self.assertNotIn('v=old',changed)
        self.assertIn('#part',changed)


if __name__ == '__main__':
    unittest.main()
