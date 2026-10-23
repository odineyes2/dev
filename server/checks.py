"""검사 등록부(DEV-161) — Settings 회귀검사의 모든 절차에 붙는 고정 번호(R)와 검사 파일 번호(T-프로젝트-###)."""
import re
from pathlib import Path

import db

# mode: settable(Settings에서 On/Off/Auto) · always(끌 수 없음) · managed(다른 설정이 정함, 이 화면에서는 보기만)
# 한 번 준 번호는 바꾸거나 다시 쓰지 않는다. 없애는 항목은 retired=True로 남긴다.
REGISTRY = (
    dict(number='R01', key='agent_task_checks', name='Task 확인 항목', stage='에이전트', mode='managed',
         description='계획서 Task의 `확인:` 항목을 실행해요. 항목은 계획서·Task 본문이 정해요.'),
    dict(number='R02', key='agent_related', name='Task 관련 검사', stage='에이전트', mode='settable',
         description='Task 확인 항목과 직접 작성·수정한 검사를 실행하도록 지시해요. Claude·Codex 공통이에요.'),
    dict(number='R03', key='agent_api', name='새 API 요청 검사', stage='에이전트', mode='settable',
         description='새 HTTP 라우트에 실제 앱 요청 검사를 작성·실행하도록 지시해요.'),
    dict(number='R04', key='agent_capture', name='UI 캡처 확인', stage='에이전트', mode='settable',
         description='변경 화면을 낮·밤 × PC·모바일로 캡처하고 확인하도록 지시해요.'),
    dict(number='R05', key='agent_js_syntax', name='JS 문법 검사', stage='에이전트', mode='managed',
         description='바꾼 JS 파일을 `node --check`로 확인해요. 공통 하네스 규칙이에요.'),
    dict(number='R06', key='server_completion', name='완료 전 테스트', stage='서버·완료', mode='settable',
         description='작업 완료 등록 전에 선택된 테스트 파일을 실행해요. 실패하면 한 번 수정 재시도해요.'),
    dict(number='R07', key='server_merge', name='병합 전 테스트', stage='서버·병합', mode='settable',
         description='시험 병합 결과를 검사해요. 동일 코드의 통과 결과를 재사용해요.'),
    dict(number='R08', key='server_files', name='검사 파일(py·JS)', stage='서버(R06·R07 안)', mode='managed',
         description='프로젝트의 tests/test_*.py·test_*.js 파일이에요. 파일마다 T-번호가 있고 R06·R07 안에서 실행돼요.'),
    dict(number='R09', key='server_repeat', name='바뀐 검사 반복 실행', stage='서버·완료', mode='managed',
         description='Task가 바꾼 검사를 3번 돌려 불안정한 검사를 가려요.'),
    dict(number='R10', key='server_retry', name='병합 실패 재실행 분류', stage='서버·병합', mode='managed',
         description='병합에서 실패한 검사를 2번 더 돌려 불안정한 검사인지 가려요.'),
    dict(number='R11', key='server_rollback', name='롤백 후 테스트', stage='서버·롤백', mode='settable',
         description='롤백 사본에서 선택된 테스트 파일을 실행해요.'),
    dict(number='R12', key='server_api_warning', name='새 API 검사 누락 경고', stage='서버·완료', mode='settable',
         description='새 라우트에 실제 요청 테스트가 있는지 검사하고, 없으면 완료 요약에 경고해요.'),
    dict(number='R13', key='scope_warning', name='바꿀 파일 밖 변경 경고', stage='서버·완료', mode='managed',
         description='Task에 적힌 바꿀 파일 밖의 변경을 완료 메모에 알려요.'),
    dict(number='R14', key='project_prepare', name='프로젝트 검사 준비', stage='서버·완료 전', mode='managed',
         description='Moai 같은 프로젝트의 검사 의존성·실행기를 모델 호출 전에 확인해요.'),
    dict(number='R15', key='web_build', name='웹 빌드 검사', stage='서버·병합', mode='managed',
         description='프로젝트가 선언한 웹 빌드를 격리 사본에서 확인해요.'),
    dict(number='R16', key='reproduce', name='실패 검사 재현', stage='사람이 시작', mode='always',
         description='실패한 검사를 고정된 커밋 사본에서 3번 다시 돌려요.'),
    dict(number='R17', key='troubleshoot', name='자동 복구', stage='서버', mode='managed',
         description='실패한 승인 Task를 한 번 복구해요. Auto 위임 설정을 따라요.'),
    dict(number='R18', key='qa_round', name='QA 라운드', stage='서버', mode='managed',
         description='In Review 이슈의 QA예요. QA 설정을 따라요.'),
    dict(number='R19', key='fixed_auth', name='인증·권한·입력 검증', stage='고정', mode='always',
         description='모든 API의 로그인·권한·입력 검증이에요.'),
    dict(number='R20', key='fixed_approval', name='승인 범위', stage='고정', mode='always',
         description='계획서 승인과 Task만 실행해요.'),
    dict(number='R21', key='fixed_git', name='Git 충돌·변경 중 반영 방지', stage='고정', mode='always',
         description='시험 병합과 ff-only 반영으로 충돌·검사 중 변경을 막아요.'),
    dict(number='R22', key='fixed_result', name='결과 형식·커밋 연결', stage='고정', mode='always',
         description='완료 결과 형식, 커밋 연결, 캡처 산출물 커밋 거절이에요.'),
    dict(number='R23', key='fixed_health', name='재시작 health 확인·되돌리기', stage='고정', mode='always',
         description='병합 뒤 재시작의 health를 확인하고 실패하면 되돌려요.'),
)
BY_KEY = {r['key']: r for r in REGISTRY}
FILE_NUMBER = 'R08'


def number_of(key):
    """설정 키 → 절차 번호('R07'). 검사 파일 키는 R08."""
    if key.startswith('file:'):
        return FILE_NUMBER
    return BY_KEY.get(key, {}).get('number', '')


def unlocked():
    """Settings에서 On/Off/Auto를 바꿀 수 있는 키."""
    return {r['key'] for r in REGISTRY if r['mode'] == 'settable'}


def kind(path):
    return 'js' if str(path).endswith('.js') else 'py'


def _ready(c):
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE name='check_files'").fetchone())


def label(project, number):
    return f'T-{project}-{number:03d}'


def file_numbers(project, paths):
    """검사 파일 경로(저장소 기준) → 'T-프로젝트-###'. 처음 보는 파일은 새 번호를 받고, 사라진 파일은 삭제로 표시한다.
    지워진 번호와 이름이 바뀐 옛 번호는 다시 쓰지 않는다. DB가 아직 없으면 빈 값."""
    paths = sorted({Path(p).as_posix() for p in paths})
    with db.connect() as c:
        if not _ready(c):
            return {}
        rows = {r['path']: r for r in c.execute('SELECT path,number,deleted FROM check_files WHERE project_key=?', (project,))}
        top = max([r['number'] for r in rows.values()] or [0])
        for p in paths:
            if p not in rows:
                top += 1
                c.execute('INSERT INTO check_files(project_key,path,number,first_seen,deleted) VALUES(?,?,?,?,0)',
                          (project, p, top, db.now_iso()))
                rows[p] = {'number': top}
            elif rows[p]['deleted']:
                c.execute('UPDATE check_files SET deleted=0 WHERE project_key=? AND path=?', (project, p))
        gone = [p for p, r in rows.items() if p not in paths and not r['deleted']]
        for p in gone:
            c.execute('UPDATE check_files SET deleted=1 WHERE project_key=? AND path=?', (project, p))
        return {p: label(project, rows[p]['number']) for p in paths}


def file_label(project, path):
    """이미 매겨진 번호만 읽는다(쓰지 않음)."""
    with db.connect() as c:
        if not _ready(c):
            return ''
        r = c.execute('SELECT number FROM check_files WHERE project_key=? AND path=?', (project, Path(path).as_posix())).fetchone()
        return label(project, r['number']) if r else ''


def valid_file_key(key):
    return bool(re.fullmatch(r'file:test_[A-Za-z0-9_]+\.(py|js)', key))
