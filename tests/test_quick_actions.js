// 빠른 액션 판정(DEV-243-2) — quickAction이 본문 사례마다 서버 수락 조건과 같은 버튼을 내는지 표로 검사한다.
const test = require('node:test');
const assert = require('node:assert');
const {quickAction} = require('../static/quick_actions.js');

const OK = v => ({verdict: 'approve', plan_version: v, stale: false});
const ctx = (kw = {}) => ({plan_version: null, approval: null, revision_requested: false, has_children: false, has_execution: false,
  has_active_job: false, claim_active: false, claimed_by: null, lease_until: null, superseded: false, parent: null, ...kw});
const top = (status, kw) => ({status, parent_ref: null, action_context: ctx(kw)});
const task = (status, kw, parent = ctx({plan_version: 1, approval: OK(1)})) => ({status, parent_ref: 'DEV-1', action_context: ctx({...kw, parent})});

const CASES = [
  ['저장된 수정 피드백 → 수정 맡기기', top('changes_requested', {plan_version:1, approval:OK(1), can_dispatch_change_request:true}), 'change-review'],
  ['NS-116: backlog+v1+승인 없음 → 계획 승인(검토 아님)', top('backlog', {plan_version: 1}), 'decision'],
  ['in_progress+v1+승인 없음 → 계획 승인', top('in_progress', {plan_version: 1}), 'decision'],
  ['triage+v2+v1 승인만 → 계획 승인', top('triage', {plan_version: 2, approval: OK(1)}), 'decision'],
  ['낡은 승인 → 계획 승인', top('backlog', {plan_version: 1, approval: {...OK(1), stale: true}}), 'decision'],
  ['유효 승인 → 없음', top('in_progress', {plan_version: 1, approval: OK(1)}), ''],
  ['changes_requested Task+부모 승인 → 실행', task('changes_requested'), 'execute'],
  ['backlog Task+부모 승인 → 실행', task('backlog'), 'execute'],
  ['backlog Task+부모 승인 없음 → 없음', task('backlog', {}, ctx({plan_version: 1})), ''],
  ['backlog Task+부모 거절 → 없음', task('backlog', {}, ctx({plan_version: 1, approval: {verdict: 'reject', plan_version: 1, stale: false}})), ''],
  ['부모 in_progress의 in_review Task → Task 승인', task('in_review'), 'task-approve'],
  ['병합 중 in_review Task → 없음', {...task('in_review'), merge_state: '병합 중'}, ''],
  ['계획서 없음+Task 있음 → 검토', top('backlog', {has_children: true}), 'review'],
  ['계획서 없음+in_progress → 검토', top('in_progress'), 'review'],
  ['계획서 없음+점유 → 없음(claimed)', top('in_progress', {claim_active: true, claimed_by: 'agent:1'}), ''],
  ['job·run 있음 → 없음', top('backlog', {has_active_job: true}), ''],
  ['Task job 있음 → 없음', task('backlog', {has_active_job: true}), ''],
  ['done → 없음', top('done', {plan_version: 1}), ''],
  ['closed → 없음', top('closed'), ''],
  ['closed Task → 없음', task('closed'), ''],
  ['재검토 요청 → 없음', top('backlog', {plan_version: 1, revision_requested: true}), ''],
  ['대체된 Task → 없음', task('backlog', {superseded: true}), ''],
  ['최상위 in_review → 전체 완료', top('in_review', {plan_version: 1, approval: OK(1)}), 'complete-tree'],
  ['action_context 없음 → 없음', {status: 'backlog', parent_ref: null}, ''],
];

for(const [name, item, kind] of CASES) test(name, () => assert.strictEqual(quickAction(item).kind, kind));

test('이유: 점유·처리 중·종결', () => {
  assert.strictEqual(quickAction(top('in_progress', {claim_active: true})).why, 'claimed');
  assert.strictEqual(quickAction(top('backlog', {has_active_job: true})).why, 'busy');
  assert.strictEqual(quickAction(top('closed')).why, 'closed');
});

test('처리 중 표시(pending)는 버튼을 숨긴다', () => {
  assert.strictEqual(quickAction(top('backlog'), {pending: true}).kind, '');
  assert.strictEqual(quickAction(top('backlog'), {pending: false}).kind, 'review');
});
