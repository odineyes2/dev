"""프로젝트 초기 생성의 점유를 같은 DB 트랜잭션에서 확인한다."""


def lanes(c):
    return {(r['key'], 'initialization') for r in c.execute("""SELECT p.key FROM project_initializations a
        JOIN projects p ON p.id=a.project_id WHERE a.status IN ('running','cancelling')""")}

