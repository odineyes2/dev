"""프로젝트 템플릿 목록·옵션 검증·자리표시 렌더를 담당한다."""
import copy
import json
from pathlib import Path, PurePosixPath
import re

from issues import StoreError

ROOT = Path(__file__).resolve().parent.parent / 'templates'
VALUES = {'PROJECT_NAME', 'PROJECT_KEY', 'APP_NAME', 'PORT'}
MARKER = re.compile(r'^\s*(?:(?:#|//|/\*|<!--)\s*)?\[(/?)option:([a-z_]+)\](?:\s*(?:\*/|-->))?\s*$')


def get(template_id):
    """매니페스트를 읽고 알 수 없는 종류는 거절한다."""
    if template_id == 'blank':
        return {'id': 'blank', 'name': '빈 시작', 'description': '기획서로 최소 앱을 만들어요.', 'options': {}, 'provides': [], 'overwrite': []}
    if not isinstance(template_id, str) or not re.fullmatch(r'[a-z][a-z0-9-]*', template_id):
        raise StoreError('알 수 없는 템플릿이에요.')
    path = ROOT / template_id / 'template.json'
    if not path.is_file():
        raise StoreError('알 수 없는 템플릿이에요.')
    return json.loads(path.read_text(encoding='utf-8'))


def catalog():
    """화면에 필요한 목록과 옵션 기본값을 돌려준다."""
    manifests = [get('blank')] + [get(p.parent.name) for p in sorted(ROOT.glob('*/template.json'))]
    return [{key: copy.deepcopy(m[key]) for key in ('id', 'name', 'description', 'options')} for m in manifests]


def _blocks(text, options):
    output, stack = [], []
    for line in text.splitlines(keepends=True):
        marker = MARKER.fullmatch(line.rstrip('\r\n'))
        if marker:
            closing, key = marker.groups()
            if key not in options:
                raise StoreError('템플릿 옵션 표지가 잘못됐어요.')
            if closing:
                if not stack or stack.pop() != key:
                    raise StoreError('템플릿 옵션 블록이 맞지 않아요.')
            else:
                stack.append(key)
        elif all(options[key] for key in stack):
            output.append(line)
    if stack:
        raise StoreError('템플릿 옵션 블록이 닫히지 않았어요.')
    return ''.join(output)


def render(template_id, options, values):
    """옵션 파일을 빼고 자리표시를 치환한 파일 사전을 만든다."""
    manifest = get(template_id)
    if not isinstance(options, dict) or options.keys() - manifest['options'].keys() or any(type(v) is not bool for v in options.values()):
        raise StoreError('템플릿 옵션을 확인해 주세요.')
    enabled = {key: value['default'] for key, value in manifest['options'].items()}
    enabled.update(options)
    if template_id == 'blank':
        return {}
    if not isinstance(values, dict) or set(values) != VALUES:
        raise StoreError('템플릿 자리표시 값을 확인해 주세요.')
    values = {key: str(value) for key, value in values.items()}
    if not values['PORT'].isascii() or not values['PORT'].isdigit() or not 1 <= int(values['PORT']) <= 65535:
        raise StoreError('포트를 확인해 주세요.')
    if any(not re.fullmatch(r'[\w .-]{1,100}', values[key]) for key in VALUES - {'PORT'}):
        raise StoreError('프로젝트 이름에는 글자·숫자·공백·점·밑줄·하이픈만 써 주세요.')
    dropped = {path for key, option in manifest['options'].items() if not enabled[key] for path in option.get('drop', [])}
    result = {}
    base = ROOT / template_id
    for path in sorted(base.rglob('*')):
        if path.is_symlink():
            raise StoreError('템플릿에는 링크를 둘 수 없어요.')
        if not path.is_file():
            continue
        relative = path.relative_to(base).as_posix()
        if relative == 'template.json' or relative in dropped:
            continue
        if any(part.startswith('.') for part in PurePosixPath(relative).parts) and relative not in ('.env.example', '.gitignore'):
            raise StoreError('템플릿에 숨김 경로가 있어요.')
        text = _blocks(path.read_text(encoding='utf-8'), enabled)
        def substitute(match):
            if match[1] not in values:
                raise StoreError('알 수 없는 템플릿 자리표시예요.')
            return values[match[1]]
        result[relative] = re.sub(r'\{\{([A-Z_]+)\}\}', substitute, text)
    return result
