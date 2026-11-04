"""미연결 서비스 경고(DEV-247-2) — Task 맡기기 확인창의 execute.service_warning이 미연결·실패면 참, 연결·포트 0·도메인 없음이면 거짓인지 임시 DB로 검사한다."""
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix='dev-service-warning-'))
os.environ['DEV_DATA_DIR'] = str(TMP / 'data')
os.environ['DEV_DB_PATH'] = str(TMP / 'data' / 'dev.db')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))

import db  # noqa: E402
import execute  # noqa: E402
import issues  # noqa: E402

db.init()
HUMAN = {'kind': 'human', 'name': 'admin'}
FAILS = []


def check(cond, label):
    print(('ok   ' if cond else 'FAIL ') + label)
    if not cond:
        FAILS.append(label)


def sql(query, args=()):
    with db.connect() as c:
        return c.execute(query, args).lastrowid


def project(key, domain, port, service_status=None):
    """프로젝트와 Task 하나를 만들고 그 Task의 실행 패널 경고를 돌려준다."""
    issues.create_project(HUMAN, key, key)
    sql("UPDATE projects SET service_domain=?, service_port=? WHERE key=?", (domain, port, key))
    if service_status:
        sql("INSERT INTO project_services(project_id, status, actor, action, updated_at) "
            "SELECT id, ?, 'human:admin', 'connect', ? FROM projects WHERE key=?", (service_status, db.now_iso(), key))
    parent = issues.create_issue(HUMAN, key, '부모')
    task = issues.create_issue(HUMAN, key, 'Task', parent=parent['ref'])
    return execute.panel(issues.get_issue(task['ref']))['service_warning']


check(project('NA', 'na.lomebrote.com', 8401) is True, '도메인·포트 있고 연결 행 없음 → 경고')
check(project('FA', 'fa.lomebrote.com', 8402, 'failed') is True, '연결 실패 → 경고')
check(project('RU', 'ru.lomebrote.com', 8403, 'running') is True, '연결 중 → 경고')
check(project('CO', 'co.lomebrote.com', 8404, 'connected') is False, '연결됨 → 경고 없음')
check(project('ZP', 'zp.lomebrote.com', 0) is False, '포트 0(외부 운영 서비스) → 경고 없음')
check(project('ND', '', 8405) is False, '도메인 없음 → 경고 없음')
check(execute.panel({'parent_ref': None}) is None, 'Task가 아니면 패널 없음')

print(f'\n{len(FAILS)} failed' if FAILS else '\nall passed')
sys.exit(1 if FAILS else 0)
