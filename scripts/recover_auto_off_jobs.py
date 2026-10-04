"""명시한 legacy job만 OFF 증거를 검사한다. 기본은 읽기 전용 dry-run이다."""
import argparse
import json
import sqlite3
from pathlib import Path


def recover(c, job_ids, apply=False):
    """한 트랜잭션에서 모든 대상의 증거를 검사한 뒤 사유만 보완한다."""
    c.row_factory = sqlite3.Row
    c.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
    results = []
    try:
        for job_id in dict.fromkeys(job_ids):
            j = c.execute('SELECT j.*,i.project_id FROM jobs j JOIN issues i ON i.id=j.issue_id WHERE j.id=?', (job_id,)).fetchone()
            if (not j or j['source'] != 'auto' or j['mode'] != 'execute'
                    or j['status'] != 'cancelled' or j['started_at'] or j['run_id']
                    or j['previous_status'] != 'backlog'
                    or j['cancellation_reason'] not in (None, 'auto_execute_off')
                    or c.execute('SELECT 1 FROM runs WHERE issue_id=?', (j['issue_id'],)).fetchone()):
                raise ValueError(f'job {job_id}: 미실행 자동 취소 조건이 맞지 않는다')
            events = c.execute("SELECT * FROM events WHERE issue_id=? AND kind='status' ORDER BY id", (j['issue_id'],)).fetchall()
            waiting = [e for e in events if json.loads(e['data_json']).get('job_id') == job_id
                       and json.loads(e['data_json']).get('to') == 'waiting']
            if len(waiting) != 1:
                raise ValueError(f'job {job_id}: 대기 증거가 모호하다')
            following = next((e for e in events if e['id'] > waiting[0]['id']), None)
            if (not following or following['actor'] != j['actor']
                    or following['body'] != '대기 전 상태로 복구했어요'
                    or json.loads(following['data_json']).get('from') != 'waiting'
                    or json.loads(following['data_json']).get('to') != 'backlog'
                    or j['note'] != '자동 위임 조건이 바뀌었어요'):
                raise ValueError(f'job {job_id}: 자동 대기 복구 증거가 없다')
            changes = c.execute('SELECT * FROM project_auto_settings_events WHERE project_id=? AND id>? AND created_at>=? AND created_at<=? ORDER BY id',
                                (j['project_id'], j['delegation_id'] or 0, waiting[0]['created_at'], following['created_at'])).fetchall()
            # 시간 해상도 안에서 여러 설정 변경이 겹치면 추정하지 않는다.
            if len(changes) != 1:
                raise ValueError(f'job {job_id}: OFF 설정 증거가 없거나 모호하다')
            event = changes[0]
            before, after = json.loads(event['before_json']), json.loads(event['after_json'])
            if (not event['actor'].startswith('human:') or not before.get('auto_execute')
                    or after.get('auto_execute') or event['created_at'] < j['created_at']):
                raise ValueError(f'job {job_id}: 실행 OFF 전환이 아니다')
            # OFF 외 조건 변경·사람 개입을 함께 관측한 취소는 복구하지 않는다.
            for key in ('auto_review', 'auto_approve', 'provider_order'):
                if before.get(key) != after.get(key):
                    raise ValueError(f'job {job_id}: 여러 조건이 동시에 변경됐다')
            parent = c.execute('SELECT parent_id FROM issues WHERE id=?', (j['issue_id'],)).fetchone()[0]
            if c.execute('SELECT 1 FROM decisions WHERE issue_id=? AND created_at>=? AND created_at<=?',
                         (parent, waiting[0]['created_at'], following['created_at'])).fetchone():
                raise ValueError(f'job {job_id}: 승인 변경 증거가 있다')
            if c.execute("SELECT 1 FROM events WHERE issue_id IN (?,?) AND id<>? AND id<>? AND created_at>=? AND created_at<=? AND kind IN ('status','claim','edit','plan','decision')",
                         (j['issue_id'], parent, waiting[0]['id'], following['id'], waiting[0]['created_at'], following['created_at'])).fetchone():
                raise ValueError(f'job {job_id}: 다른 조건 변경 증거가 있다')
            if j['cancellation_event_id'] not in (None, event['id']):
                raise ValueError(f'job {job_id}: 기존 취소 증거가 다르다')
            results.append({'job_id': job_id, 'event_id': event['id'], 'applied': apply})
        if apply:
            for result in results:
                c.execute("UPDATE jobs SET cancellation_reason='auto_execute_off',cancellation_event_id=? WHERE id=?", (result['event_id'], result['job_id']))
            c.commit()
        else:
            c.rollback()
        return results
    except Exception:
        c.rollback()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--job-id', required=True, action='append', type=int)
    parser.add_argument('--apply', action='store_true', help='운영 담당자가 OFF 증거를 확인한 대상에만 적용한다')
    args = parser.parse_args()
    # dry-run에서 파일 생성·마이그레이션·서버 초기화를 하지 않는다.
    uri = args.db.resolve().as_uri() + ('?mode=rw' if args.apply else '?mode=ro')
    with sqlite3.connect(uri, uri=True) as c:
        c.execute('PRAGMA foreign_keys=ON')
        try:
            print(json.dumps(recover(c, args.job_id, args.apply), ensure_ascii=False))
        except (ValueError, sqlite3.Error) as error:
            parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
