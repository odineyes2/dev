"""??? Issue? QA ??? ????? ?? ??? ????."""
import json

import db
import issues


def panel(ref):
    issue = issues.get_issue(ref)
    if issue['parent_id'] is not None:
        raise issues.StoreError('QA? ??? Issue?? ??? ???.', 400)
    with db.connect() as c:
        rounds = []
        for row in c.execute("SELECT q.*,r.input_tokens,r.output_tokens,r.cost_usd,r.started_at FROM qa_rounds q LEFT JOIN runs r ON r.id=q.run_id WHERE q.issue_id=? ORDER BY q.round_number DESC", (issue['id'],)):
            entry = dict(row)
            entry['snapshot'] = json.loads(entry.pop('snapshot_json'))
            entry['items'] = []
            for item in c.execute('SELECT * FROM qa_items WHERE qa_id=? ORDER BY requirement_id', (row['id'],)):
                value = dict(item)
                value['evidence'] = json.loads(value.pop('evidence_json'))
                value['task_refs'] = json.loads(value.pop('task_refs_json'))
                entry['items'].append(value)
            batches = []
            for batch in c.execute('SELECT * FROM qa_rework_batches WHERE qa_id=? ORDER BY id', (row['id'],)):
                value = dict(batch)
                value['task_refs'] = json.loads(value.pop('task_refs_json'))
                batches.append(value)
            entry['rework_batches'] = batches
            rounds.append(entry)
        count = c.execute("SELECT COUNT(*) FROM qa_rework_batches WHERE issue_id=? AND status IN ('registered','complete')", (issue['id'],)).fetchone()[0]
    return {'issue_ref': issue['ref'], 'rounds': rounds, 'rework_count': count, 'rework_limit': 5}
