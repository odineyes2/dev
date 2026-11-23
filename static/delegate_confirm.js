// 맡기기 확인 창 내용(DEV-266) — 무엇을(카드)·작업·에이전트·검사(실행만)·조건부 대기 안내만 만든다. DOM 없음.
(function(root){
  const KINDS = {
    execute: {title: '실행을 맡길까요?', task: '태스크 실행', ok: '실행 맡기기'},
    review: {title: '검토를 맡길까요?', task: '계획 검토', ok: '검토 맡기기'},
    'change-review': {title: '수정을 맡길까요?', task: '수정 피드백 재검토', ok: '수정 맡기기'},
  };
  const PROVIDER = {claude: 'Claude', codex: 'Codex'};
  // 본문을 카드에 쓸 평문으로 — 마크다운 기호를 걷고 앞 600자만.
  function plainBody(text){
    return String(text || '').replace(/\*\*|__|`/g, '').replace(/^\s*(?:#+|[-*>]|\d+\.)\s+/gm, '')
      .replace(/\n{2,}/g, '\n').trim().slice(0, 600);
  }
  function delegateConfirm(it, kind, provider){
    const k = KINDS[kind] || KINDS.review;
    const d = (it.delegates || {})[provider] || {};
    const agent = d.agent || PROVIDER[provider] || provider || '';
    const model = d.model_name || d.model || 'CLI 기본값';
    const isExec = kind === 'execute';
    return {
      title: `${it.ref} ${k.title}`,
      card: {title: it.title || '', body: plainBody(it.body)},
      task: k.task,
      agent: `${agent} · ${model}`,
      notice: it.wait_notice || '',
      warning: isExec && it.execute && it.execute.service_warning ? '아직 외부 주소로 확인할 수 없어요 — 서비스 연결이 끝나지 않았어요.' : '',
      checks: isExec && it.execute ? it.execute.checks || null : null,
      okText: k.ok,
    };
  }
  root.delegateConfirm = delegateConfirm;
  if(typeof module !== 'undefined' && module.exports) module.exports = {delegateConfirm, plainBody};
})(typeof window !== 'undefined' ? window : globalThis);
