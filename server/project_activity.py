"""점검과 기존 작업의 점유를 같은 DB 트랜잭션에서 확인한다."""


def audit_active(c, key=None):
    return c.execute("""SELECT 1 FROM project_audits a JOIN projects p ON p.id=a.project_id
        WHERE a.status IN ('preparing','running','cancelling')""" +
        (' AND p.key=?' if key else '') + ' LIMIT 1', (key,) if key else ()).fetchone() is not None


def lanes(c):
    return {(r['key'], 'audit') for r in c.execute("""SELECT p.key FROM project_audits a
        JOIN projects p ON p.id=a.project_id WHERE a.status IN ('preparing','running','cancelling')""")} | {
        (r['key'], 'initialization') for r in c.execute("""SELECT p.key FROM project_initializations a
        JOIN projects p ON p.id=a.project_id WHERE a.status IN ('running','cancelling')""")}


def audit_block(c, key):
    import jobs
    block = jobs.start_block(c, key, 'audit')
    if block:
        return block
    if any(lane[0] == key for lane in jobs._active_lanes(c)):
        return '프로젝트 작업이 진행 중이에요. 끝난 뒤 점검해 주세요.'
    if c.execute("""SELECT 1 FROM project_initializations a JOIN projects p ON p.id=a.project_id
        WHERE p.key=? AND a.status IN ('queued','running','cancelling')""", (key,)).fetchone():
        return '프로젝트 초기 생성이 진행 중이에요.'
    return None


def pending_jobs(c, key):
    return [dict(r) for r in c.execute("""SELECT j.id,j.mode,j.provider FROM jobs j
        JOIN issues i ON i.id=j.issue_id JOIN projects p ON p.id=i.project_id
        WHERE p.key=? AND j.status='queued' ORDER BY j.id""", (key,))]
