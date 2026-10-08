"""임시 admin으로 지정한 dev 화면을 캡처한다. 운영 설정은 사용하지 않는다."""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from ui_fixture import FAKE_NS, child_environment, free_port, wait_port, BOOTSTRAP

def validate_route(route):
    """URL 대신 알려진 로컬 hash 경로만 받는다."""
    if not re.fullmatch(r'#/(?:board|new|agents|projects|settings(?:/(?:auto|types))?|types|issue/[A-Z][A-Z0-9]*-\d+(?:-\d+)*)?(?:\?[A-Za-z0-9_=&%.-]*)?', route):
        raise ValueError('로컬 hash 경로만 지원한다: #/, #/issue/DEV-1 등')
    return route


def local_request(url, base):
    parsed = urlsplit(url)
    return parsed.scheme in ('http', 'https') and parsed.netloc == urlsplit(base).netloc


def guard_browser(context, base):
    context.route('**/*', lambda r: r.continue_() if local_request(r.request.url, base) else r.abort())


@contextmanager
def isolated_environment(env):
    """Playwright driver도 환경 허용 목록만 물려받도록 한다."""
    original = dict(os.environ)
    original_cwd = Path.cwd()
    os.environ.clear()
    os.environ.update(env)
    try:
        os.chdir(Path(env['DEV_DATA_DIR']).parent)
        yield
    finally:
        os.chdir(original_cwd)
        os.environ.clear()
        os.environ.update(original)


def seed_fixture(request, base, fixture):
    """임시 API에만 재현 데이터를 만든다. 자동 위임은 기본 비활성이다."""
    headers = {'X-Requested-With': 'dev'}
    def post(path, body):
        response = request.post(base + path, headers=headers, data=body)
        if not response.ok:
            raise RuntimeError(f'fixture API 실패: {path} ({response.status})')
        return response.json()
    post('/api/projects', {'key': 'DEV', 'name': 'UI 캡처', 'description': '임시 화면 확인용 프로젝트'})
    if fixture == 'detail':
        issue = post('/api/issues', {'project': 'DEV', 'title': '화면 확인용 Issue',
                                   'body': '임시 데이터로 Description, Plan, Tasks를 확인한다.'})
        post('/api/issues/' + issue['ref'] + '/plans', {'body': '화면 확인용 Plan'})
        post('/api/issues', {'project': 'DEV', 'parent': issue['ref'], 'title': '화면 확인용 Task'})


def wait_screen(page, route):
    name = route[2:].split('?')[0]
    selector = {'': '#project-filter', 'board': '.kanban', 'new': '#new-form',
                'agents': '#agent-form', 'projects': '#project-create',
                'settings': '.auto-settings', 'settings/auto': '.auto-settings',
                'types': '.type-management', 'settings/types': '.type-management'}.get(name, '#view')
    page.wait_for_selector(selector)
    if name.startswith('issue/'):
        page.wait_for_function("ref => document.querySelector('.detail .ref')?.textContent.trim() === ref", arg=name[6:])
        page.wait_for_selector('h1#title')
    page.wait_for_function("document.querySelector('#view')?.textContent.trim().length > 0")
    page.wait_for_function("!document.querySelector('#view [aria-busy=true]')")
    page.evaluate('() => document.fonts.ready')
    page.wait_for_timeout(300)
    if page.locator('#view').inner_text().strip() == '없는 화면이에요.':
        raise RuntimeError('route 화면을 찾지 못했다')


def capture(route='#/', fixture='empty'):
    validate_route(route)
    output = ROOT / '.ui-captures'
    output.mkdir(exist_ok=True)
    if output.is_symlink() or output.resolve().parent != ROOT.resolve():
        raise ValueError('산출물 폴더는 worktree 안에 있어야 한다')
    directory = Path(tempfile.mkdtemp(prefix='capture-', dir=output))
    manifest = {'version': 1, 'route': route, 'fixture': fixture, 'captures': [], 'errors': []}
    processes, logs = [], []
    stage = 'startup'
    try:
        ns_port, dev_port = free_port(), free_port()
        while ns_port == dev_port:
            dev_port = free_port()
        base = f'http://127.0.0.1:{dev_port}'
        env = child_environment(directory, ns_port, dev_port)
        (directory / 'fake_ns.py').write_text(FAKE_NS, encoding='utf-8')
        (directory / 'capture_app.py').write_text(BOOTSTRAP, encoding='utf-8')
        commands = [[sys.executable, '-m', 'uvicorn', 'fake_ns:app', '--host', '127.0.0.1', '--port', str(ns_port)],
                    [sys.executable, str(directory / 'capture_app.py'), str(ROOT / 'server'), str(ns_port), str(dev_port)]]
        for index, command in enumerate(commands):
            log = (directory / f'server-{index}.log').open('w', encoding='utf-8')
            logs.append(log)
            processes.append(subprocess.Popen(command, cwd=directory, env=env, stdout=log, stderr=log))
        wait_port(ns_port)
        wait_port(dev_port)
        from playwright.sync_api import sync_playwright
        with isolated_environment(env), sync_playwright() as playwright:
            browser = playwright.chromium.launch(env=env, args=['--enable-logging=stderr'])
            try:
                context = browser.new_context(service_workers='block')
                context.set_default_timeout(10000)
                guard_browser(context, base)
                page = context.new_page()
                page.set_default_timeout(10000)
                stage = 'login'
                response = context.request.post(base + '/api/auth/login', headers={'X-Requested-With': 'dev'},
                                                data={'username': 'admin', 'password': 'pw'})
                if not response.ok:
                    raise RuntimeError(f'테스트 로그인 실패 ({response.status})')
                stage = 'fixture'
                seed_fixture(context.request, base, fixture)
                for theme in ('light', 'dark'):
                    for width, height in ((1300, 850), (390, 800)):
                        stage = f'route/{theme}/{width}'
                        try:
                            page.set_viewport_size({'width': width, 'height': height})
                            page.emulate_media(color_scheme=theme)
                            page.goto(base)
                            page.wait_for_selector('#shell:not([hidden])')
                            page.evaluate("s => {localStorage.setItem('dev.theme',s);document.documentElement.dataset.theme=s}", theme)
                            page.goto(base + '/' + route)
                            wait_screen(page, route)
                            filename = f'{theme}-{width}.png'
                            page.screenshot(path=str(directory / filename), full_page=True)
                            manifest['captures'].append({'path': (directory / filename).relative_to(ROOT).as_posix(),
                                                         'theme': theme, 'width': width, 'height': height})
                        except Exception as exc:
                            manifest['errors'].append({'stage': stage, 'reason': type(exc).__name__ + ': ' + str(exc)[:1200]})
            finally:
                browser.close()
    except Exception as exc:
        manifest['errors'].append({'stage': stage, 'reason': type(exc).__name__ + ': ' + str(exc)[:1200]})
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for log in logs:
            log.close()
        if manifest['errors'] and manifest['errors'][0]['stage'] == 'startup':
            manifest['errors'][0]['log_summary'] = '\n'.join(
                p.read_text(encoding='utf-8', errors='replace')[-1200:]
                for p in sorted(directory.glob('server-*.log')))
        manifest['status'] = 'ok' if len(manifest['captures']) == 4 and not manifest['errors'] else 'partial' if manifest['captures'] else 'failed'
        (directory / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return directory, manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--route', default='#/')
    parser.add_argument('--fixture', choices=('empty', 'detail'), default='empty', help='detail fixture의 Issue는 DEV-1이다')
    args = parser.parse_args()
    try:
        directory, manifest = capture(args.route, args.fixture)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({'manifest': (directory / 'manifest.json').relative_to(ROOT).as_posix(),
                      'status': manifest['status'], 'errors': manifest['errors']}, ensure_ascii=False))
    return 0 if manifest['status'] == 'ok' else 1


if __name__ == '__main__':
    sys.exit(main())
