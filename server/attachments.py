"""첨부 저장·연결·만료 정리. 외부 URL을 가져오거나 자동화를 시작하지 않는다."""
import json
import re
import struct
import uuid
import zlib
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import config
import db
import issues

# MCP 콘텐츠는 업로드 상한과 별도로 제한한다.
TEXT_PREVIEW_BYTES = 64 * 1024
IMAGE_CONTENT_BYTES = 4 * 1024 * 1024


def capture_baseline(cwd):
    """실행 전에 존재한 캡처 디렉터리는 이후 수정되어도 제외한다."""
    root = Path(cwd) / '.ui-captures'
    if root.is_symlink() or root.is_junction():
        raise issues.StoreError('캡처 링크 경로는 사용할 수 없어요.')
    return {p.name for p in root.iterdir()} if root.is_dir() else set()


def _capture_path(cwd, relative):
    """경로 구성 요소의 symlink와 Windows 경로도 거절한다."""
    if not isinstance(relative, str) or '\\' in relative or ':' in relative:
        raise issues.StoreError('캡처 상대 경로가 잘못됐어요.')
    parts = relative.split('/')
    if len(parts) < 3 or parts[0] != '.ui-captures' or any(p in ('', '.', '..') for p in parts):
        raise issues.StoreError('캡처 경로가 산출물 폴더를 벗어나요.')
    root = Path(cwd).resolve()
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink() or path.is_junction():
            raise issues.StoreError('캡처 링크 경로는 사용할 수 없어요.')
    if not path.resolve().is_relative_to(root):
        raise issues.StoreError('캡처 경로가 worktree를 벗어나요.')
    return path


def _capture_png(data):
    """PNG 시그니처뿐 아니라 청크 길이·CRC·필수 청크를 확인한다."""
    _validate('.png', data)
    offset, kinds = 8, []
    while offset + 12 <= len(data):
        size = struct.unpack('>I', data[offset:offset + 4])[0]
        end = offset + 12 + size
        if end > len(data):
            break
        chunk = data[offset + 4:end - 4]
        kind = chunk[:4]
        if zlib.crc32(chunk) != struct.unpack('>I', data[end - 4:end])[0]:
            break
        if not kinds and (kind != b'IHDR' or size != 13 or not all(struct.unpack('>II', chunk[4:12]))):
            break
        kinds.append(kind)
        offset = end
        if kind == b'IEND':
            if size == 0 and offset == len(data) and b'IDAT' in kinds:
                return
            break
    raise issues.StoreError('캡처 PNG 형식이 잘못됐어요.')


def collect_captures(ref, run_id, cwd, baseline):
    """서버 내부 완료 경로만 호출한다. 현재 실행의 PNG를 원자적으로 연결한다."""
    warnings, added = [], 0
    root = Path(cwd) / '.ui-captures'
    if not root.exists():
        return added, warnings
    # 산출물 폴더 자체가 링크이면 열거하지 않는다.
    _capture_path(cwd, '.ui-captures/check/manifest.json')
    directories = sorted(p.name for p in root.iterdir() if p.name not in baseline)
    for directory in directories:
        try:
            relative = f'.ui-captures/{directory}/manifest.json'
            manifest_path = _capture_path(cwd, relative)
            if not manifest_path.is_file():
                continue
            with manifest_path.open('rb') as f:
                raw = f.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                raise issues.StoreError('캡처 manifest 용량 제한을 넘었어요.')
            manifest = json.loads(raw)
            if not isinstance(manifest, dict) or manifest.get('version') != 1 or not isinstance(manifest.get('captures'), list):
                raise issues.StoreError('캡처 manifest 형식이 잘못됐어요.')
            for error in manifest.get('errors', [])[:10]:
                warnings.append(f'{relative}: {str(error)[:500]}')
            for entry in manifest['captures']:
                try:
                    source = entry['path']
                    path = _capture_path(cwd, source)
                    if not source.startswith(f'.ui-captures/{directory}/') or path.suffix.lower() != '.png':
                        raise issues.StoreError('현재 캡처 디렉터리의 PNG만 첨부할 수 있어요.')
                    with path.open('rb') as f:
                        data = f.read(config.ATTACHMENT_MAX_BYTES + 1)
                    if len(data) > config.ATTACHMENT_MAX_BYTES:
                        raise issues.StoreError('캡처 파일 용량 제한을 넘었어요.', 413)
                    _capture_png(data)
                    key, stored = uuid.uuid4().hex, None
                    try:
                        with db.connect() as c:
                            c.execute('BEGIN IMMEDIATE')
                            row = issues._find(c, ref)
                            run = c.execute("SELECT * FROM runs WHERE id=? AND issue_id=? AND mode='execute'", (run_id, row['id'])).fetchone()
                            latest = c.execute("SELECT MAX(id) FROM runs WHERE issue_id=? AND mode='execute'", (row['id'],)).fetchone()[0]
                            if not run or latest != run_id:
                                raise issues.StoreError('현재 Task 실행이 아니에요.')
                            aid = uuid.uuid5(uuid.NAMESPACE_URL, f'dev:capture:{row["id"]}:{run_id}:{source}').hex
                            if c.execute('SELECT 1 FROM attachments WHERE issue_id=? AND id=?', (row['id'], aid)).fetchone():
                                continue
                            count, total = c.execute('SELECT COUNT(*),COALESCE(SUM(size),0) FROM attachments WHERE issue_id=?', (row['id'],)).fetchone()
                            if count >= config.ATTACHMENT_MAX_COUNT or total + len(data) > config.ATTACHMENT_TOTAL_BYTES:
                                raise issues.StoreError('이슈의 첨부 개수 또는 합계 용량 제한을 넘었어요.', 413)
                            stored = file_path(key)
                            stored.parent.mkdir(parents=True, exist_ok=True)
                            stored.write_bytes(data)
                            a = _insert(c, run['actor'], 'image', _name(path.name), 'image/png', len(data), key)
                            c.execute('UPDATE attachments SET id=?,issue_id=?,expires_at=NULL WHERE id=?', (aid, row['id'], a['id']))
                        added += 1
                    except BaseException:
                        if stored:
                            stored.unlink(missing_ok=True)
                        raise
                except (OSError, ValueError, KeyError, TypeError, issues.StoreError) as e:
                    warnings.append(f'{relative}: {str(e)[:500]}')
        except (OSError, ValueError, TypeError, issues.StoreError) as e:
            warnings.append(f'.ui-captures/{directory}: {str(e)[:500]}')
    return added, warnings


def metadata(a):
    """저장 키·소유자를 제외한 공개 첨부 메타데이터를 만든다."""
    return {**{k: a[k] for k in ('id', 'kind', 'name', 'media_type', 'size', 'url', 'created_at')},
            'download_url': f"/api/attachments/{a['id']}/content" if a['storage_key'] else None}


def get(actor, attachment_id, ref=None):
    """연결된 첨부는 인증 사용자, 임시 첨부는 소유자만 읽는다."""
    if not actor:
        raise issues.StoreError('인증이 필요해요.', 401)
    with db.connect() as c:
        a = c.execute('SELECT * FROM attachments WHERE id=?', (attachment_id,)).fetchone()
        if a is None:
            raise issues._not_found('첨부')
        a = dict(a)
        if ref is not None and a['issue_id'] != issues._find(c, ref)['id']:
            raise issues._not_found('첨부')
        if a['issue_id'] is None:
            if a['owner'] != issues.actor_label(actor):
                raise issues.StoreError('자신의 임시 첨부만 읽을 수 있어요.', 403)
            if a['expires_at'] <= db.now_iso():
                raise issues.StoreError('임시 첨부가 만료됐어요.', 410)
    return a


def open_content(a):
    if not a['storage_key']:
        raise issues.StoreError('URL 첨부에는 저장 파일이 없어요.')
    try:
        return file_path(a['storage_key']).open('rb')
    except FileNotFoundError as e:
        raise issues._not_found('첨부 파일') from e


FORMATS = {
    '.png': ('image', 'image/png'), '.jpg': ('image', 'image/jpeg'),
    '.jpeg': ('image', 'image/jpeg'), '.gif': ('image', 'image/gif'),
    '.webp': ('image', 'image/webp'), '.mp4': ('video', 'video/mp4'),
    '.webm': ('video', 'video/webm'), '.mp3': ('audio', 'audio/mpeg'),
    '.wav': ('audio', 'audio/wav'), '.ogg': ('audio', 'audio/ogg'),
    '.m4a': ('audio', 'audio/mp4'), '.md': ('markdown', 'text/markdown'),
    '.json': ('json', 'application/json'),
}


def _owner(actor):
    if actor.get('kind') != 'human':
        raise issues.StoreError('첨부 변경은 사람만 할 수 있어요.', 403)
    return issues.actor_label(actor)


def file_path(key):
    """DB 값도 신뢰하지 않고 UUID 키와 실제 경로를 검사한다."""
    if not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{32}', key):
        raise issues.StoreError('잘못된 첨부 저장 키예요.')
    root = (config.DATA_DIR / 'attachments').resolve()
    if not root.is_relative_to(config.DATA_DIR.resolve()):
        raise issues.StoreError('첨부 저장 경로가 데이터 폴더를 벗어나요.')
    path = root / key
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise issues.StoreError('첨부 저장 경로가 잘못됐어요.')
    return path


def _name(name):
    if not isinstance(name, str) or not name or len(name) > 255 or name in ('.', '..'):
        raise issues.StoreError('파일 이름이 잘못됐어요.')
    if any(ord(c) < 32 or c in '/\\:' or ord(c) == 127 for c in name) or name.endswith((' ', '.')):
        raise issues.StoreError('파일 이름에 경로나 제어 문자를 넣을 수 없어요.')
    return name


def _validate(ext, data):
    head = data[:64]
    valid = {
        '.png': head.startswith(b'\x89PNG\r\n\x1a\n'),
        '.jpg': head.startswith(b'\xff\xd8\xff'),
        '.jpeg': head.startswith(b'\xff\xd8\xff'),
        '.gif': head.startswith((b'GIF87a', b'GIF89a')),
        '.webp': head.startswith(b'RIFF') and head[8:12] == b'WEBP',
        '.wav': head.startswith(b'RIFF') and head[8:12] == b'WAVE',
        '.ogg': head.startswith(b'OggS'),
        '.mp3': head.startswith(b'ID3') or (len(head) >= 2 and head[0] == 255 and head[1] & 0xe0 == 0xe0),
        '.webm': head.startswith(b'\x1aE\xdf\xa3') and b'webm' in data[:4096],
        '.mp4': head[4:8] == b'ftyp' and head[8:12] in (b'isom', b'iso2', b'mp41', b'mp42', b'avc1', b'M4V ', b'dash'),
        '.m4a': head[4:8] == b'ftyp' and head[8:12] in (b'M4A ', b'M4B ', b'isom'),
    }
    if ext in ('.md', '.json'):
        try:
            text = data.decode('utf-8-sig')
            if '\x00' in text:
                raise ValueError()
            if ext == '.json':
                json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (UnicodeError, ValueError, RecursionError) as e:
            raise issues.StoreError('JSON 문법 또는 UTF-8 텍스트 형식이 잘못됐어요.' if ext == '.json' else 'UTF-8 Markdown 파일이 필요해요.') from e
    elif not valid.get(ext):
        raise issues.StoreError('확장자와 파일 콘텐츠가 맞지 않아요.')


def _insert(c, owner, kind, name, media_type, size, key=None, url=None):
    aid = uuid.uuid4().hex
    now = issues._now()
    c.execute('INSERT INTO attachments VALUES(?,NULL,?,?,?,?,?,?,?,?,?)',
              (aid, owner, kind, name, media_type, size, key, url,
               now.isoformat(timespec='seconds'), (now + timedelta(seconds=config.ATTACHMENT_TTL_SECONDS)).isoformat(timespec='seconds')))
    return dict(c.execute('SELECT * FROM attachments WHERE id=?', (aid,)).fetchone())


def upload(actor, name, chunks):
    """바이트 스트림을 상한 내에서 저장한다. 실패하면 DB 행을 남기지 않는다."""
    owner = _owner(actor)
    name = _name(name)
    ext = Path(name).suffix.lower()
    if ext not in FORMATS:
        raise issues.StoreError('허용되지 않는 파일 형식이에요.')
    key = uuid.uuid4().hex
    path = file_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # 업로드와 만료 정리를 직렬화한다.
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            size = 0
            with path.open('xb') as f:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise issues.StoreError('업로드 바이트가 잘못됐어요.')
                    size += len(chunk)
                    if size > config.ATTACHMENT_MAX_BYTES:
                        raise issues.StoreError('파일 용량 제한을 넘었어요.', 413)
                    f.write(chunk)
            if not size:
                raise issues.StoreError('빈 파일은 첨부할 수 없어요.')
            _validate(ext, path.read_bytes())
            result = _insert(c, owner, FORMATS[ext][0], name, FORMATS[ext][1], size, key)
        return result
    except BaseException:
        # 파일 삭제 실패 시에도 미등록 파일은 cleanup에서 다시 회수한다.
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def add_url(actor, url):
    owner = _owner(actor)
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) <= 32 for c in url) or '\\' in url:
        raise issues.StoreError('HTTP(S) URL이 필요해요.')
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError()
        parsed.port
    except ValueError as e:
        raise issues.StoreError('HTTP(S) URL이 필요해요.') from e
    with db.connect() as c:
        return _insert(c, owner, 'url', url, '', 0, url=url)


def link(c, actor, issue_id, attachment_ids):
    """발행 트랜잭션 안에서 호출한다. 같은 이슈로 재시도하면 중복 없이 성공한다."""
    owner = _owner(actor)
    if not isinstance(attachment_ids, (list, tuple)) or any(not isinstance(i, str) for i in attachment_ids):
        raise issues.StoreError('첨부 ID 목록이 필요해요.')
    if len(set(attachment_ids)) != len(attachment_ids):
        raise issues.StoreError('첨부 ID가 중복됐어요.')
    row = c.execute('SELECT * FROM issues WHERE id=?', (issue_id,)).fetchone()
    if row is None:
        raise issues._not_found()
    selected = []
    for aid in attachment_ids:
        a = c.execute('SELECT * FROM attachments WHERE id=?', (aid,)).fetchone()
        if a is None:
            raise issues._not_found('첨부')
        if a['owner'] != owner or a['issue_id'] not in (None, issue_id):
            raise issues.StoreError('다른 작성자나 이슈의 첨부는 연결할 수 없어요.', 403)
        if a['issue_id'] is None and a['expires_at'] <= db.now_iso():
            raise issues.StoreError('임시 첨부가 만료됐어요.', 409)
        if a['storage_key'] and not file_path(a['storage_key']).is_file():
            raise issues.StoreError('첨부 파일이 없어요.', 409)
        selected.append(a)
    existing = c.execute('SELECT COUNT(*),COALESCE(SUM(size),0) FROM attachments WHERE issue_id=?', (issue_id,)).fetchone()
    new = [a for a in selected if a['issue_id'] is None]
    if existing[0] + len(new) > config.ATTACHMENT_MAX_COUNT or existing[1] + sum(a['size'] for a in new) > config.ATTACHMENT_TOTAL_BYTES:
        raise issues.StoreError('이슈의 첨부 개수 또는 합계 용량 제한을 넘었어요.', 413)
    for a in new:
        c.execute('UPDATE attachments SET issue_id=?,expires_at=NULL WHERE id=?', (issue_id, a['id']))


def list_for_issue(issue_id):
    with db.connect() as c:
        return [dict(r) for r in c.execute('SELECT * FROM attachments WHERE issue_id=? ORDER BY created_at,id', (issue_id,))]


def delete(actor, attachment_id):
    owner = _owner(actor)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        a = c.execute('SELECT * FROM attachments WHERE id=?', (attachment_id,)).fetchone()
        if a is None:
            raise issues._not_found('첨부')
        if a['owner'] != owner:
            raise issues.StoreError('자신의 첨부만 삭제할 수 있어요.', 403)
        c.execute('DELETE FROM attachments WHERE id=?', (attachment_id,))
    issues._cleanup_attachments()


def cleanup():
    """만료·삭제 큐·중단된 업로드 파일을 회수하며 파일 실패는 다음 호출에 재시도한다."""
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('DELETE FROM attachments WHERE issue_id IS NULL AND expires_at<=?', (db.now_iso(),))
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        for row in c.execute('SELECT storage_key FROM attachment_gc').fetchall():
            try:
                file_path(row[0]).unlink(missing_ok=True)
            except (OSError, issues.StoreError):
                continue
            c.execute('DELETE FROM attachment_gc WHERE storage_key=?', (row[0],))
        root = config.DATA_DIR / 'attachments'
        if root.is_dir():
            cutoff = (issues._now() - timedelta(seconds=config.ATTACHMENT_TTL_SECONDS)).timestamp()
            for path in root.iterdir():
                if not re.fullmatch(r'[0-9a-f]{32}', path.name):
                    continue
                if c.execute('SELECT 1 FROM attachments WHERE storage_key=?', (path.name,)).fetchone():
                    continue
                try:
                    safe = file_path(path.name)
                    if safe.stat().st_mtime <= cutoff:
                        safe.unlink()
                except (OSError, issues.StoreError):
                    pass
