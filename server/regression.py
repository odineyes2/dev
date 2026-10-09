"""서버 검사 단계·개별 파일과 에이전트 검사 지시를 공통 설정으로 관리한다."""
import ast
from pathlib import Path

import db
import issues

CHECKS = (
    ('server_completion', '서버', '완료 전 전체 검사', '작업 완료 등록 전에 선택된 테스트 파일을 실행해요. 실패하면 한 번 수정 재시도해요.'),
    ('server_merge', '서버', '병합 전 전체 검사', '시험 병합 결과를 검사해요. 완료 때 검사한 코드와 같으면 중복 실행하지 않아요.'),
    ('server_rollback', '서버', '롤백 후 전체 검사', '롤백 사본에서 선택된 테스트 파일을 실행해요.'),
    ('server_api_warning', '서버', '새 API 검사 누락 경고', '새 라우트에 실제 요청 테스트가 있는지 검사하고, 없으면 완료 요약에 경고해요.'),
    ('agent_related', '에이전트', 'Task 관련 검사', 'Task 확인 항목과 직접 작성·수정한 검사를 실행하도록 지시해요. Claude·Codex 공통이에요.'),
    ('agent_api', '에이전트', '새 API 요청 검사', '새 HTTP 라우트에 실제 앱 요청 검사를 작성·실행하도록 지시해요.'),
    ('agent_capture', '에이전트', 'UI 캡처 확인', '변경 화면을 낮·밤 × PC·모바일로 캡처하고 확인하도록 지시해요.'),
)


def catalog():
    rows = [dict(key=k, group=g, name=n, description=d) for k, g, n, d in CHECKS]
    for path in sorted(Path(__file__).resolve().parents[1].joinpath('tests').glob('test_*.py')):
        doc = ast.get_docstring(ast.parse(path.read_text(encoding='utf-8'))) or '개별 회귀 검사'
        rows.append(dict(key='file:' + path.name, group='테스트 파일', name=path.name,
                         description=doc.splitlines()[0]))
    return rows


def values():
    with db.connect() as c:
        return {r['key'][11:]: r['value'] == 'true' for r in c.execute(
            "SELECT key,value FROM app_settings WHERE key LIKE 'regression:%'")}


def enabled(key):
    return values().get(key, True)


def settings():
    saved = values()
    return {'checks': [{**row, 'enabled': saved.get(row['key'], True)} for row in catalog()]}


def update(actor, changes):
    if actor['kind'] != 'human':
        raise issues.StoreError('검사 설정은 사람만 바꿀 수 있어요.', 403)
    keys = {row['key'] for row in catalog()}
    if not isinstance(changes, dict) or not changes or any(k not in keys or type(v) is not bool for k, v in changes.items()):
        raise issues.StoreError('검사 종류와 켜짐·꺼짐 값을 확인해 주세요.', 400)
    with db.connect() as c:
        for key, value in changes.items():
            c.execute('INSERT INTO app_settings(key,value,updated_at,updated_by) VALUES(?,?,?,?) '
                      'ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at,updated_by=excluded.updated_by',
                      ('regression:' + key, 'true' if value else 'false', db.now_iso(), issues.actor_label(actor)))
    return settings()
