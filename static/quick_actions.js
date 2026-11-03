// 빠른 액션 판정(DEV-243) — 목록·보드 버튼, 상세 진행 줄 go, 검토 절, Task 실행 버튼이 모두 quickAction 하나만 본다. DOM 없음.
// 입력은 status 이름이 아니라 서버 `issues._action_facts`의 사실(action_context)이고, 보이는 조건은 서버가 받는 조건과 같다.
const LIVE_MERGE = new Set(['병합 중', '운영 반영 중', '재시작 확인 중', '복구 중', '복구 확인 중']);

// 유효한 승인 — 최신 판에 대한 approve/approve_notes이고 낡지 않았다(서버 decide·execute.blocked_reason과 같은 기준)
function approvedPlan(c){
  const a = c?.approval;
  return !!(c?.plan_version && a && !a.stale && a.plan_version === c.plan_version && ['approve', 'approve_notes'].includes(a.verdict));
}

// item: {status, parent_ref, merge_state, action_context} — 목록 항목·상세 응답·child 행 공통. opt.pending은 화면이 처리 중인 요청.
// 반환: {kind: 'review'|'decision'|'execute'|'task-approve'|'complete-tree'|'', why}
function quickAction(item, opt = {}){
  const c = item?.action_context, none = why => ({kind: '', why});
  if(!c) return none('unknown');
  if(['done', 'closed'].includes(item.status)) return none('closed');
  if(c.has_active_job || opt.pending) return none('busy');
  if(item.parent_ref){
    if(c.superseded) return none('superseded');
    // Task 승인은 부모 상태와 상관없이 받는다(병합·운영 반영이 안 끝났으면 서버 _checks_gate가 거절)
    if(item.status === 'in_review') return LIVE_MERGE.has(item.merge_state) ? none('merging') : {kind: 'task-approve', why: ''};
    if(['backlog', 'changes_requested'].includes(item.status) && approvedPlan(c.parent)) return {kind: 'execute', why: ''};
    return none('not-runnable');
  }
  if(item.status === 'in_review') return {kind: 'complete-tree', why: ''};
  // 계획 승인은 status와 상관없이 — 최신 판이 있고 유효한 승인·재검토 요청이 없을 때
  if(c.plan_version) return c.revision_requested ? none('revision') : approvedPlan(c) ? none('approved') : {kind: 'decision', why: ''};
  // 계획서가 없으면 Task가 이미 있어도 검토를 받는다. 다른 에이전트가 잡고 있으면 서버가 거절한다.
  return c.claim_active ? none('claimed') : {kind: 'review', why: ''};
}

if(typeof module !== 'undefined') module.exports = {quickAction, approvedPlan, LIVE_MERGE};
