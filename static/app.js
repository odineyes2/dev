// dev 화면 — 해시 라우팅 한 파일. #/ 목록 · #/board 칸반 · #/issue/NS-1 상세 · #/new 새 이슈 · #/agents · #/projects
'use strict';

const STATUSES = ['backlog', 'waiting', 'triage', 'in_progress', 'in_review', 'changes_requested', 'on_hold', 'done', 'closed'];
const STATUS_LABEL = { backlog: 'Backlog', waiting: 'Waiting', triage: 'Triage', in_progress: 'In Progress', in_review: 'In Review',
  changes_requested: 'Changes Requested', on_hold: 'On Hold', done: 'Done', closed: 'Closed' };
const PRIORITIES = ['urgent', 'high', 'medium', 'low', 'none'];
const OPEN_STATUSES = STATUSES.filter(s => s !== 'done' && s !== 'closed');

let me = null;
let restartGraceUntil = 0;
let projects = [];
let agentsById = new Map();
let tokenUnit = 'auto';
const view = document.getElementById('view');

// 결정·결과 폼의 임시 첨부를 같은 방식으로 관리한다.
function attachPicker({list, send, button, input, textarea, enabled, imagesOnly = false}) {
    const rFiles = [];   // {name, busy, error, data, preview}
    const rDelete = f => { if(f.preview){ URL.revokeObjectURL(f.preview); f.preview = null; } if(f.data) void api('DELETE', `/api/attachments/${encodeURIComponent(f.data.id)}`).catch(() => {}); };
    const rClear = () => rFiles.splice(0).forEach(rDelete);
    window.addEventListener('hashchange', rClear, { once: true });   // 화면을 떠나면 연결 안 된 첨부를 지운다
    const rDraw = () => {
      list.innerHTML = rFiles.map((f, i) => `<span class="cmp-chip${f.error ? ' bad' : ''}">${f.preview ? `<span class="cmp-thumb"><img src="${f.preview}" alt=""></span>` : ''}${esc(f.name)}${f.busy ? ' · 업로드 중…' : f.error ? ' · 실패' : ''}
        <button type="button" data-picker-remove="${i}" title="첨부 제거" aria-label="${esc(f.name)} 첨부 제거"><svg class="ico" aria-hidden="true"><use href="#i-x"/></svg></button></span>`).join('');
      send.disabled = rFiles.some(f => f.busy || f.error);
    };
    const rAdd = files => files.forEach(async file => {
      if(!enabled() || (imagesOnly && !file.type.startsWith('image/'))) return;
      const f = { name: file.name || 'image.png', busy: true, error: '', preview: file.type.startsWith('image/') ? URL.createObjectURL(file) : null };
      rFiles.push(f); rDraw();
      try{
        const body = new FormData(); body.append('file', file, f.name);
        f.data = await api('POST', '/api/attachments', body);
        if(!rFiles.includes(f)) rDelete(f);
      }catch(err){ f.error = err.message; toast(`${f.name}: ${err.message} 제거하고 다시 첨부해 주세요.`); }
      finally{ f.busy = false; rDraw(); }
    });
    button.addEventListener('click', () => input.click());
    input.addEventListener('change', (e) => { rAdd([...e.target.files]); e.target.value = ''; });
    textarea.addEventListener('paste', (e) => {
      if(!enabled()) return;
      const imgs = [...(e.clipboardData?.files || [])].filter(x => x.type.startsWith('image/'));
      if(imgs.length){ e.preventDefault(); rAdd(imgs); }
    });
    list.addEventListener('click', (e) => {
      const b = e.target.closest('[data-picker-remove]'); if(!b) return;
      const [f] = rFiles.splice(+b.dataset.pickerRemove, 1); if(f) rDelete(f); rDraw();
    });
    return { ids: () => rFiles.map(f => f.data.id), clear: () => { rClear(); rDraw(); },
      take: () => rFiles.splice(0).forEach(f => { if(f.preview) URL.revokeObjectURL(f.preview); }) };
}

// ---- 공용 ----
function esc(s){
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function formatTokens(value){
  if(value == null) return '미측정';
  const n = Number(value);
  if(!Number.isFinite(n)) return '미측정';
  if(n === 0) return '0';
  let unit = tokenUnit;
  if(unit === 'auto') unit = Math.abs(n) >= 1e12 ? 'T' : Math.abs(n) >= 1e9 ? 'B' : Math.abs(n) >= 1e6 ? 'M' : Math.abs(n) >= 1e4 ? 'k' : 'full';
  if(unit === 'full') return n.toLocaleString('ko-KR');
  const divisor = {k:1e3, M:1e6, B:1e9, T:1e12}[unit];
  const scaled = n / divisor;
  if(Math.abs(scaled) < 0.1) return n.toLocaleString('ko-KR');
  const digits = Math.abs(scaled) >= 100 ? 0 : 1;
  return `${Number(scaled.toFixed(digits)).toLocaleString('ko-KR', {maximumFractionDigits:digits})}${unit}`;
}
function tokenHtml(value){
  const label = formatTokens(value);
  const exact = value == null ? '' : ` title="${esc(Number(value).toLocaleString('ko-KR'))} 토큰"`;
  return `<span${exact}>${esc(label)}</span>`;
}
function toast(msg){
  const el = document.getElementById('toast');
  el.textContent = msg; el.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => { el.hidden = true; }, 3000);
}
// 계획서의 '사람이 할 일'(NS-60) — Done 전에 다 했는지 확인받는다. 없으면 바로 통과. 서버도 확인 없이는 Done을 거절한다.
function confirmDialog(message, {description, summary, body: workBody, wrapItems = false, itemLimit = 3, items = [], moreLabel, okText = '확인', danger = false, input = false, focusCancel = false, checklist = null} = {}){
  return new Promise(resolve => {
    const previous = document.activeElement;
    const dialog = document.createElement('dialog');
    dialog.className = 'confirm-dialog';
    dialog.setAttribute('role', 'alertdialog');
    dialog.setAttribute('aria-modal', 'true');
    const [title, ...lines] = String(message).split('\n');
    const detail = description ?? lines.join('\n');
    const list = items.length ? `<div class="confirm-items${wrapItems ? ' wrap' : ''}">${items.slice(0,itemLimit).map(x => `<div class="confirm-item"><span class="confirm-ref">${esc(x.ref)}</span><span class="confirm-item-title" title="${esc(x.title)}">${esc(x.title)}</span>${x.note ? `<span class="confirm-note">${esc(x.note)}</span>` : ''}</div>`).join('')}${items.length > itemLimit ? `<div class="confirm-more">${esc(moreLabel ?? `외 ${items.length - itemLimit}개`)}</div>` : ''}</div>` : '';
    dialog.innerHTML = `<form method="dialog"><div class="confirm-badge${danger ? ' confirm-badge-danger' : ''}"><svg class="ico" aria-hidden="true" focusable="false"><use href="#i-circle-${danger ? 'alert' : 'help'}"/></svg></div><h2 id="confirm-title">${esc(title)}</h2><div class="confirm-content">${summary ? `<p class="confirm-summary">${esc(summary)}</p>` : ''}${summary && (list || workBody) ? '<h3 class="confirm-work-title">작업 내용</h3>' : ''}${summary ? '' : `<p class="confirm-message">${esc(detail)}</p>`}${list}${workBody ? `<p class="confirm-body">${esc(workBody)}</p>` : ''}${summary ? `<p class="confirm-message confirm-foot">${esc(detail)}</p>` : ''}${input ? '<input class="confirm-input" aria-label="닫는 이유" required>' : ''}${checklist ? checklistHtml(checklist) : ''}</div><div class="confirm-actions"><button type="button" class="confirm-cancel">취소</button><button type="submit" class="confirm-ok${danger ? ' danger' : ''}">${esc(okText)}</button></div></form>`;
    dialog.setAttribute('aria-labelledby', 'confirm-title');
    if(!detail) dialog.querySelector('.confirm-message').remove();
    if(detail){ dialog.querySelector('.confirm-message').id = 'confirm-description'; dialog.setAttribute('aria-describedby', 'confirm-description'); }
    document.body.append(dialog);
    let result = input ? null : false;
    dialog.querySelector('.confirm-cancel').onclick = () => dialog.close();
    dialog.onclick = e => { if(e.target === dialog) dialog.close(); };
    dialog.querySelector('form').onsubmit = e => { e.preventDefault(); result = input ? dialog.querySelector('.confirm-input').value
      : checklist ? [...dialog.querySelectorAll('input[data-check]:checked')].map(x => x.dataset.check) : true; dialog.close(); };
    dialog.onclose = () => { dialog.remove(); if(previous?.isConnected) previous.focus({preventScroll:true}); resolve(result); };
    dialog.showModal();
    (input ? dialog.querySelector('.confirm-input') : dialog.querySelector(focusCancel ? '.confirm-cancel' : '.confirm-ok')).focus();
  });
}
// 맡기기 확인창의 검사 체크리스트(DEV-238) — 계획 검사는 체크된 채 해제 가능, 필수와 언제나 절차는 체크된 채 비활성.
// 좁은 화면에서는 한 줄 요약으로 접고 누르면 펼친다. 고른 계획 검사 키 목록이 확인창의 결과가 된다.
function checklistHtml(c){
  const plan = c.plan || [], always = c.always || [];
  const row = (i, attr) => `<label class="check-row"><input type="checkbox" ${attr} checked ${i.required ? 'disabled' : ''}><span class="check-text"><span class="check-title">${attr.startsWith('data-check') ? `<code>${esc(i.cmd)}</code>` : `<b>${esc(i.label)}</b>`}${i.required ? ' <span class="check-badge">필수</span>' : ''}</span><small>${esc(i.why)}</small></span></label>`;
  const required = plan.filter(i => i.required).length + always.length;
  const open = window.matchMedia('(min-width: 601px)').matches ? ' open' : '';
  return `<details class="confirm-checks"${open}><summary>검사 ${plan.length + always.length}개 · 필수 ${required}개</summary>
    <div class="check-group"><div class="check-head">계획 검사</div>${plan.length ? plan.map(i => row(i, `data-check="${esc(i.key)}"`)).join('') : `<p class="check-none">${c.none ? `없음 — ${esc(c.none)}` : '계획서에 이 Task의 검사가 없어요.'}</p>`}</div>
    <div class="check-group"><div class="check-head">언제나 하는 절차</div>${always.map(i => row(i, `data-always="${esc(i.key)}"`)).join('')}</div>
    <p class="check-foot">체크를 해제한 검사는 이번 실행에서 돌리지 않고 기록에 남겨요. 서버가 끝난 뒤 고른 검사를 다시 돌려요.</p></details>`;
}
// 마지막 검토 메모를 HTML 없이 평문으로 보여 준다.
function inReviewNote(it){
  const event = [...(it.events || [])].reverse().find(e => e.kind === 'status' && e.data?.to === 'in_review' && e.body?.trim());
  return (event?.body || '').replace(/\*\*|__|`/g, '').replace(/^\s*(?:#+|[-*>])\s+/gm, '').trim();
}
function confirmCompleteTree(root){
  const children = root.children || [];
  const warn = children.filter(c => !['done', 'closed'].includes(c.status)
    && (c.status !== 'in_review' || ['병합 대기', '재시작 대기', '되돌림'].includes(c.merge_state)));
  return confirmDialog(`${root.ref}을 완료할까요?`, {
    summary:root.title, body:children.length ? '' : inReviewNote(root),
    items:children.map(c => ({...c, note:STATUS_LABEL[c.status] || c.status})), wrapItems:true, itemLimit:6,
    description:'하위 Task까지 모두 Done이 되고, 되돌릴 수 없어요.' + (warn.length ? `\n병합 대기 중이거나 아직 끝나지 않은 Task가 있어요: ${warn.map(c => c.ref).join(', ')}` : ''),
    okText:'완료로 바꾸기', focusCancel:true
  });
}
async function confirmChecks(items){
  return !(items || []).length || await confirmDialog('사람이 할 일을 모두 했나요?', {description:items.map(x => `☐ ${x}`).join('\n'), okText:'완료로 바꾸기', focusCancel:true});
}
async function api(method, url, body, isCurrent = () => true, {notifyErrors = true} = {}){
  const opt = { method, headers: { 'X-Requested-With': 'dev' } };
  if(body instanceof FormData) opt.body = body;
  else if(body !== undefined){ opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const t0 = performance.now();
  const res = await fetch(url, opt);
  perf.fetchMs += performance.now() - t0;
  perf.server = res.headers.get('Server-Timing') || perf.server;
  if(res.status === 401){ if(Date.now() < restartGraceUntil) throw new Error('서버 재시작 후 다시 연결하고 있어요.'); if(isCurrent()) showLogin(); throw new Error('로그인이 필요해요.'); }
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if(!res.ok){ const msg = (data && data.detail) || `요청이 실패했어요(${res.status}).`; if(isCurrent() && notifyErrors) toast(msg); const error = new Error(msg); error.status = res.status; throw error; }
  return data;
}
// 저장 버튼은 요청이 끝날 때까지 막는다 — 두 번 눌러(모바일 두 번 탭) 같은 이슈가 두 번 저장되던 문제(DEV-12).
async function whileBusy(btn, fn){
  if(!btn || btn.disabled) return;
  btn.disabled = true;
  try{ return await fn(); }
  catch(e){ /* api()가 이미 알림을 띄웠다 */ }
  finally{ if(btn.isConnected) btn.disabled = false; }
}
function submitBtn(e){ return e.submitter || e.target.querySelector('[type=submit]'); }
function fmtTime(iso){
  if(!iso) return '';
  const d = new Date(iso), diff = (Date.now() - d) / 1000;
  if(diff < 60) return '방금';
  if(diff < 3600) return `${Math.floor(diff / 60)}분 전`;
  if(diff < 86400) return `${Math.floor(diff / 3600)}시간 전`;
  if(diff < 86400 * 7) return `${Math.floor(diff / 86400)}일 전`;
  return d.toLocaleDateString();
}
function titleHtml(i){ return i.title_missing ? '<span class="dim">(제목 없음 — 에이전트가 지어요)</span>' : esc(i.title); }
function statusHtml(s){ return `<span class="status${s === 'in_progress' ? ' status-in-progress' : ''}" style="--sc:var(--s-${esc(s)})">${esc(STATUS_LABEL[s] || s)}</span>`; }
function prioHtml(p){ return p && p !== 'none' ? `<span class="prio ${esc(p)}">${esc(p)}</span>` : ''; }
function approvalHtml(a){   // 계획서 결정 배지 — 새 판이 올라와 무효가 된 것은 흐리게
  if(!a) return '';
  if(a.stale) return `<span class="status dim" style="--sc:var(--s-in_progress)" title="새 계획서가 올라와 결정이 무효예요">결정 무효</span>`;
  return `<span class="status" style="--sc:var(--s-${VERDICT_COLOR[a.verdict]})">${VERDICT_LABEL[a.verdict]}</span>`;
}
function blockedHtml(bs){   // 선행 이슈 — Done만 완료로 표시하고 상태를 함께 보여준다.
  if(!bs || !bs.length) return '';
  const open = bs.some(b => b.status !== 'done');
  return `<div class="sub">${open ? '대기 · ' : ''}선행: ${bs.map(b => `<a href="#/issue/${esc(b.ref)}" class="${b.status === 'done' ? 'dim' : ''}">${esc(b.ref)}</a> ${statusHtml(b.status)}`).join(', ')}</div>`;
}
function labelsHtml(ls){ return (ls || []).map(l => `<span class="label">${esc(l)}</span>`).join(''); }
function actorName(a){
  if(!a) return '';
  if(a.startsWith('human:')) return a.slice(6);
  const ag = agentsById.get(Number(a.slice(6)));
  return ag ? ag.name : a;
}
function actorHtml(a, model){
  const m = model ? `<span class="model">${esc(model)}</span>` : '';
  return `<span class="who">${esc(actorName(a))}</span>${m}`;
}

// 아주 작은 마크다운 — 먼저 전부 이스케이프하고 나서 몇 가지 표기만 태그로 바꾼다(링크는 http(s)만).
function md(src){
  const blocks = String(src || '').replace(/\r\n/g, '\n').split(/\n```/);
  let out = '';
  blocks.forEach((b, i) => {
    if(i % 2 === 1){
      const nl = b.indexOf('\n');
      out += `<pre><code>${esc(nl >= 0 ? b.slice(nl + 1) : '')}</code></pre>`;
      return;
    }
    if(i === 0 && b.startsWith('```')) b = b.slice(3);
    let list = null;
    const flush = () => { if(list){ out += `</${list}>`; list = null; } };
    for(const raw of b.split('\n')){
      const line = inline(esc(raw));
      let m;
      if((m = raw.match(/^(#{1,3})\s+/))){ flush(); out += `<h${m[1].length}>${inline(esc(raw.slice(m[0].length)))}</h${m[1].length}>`; }
      else if((m = raw.match(/^\s*[-*]\s+/))){ if(list !== 'ul'){ flush(); out += '<ul>'; list = 'ul'; } out += `<li>${inline(esc(raw.slice(m[0].length)))}</li>`; }
      else if((m = raw.match(/^\s*\d+\.\s+/))){ if(list !== 'ol'){ flush(); out += '<ol>'; list = 'ol'; } out += `<li>${inline(esc(raw.slice(m[0].length)))}</li>`; }
      else if((m = raw.match(/^>\s?/))){ flush(); out += `<blockquote>${inline(esc(raw.slice(m[0].length)))}</blockquote>`; }
      else if(!raw.trim()){ flush(); }
      else { flush(); out += `<p>${line}</p>`; }
    }
    flush();
  });
  return `<div class="md">${out}</div>`;
}
function inline(s){
  return s.replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|\s)(https?:\/\/[^\s<]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>')
    .replace(/\b([A-Z][A-Z0-9]{0,9}-\d+(?:-\d+)?)\b/g, '<a href="#/issue/$1">$1</a>');
}

// ---- 전역 재시작 안내(DEV-159) — 탭 이동과 무관하게 같은 요청·타이머를 쓴다. ----
const restartBanner = document.getElementById('restart-banner');
let restartState = null, restartIdentity = null, restartExpanded = false, restartNeedsReload = false;
let restartTimer = null, restartDoneTimer = null, restartRequest = null, restartEpoch = 0, restartWatching = false;
let restartRecovering = false;
let restartChoicePending = false;
const restartEdited = new Set();
document.addEventListener('input', e => {
  if(e.target.matches('input, textarea, select, [contenteditable]')) restartEdited.add(e.target);
});
function restartHasDraft(){
  for(const el of restartEdited){
    if(!el.isConnected){ restartEdited.delete(el); continue; }
    if(el.isContentEditable ? el.textContent.trim() : el.type === 'file' ? el.files.length : String(el.value || '').trim()) return true;
  }
  return !!document.querySelector('#cmp-files .cmp-chip') || !!document.getElementById('cmp-body').value.trim();
}
function paintRestartBanner(){
  const s = restartState;
  const focused = restartBanner.contains(document.activeElement) ? document.activeElement.className : '';
  restartBanner.hidden = !s;
  document.body.classList.toggle('has-restart-banner', !!s);
  if(!s){ restartBanner.replaceChildren(); return; }
  const refs = s.waiting || [], shown = restartExpanded ? refs : refs.slice(0, 2);
  const links = shown.map(ref => `<a href="#/issue/${encodeURIComponent(ref)}">${esc(ref)}</a>`).join(', ');
  const more = !restartExpanded && refs.length > 2 ? ` 외 ${refs.length - 2}건` : '';
  const busy = (s.busy || []).map(r => `${r.ref}(${r.minutes}분째)`).join(', ');
  const words = {
    merging: `${s.ref} 병합을 검사하고 있어요 — 끝나면 dev 서버가 재시작돼요.`,
    reserved: `${s.ref} 작업 뒤 dev 재시작이 예약돼 있어요 — 재시작을 마칠 때까지 새 작업을 시작하지 않아요.`,
    waiting: `dev 재시작을 기다리고 있어요 — ${busy || '도는 작업'}이 끝나면 재시작해요.${s.overdue_minutes > 0 ? ' (60분 넘게 밀리고 있어요)' : ''}`,
    restarting: 'dev 서버를 재시작하고 있어요. 다시 연결되면 화면을 새로 고쳐요.',
    complete: '재시작을 마쳤어요. 대기열을 이어서 진행해요.'
  };
  restartBanner.dataset.phase = s.phase;
  restartBanner.innerHTML = `<div class="restart-banner-inner"><span class="restart-dot" aria-hidden="true"></span>
    <div class="restart-copy"><span>${esc(words[s.phase] || words.restarting)}</span>${refs.length ? ` <span class="restart-next">다음 작업: ${links}${more}</span>` : ''}</div>
    ${refs.length > 2 ? `<button type="button" class="ghost restart-expand" aria-expanded="${restartExpanded}" aria-label="다음 작업 ${restartExpanded ? '접기' : '모두 보기'}">${restartExpanded ? '접기' : '모두 보기'}</button>` : ''}
    ${s.phase === 'waiting' && me?.kind === 'human' ? `<div class="restart-actions${s.overdue_minutes > 0 ? ' overdue' : ''}"><button type="button" class="restart-now" ${restartChoicePending ? 'disabled' : ''}>지금 재시작</button><button type="button" class="ghost restart-release" ${restartChoicePending ? 'disabled' : ''}>재시작은 나중에</button></div>` : ''}
    ${restartNeedsReload ? '<button type="button" class="restart-reload">새로 고침</button>' : ''}</div>`;
  restartBanner.querySelector('.restart-expand')?.addEventListener('click', () => { restartExpanded = !restartExpanded; paintRestartBanner(); });
  restartBanner.querySelector('.restart-reload')?.addEventListener('click', () => location.reload());
  restartBanner.querySelector('.restart-now')?.addEventListener('click', () => chooseRestart('now'));
  restartBanner.querySelector('.restart-release')?.addEventListener('click', () => chooseRestart('release'));
  restartBanner.querySelector('.restart-copy').onclick = e => {
    if(refs.length > 2 && !e.target.closest('a')){ restartExpanded = !restartExpanded; paintRestartBanner(); }
  };
  if(focused === 'ghost restart-expand') restartBanner.querySelector('.restart-expand')?.focus();
  if(focused === 'restart-reload') restartBanner.querySelector('.restart-reload')?.focus();
}
async function chooseRestart(action){
  const count = restartState?.busy?.length || 0;
  const refs = (restartState?.busy || []).map(r => r.ref).join(', ');
  const message = action === 'now' ? '지금 재시작할까요?' : '재시작을 미룰까요?';
  const description = action === 'now'
    ? `도는 작업 ${count}개(${refs})를 종료해요. 끊긴 실행은 orphaned로 기록되고 기존 재시작 복구 흐름에 따라 정리돼요. 필요하면 다시 대기열에 넣어 주세요.`
    : '병합은 그대로 두고 새 작업 막기를 풀어요. dev는 재시작 전까지 옛 코드로 돌아요 — 나중에 직접 재시작해 주세요.';
  if(restartChoicePending || !await confirmDialog(message, {description, okText:action==='now' ? '지금 재시작' : '재시작 미루기', focusCancel:true})) return;
  restartChoicePending = true; paintRestartBanner();
  try{ await api('POST', `/api/orchestrate/self-restart/${action}`); toast(action === 'now' ? '재시작을 요청했어요.' : '재시작 대기를 해제했어요.'); }
  catch(error){ restartChoicePending = false; paintRestartBanner(); }
}
function finishRestart(identity){
  const changedCode = identity.revision !== restartIdentity.revision;
  restartIdentity = identity;
  restartRecovering = false;
  restartGraceUntil = 0;
  restartNeedsReload = changedCode && restartHasDraft();
  restartState = {phase:'complete', waiting:[]};
  paintRestartBanner();
  if(changedCode && !restartNeedsReload){ location.reload(); return; }
  clearTimeout(restartDoneTimer);
  if(!restartNeedsReload) restartDoneTimer = setTimeout(() => { restartState = null; paintRestartBanner(); }, 6000);
}
async function checkRestartHealth(epoch){
  const response = await fetch('/api/health?execution_identity=1', {cache:'no-store', signal:AbortSignal.timeout(5000)});
  if(!response.ok) throw new Error('재시작을 기다리고 있어요.');
  const identity = await response.json();
  if(epoch !== restartEpoch || !restartWatching) return;
  if(identity.process_id && restartIdentity && identity.process_id !== restartIdentity.process_id){ finishRestart(identity); return; }
  // 같은 프로세스에서 대기 기록만 사라지면 보류·해제된 것이므로 재시작 완료로 알리지 않는다.
  if(!restartRecovering && restartState?.phase !== 'complete'){ restartState = null; restartIdentity = null; paintRestartBanner(); }
}
function receiveRestartState(state){
  if(!state) return;
  if(restartIdentity && state.process_id !== restartIdentity.process_id){ finishRestart(state); return; }
  clearTimeout(restartDoneTimer);
  restartIdentity ||= {process_id:state.process_id, revision:state.revision};
  restartRecovering = false;
  restartState = state;
  if(state.phase !== 'waiting') restartChoicePending = false;
  restartGraceUntil = Date.now() + 30000;
  paintRestartBanner();
}
function restartJobsSnapshot(){
  if(restartRequest) return restartRequest;
  const epoch = restartEpoch;
  const request = (async () => {
    try{
      const response = await fetch('/api/jobs', {cache:'no-store', signal:AbortSignal.timeout(5000)});
      if(!response.ok){
        if(response.status === 401 && !restartIdentity && epoch === restartEpoch) showLogin();
        throw new Error('대기열을 읽지 못했어요.');
      }
      const data = await response.json();
      if(epoch === restartEpoch && restartWatching){
        if(data.self_restart) receiveRestartState(data.self_restart);
        else if(restartIdentity && restartState?.phase !== 'complete') await checkRestartHealth(epoch);
      }
      return data;
    }catch(error){
      if(epoch === restartEpoch && restartWatching && restartIdentity && restartState?.phase !== 'complete'){
        restartRecovering = true;
        restartGraceUntil = Date.now() + 30000;
        restartState = {...restartState, phase:'restarting'};
        paintRestartBanner();
      }
      throw error;
    }finally{ if(restartRequest === request) restartRequest = null; }
  })();
  restartRequest = request;
  return request;
}
async function pollRestart(){
  const epoch = restartEpoch;
  try{
    if(restartRecovering) await checkRestartHealth(epoch);
    else await restartJobsSnapshot();
  }catch(error){ /* 안내 띠를 유지하고 다음 주기에 다시 연결한다. */ }
  if(epoch !== restartEpoch || !restartWatching) return;
  restartTimer = setTimeout(pollRestart, restartRecovering ? 2000 : restartState && restartState.phase !== 'complete' ? 5000 : 20000);
}
function stopRestartPolling(){
  restartWatching = false; restartEpoch++;
  clearTimeout(restartTimer); clearTimeout(restartDoneTimer);
  restartRequest = null; restartState = null; restartIdentity = null; restartRecovering = false;
  restartExpanded = false; restartNeedsReload = false; restartGraceUntil = 0;
  restartChoicePending = false;
  paintRestartBanner();
}
function startRestartPolling(){
  stopRestartPolling();
  restartWatching = true;
  pollRestart();
}

// ---- 로그인 ----
function showLogin(reason){
  stopRestartPolling();
  document.getElementById('open-jupyter').hidden = true;
  document.getElementById('shell').hidden = true;
  document.getElementById('login').hidden = false;
  document.getElementById('login-error').textContent = reason === 'not_admin' ? 'nightshift 관리자 계정만 쓸 수 있어요.' : '';
}
document.getElementById('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const err = document.getElementById('login-error');
  err.textContent = '';
  const res = await fetch('/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'dev' },
    body: JSON.stringify({ username: document.getElementById('login-username').value, password: document.getElementById('login-password').value }) });
  const data = await res.json().catch(() => ({}));
  if(!res.ok || !data.ok){ err.textContent = data.detail || '로그인에 실패했어요.'; return; }
  document.getElementById('login-password').value = '';
  boot();
});
document.getElementById('logout').addEventListener('click', async () => {
  setUserMenu(false);
  await fetch('/api/auth/logout', { method: 'POST', headers: { 'X-Requested-With': 'dev' } });
  showLogin();
});

// ---- 밤/낮 — nightshift와 같게 두 상태. 고른 적 없으면 운영체제 설정을 따라가고, 누르면 반대로 고정한다.
// 처음 적용은 index.html의 인라인 스크립트(깜빡임 방지).
const darkQuery = window.matchMedia('(prefers-color-scheme: dark)');
function savedTheme(){ const t = localStorage.getItem('dev.theme'); return t === 'light' || t === 'dark' ? t : null; }
function effectiveTheme(){ return savedTheme() || (darkQuery.matches ? 'dark' : 'light'); }
function paintTheme(){
  const cur = effectiveTheme(), btn = document.getElementById('theme');
  btn.innerHTML = `<svg class="ico"><use href="#i-${cur === 'dark' ? 'sun' : 'moon'}"/></svg>`;
  btn.title = cur === 'dark' ? '낮 모드(밝은 화면)로 전환' : '밤 모드(어두운 화면)로 전환';
  btn.dataset.current = cur;
  if(savedTheme()) document.documentElement.dataset.theme = savedTheme();
  else delete document.documentElement.dataset.theme;
}
document.getElementById('theme').addEventListener('click', () => {
  localStorage.setItem('dev.theme', effectiveTheme() === 'dark' ? 'light' : 'dark');
  paintTheme();
});
darkQuery.addEventListener('change', paintTheme);   // 고른 적 없으면 운영체제를 따라 아이콘도 바뀐다
paintTheme();

// ---- 사용자 칩 → 메뉴(nightshift 열기·jupyter 열기·로그아웃) ----
const userChip = document.getElementById('user-chip'), userMenu = document.getElementById('user-menu');
function setUserMenu(open){ userMenu.hidden = !open; userChip.setAttribute('aria-expanded', String(open)); }
userChip.addEventListener('click', (e) => { e.stopPropagation(); setUserMenu(userMenu.hidden); });
document.addEventListener('click', (e) => { if(!userMenu.hidden && !userMenu.contains(e.target)) setUserMenu(false); });
document.addEventListener('keydown', (e) => { if(e.key === 'Escape') setUserMenu(false); });

// ---- 프로젝트 필터(모든 화면 공통, 브라우저에 기억) ----
const projectSel = document.getElementById('project-filter');
function currentProject(){ return localStorage.getItem('dev.project') || ''; }
function projectOptionsHtml(){
  const cur = projects.some(p => p.key === currentProject()) ? currentProject() : '';
  return `<option value="">All projects</option>` + projects.filter(p => !p.archived)
    .map(p => `<option value="${esc(p.key)}"${p.key === cur ? ' selected' : ''}>${esc(p.key)} · ${esc(p.name)}</option>`).join('');
}
function paintProjectFilter(){
  projectSel.innerHTML = projectOptionsHtml();
  projectSel.value = projects.some(p => p.key === currentProject()) ? currentProject() : '';
}
projectSel.addEventListener('change', () => { localStorage.setItem('dev.project', projectSel.value); route(); });

async function loadProjects(){ projects = (await api('GET', '/api/projects')).projects; paintProjectFilter(); }
async function loadAgents(){
  const list = (await api('GET', '/api/agents')).agents;
  agentsById = new Map(list.map(a => [a.id, a]));
  return list;
}

// ---- 속도 측정(DEV-42) — 화면 전환마다 받기·그리기 시간을 performance.measure로 남기고, 주소에 ?perf가 있으면 구석에 보인다 ----
const perf = { fetchMs: 0, server: '' };
function perfReport(name, t0){
  const total = performance.now() - t0, fetchMs = Math.min(perf.fetchMs, total);
  performance.measure(`route:${name}`, { start: t0, duration: total });
  if(!new URLSearchParams(location.search).has('perf')) return;
  let box = document.getElementById('perf-box');
  if(!box){
    box = document.createElement('div'); box.id = 'perf-box';
    box.style.cssText = 'position:fixed;right:8px;bottom:8px;z-index:99;max-width:min(92vw,520px);padding:6px 10px;'
      + 'font:11px/1.4 var(--mono);font-variant-numeric:tabular-nums;color:var(--dim);background:var(--panel);'
      + 'border:1px solid var(--border);border-radius:var(--r-sm);pointer-events:none';
    document.body.append(box);
  }
  box.textContent = `${name} ${total.toFixed(0)}ms · 받기 ${fetchMs.toFixed(0)} · 그리기 ${(total - fetchMs).toFixed(0)}`
    + (perf.server ? ` · 서버 ${perf.server}` : '');
}

// ---- 라우팅 ----
async function route(){
  stopLiveRefresh();
  if(!me || document.getElementById('shell').hidden) return;
  issueRenderSeq++;
  settingsSession = null;
  actionSession = null;
  disposeList();
  cleanupBoard();
  if(!composerAllowed()) closeComposer(true);
  document.getElementById('fab-new').hidden = !composerAllowed();
  let h = location.hash.replace(/^#\/?/, '').split('?')[0];
  // 기존 주소를 같은 설정 영역의 하위 주소로 정규화한다.
  if(h === 'settings' || h === 'types'){
    h = h === 'types' ? 'settings/types' : 'settings/auto';
    history.replaceState(null, '', '#/' + h + (location.hash.includes('?') ? '?' + location.hash.split('?')[1] : ''));
  }
  const [name, arg] = h.split('/');
  if(name !== 'agents') agentNewKey = null;
  const settingsTab = name === 'settings' ? (arg || 'auto') : null;
  const t0 = performance.now();
  perf.fetchMs = 0; perf.server = '';
  document.querySelectorAll('[data-nav]').forEach(a => a.classList.toggle('active', a.dataset.nav === (name || 'issues')));
  try{
    if(!name) await renderList();
    else if(name === 'trends' && (!arg || arg === 'stages' || arg === 'issues')) await renderTrends(arg);
    else if(name === 'board') await renderBoard();
    else if(name === 'issue' && arg) await renderIssue(decodeURIComponent(arg));
    else if(name === 'new') await renderNew(new URLSearchParams(location.hash.split('?')[1] || ''));
    else if(name === 'agents') await renderAgents();
    else if(name === 'projects'){
      const params = new URLSearchParams(location.hash.split('?')[1] || '');
      // 로그인 직후 프로젝트 목록보다 hash 이동이 먼저 와도 상세 주소를 보존한다.
      if(params.get('project') && !projects.some(p => p.key === params.get('project'))){
        const hash = location.hash;
        view.innerHTML = '<div class="usage-loading" aria-busy="true">프로젝트를 불러와요…</div>';
        await loadProjects();
        if(location.hash !== hash) return;
      }
      renderProjects(params.get('project'), params.get('tab') || 'manage');
    }
    else if(name === 'settings' && settingsTab === 'auto') await renderSettings();
    else if(name === 'settings' && settingsTab === 'types') await renderTypes();
    else if(name === 'settings' && settingsTab === 'fonts') renderFontSettings();
    else if(name === 'settings' && settingsTab === 'tokens') await renderTokenSettings();
    else view.innerHTML = '<div class="empty">없는 화면이에요.</div>';
  }catch(e){ if(!view.innerHTML) view.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
  perfReport(name || 'issues', t0);
}
window.addEventListener('hashchange', route);
// 화면 갱신을 직렬화하고 페이지 이동 후 타이머를 무효화한다.
let liveTimer = null, liveEpoch = 0, issueRenderSeq = 0;
function stopLiveRefresh(){ clearTimeout(liveTimer); liveEpoch++; }
function followProgress(refresh, current){
  stopLiveRefresh();
  const epoch = liveEpoch;
  const tick = async () => {
    if(epoch !== liveEpoch || !current() || document.getElementById('shell').hidden) return;
    try{ await refresh(); }catch(e){ /* 다음 갱신에서 재연결한다. */ }
    if(epoch === liveEpoch && current()) liveTimer = setTimeout(tick, 5000);
  };
  liveTimer = setTimeout(tick, 5000);
}
// LIVE_MERGE는 quick_actions.js에 있다
function activeProgress(i){ return i.review_running || i.job || ['in_progress','waiting'].includes(i.status) || LIVE_MERGE.has(i.merge_state); }
function splitProgress(text, mode){
  if(text.length <= 20 && !text.includes('—')) return {short:text, long:''};
  const ref = '[A-Z][A-Z0-9]*-\\d+(?:-\\d+)?';
  let short = '';
  const waiting = text.match(new RegExp(`^(${ref})(?: 작업이 끝날 때까지 기다려요|이 사람을 기다려요)`));
  if(waiting) short = `대기: ${waiting[1]}`;
  if(!short && /(?:병합·운영 반영 완료|Task 병합)(?:을|를) 기다려요/.test(text)){
    const first = text.match(new RegExp(ref));
    if(first) short = `병합 대기: ${first[0]}`;
  }
  if(!short && /^선행 (?:Task|Issue|이슈)\(/.test(text)){
    const refs = (text.match(new RegExp(ref, 'g')) || []).join(', ');
    if(refs) short = `선행: ${refs}`;
  }
  if(!short && text.startsWith('동시 실행 상한')) short = '동시 실행 상한 대기';
  if(!short){
    const first = text.split('—')[0].trim();
    short = Array.from(first).length > 20 ? (mode === 'review' ? '검토 대기' : '실행 대기') : first;
  }
  if(Array.from(short).length > 28) short = Array.from(short).slice(0, 27).join('') + '…';
  return {short, long:text};
}
function progressHtml(i){
  const run = !i.job?.note && !i.merge_state ? i.run_progress : null;   // 실행 단계 — 구현·멈춤(DEV-206)
  const text = i.job?.note || i.merge_state || run?.long || (i.status === 'in_progress' && i.parent_ref ? '구현 중' : '');
  const clock = clockHtml(i);
  if(!text) return clock ? `<div class="progress-note">${clock}</div>` : '';
  const {short, long} = run ? {short:run.short, long:run.long}
    : i.job?.note_short
    ? {short:i.job.note_short, long:i.job.note_short === text ? '' : text}
    : splitProgress(text, i.job?.mode);
  return `<div class="progress-note"><span class="progress-short">${esc(short)}</span>${clock}${long ? `<button type="button" class="progress-more" aria-label="자세히 보기" title="자세히 보기" aria-expanded="false" data-progress="${esc(long)}">ⓘ</button>` : ''}</div>`;
}
// In Progress 경과 시간(DEV-208) — 받은 뒤 흐른 시간은 tickClocks가 1초마다 더한다
function clockHtml(i){
  return i.status === 'in_progress' && i.in_progress_seconds != null
    ? `<span class="progress-clock" title="In Progress 경과 시간" data-elapsed="${i.in_progress_seconds}" data-at="${performance.now()}">${fmtClock(i.in_progress_seconds)}</span>` : '';
}
function fmtClock(sec){
  const s = Math.floor(Math.max(0, sec)), h = Math.floor(s / 3600), p = n => String(n).padStart(2, '0');
  return h ? `${h}:${p(Math.floor(s / 60) % 60)}:${p(s % 60)}` : `${p(Math.floor(s / 60))}:${p(s % 60)}`;
}
function tickClocks(){
  for(const el of document.querySelectorAll('.progress-clock')){
    el.textContent = fmtClock(Number(el.dataset.elapsed) + (performance.now() - Number(el.dataset.at)) / 1000);
  }
}
setInterval(tickClocks, 1000);
document.addEventListener('visibilitychange', () => { if(!document.hidden) tickClocks(); });
let progressButton = null;
function closeProgress(){
  if(progressButton){ progressButton.setAttribute('aria-expanded', 'false'); progressButton.removeAttribute('aria-describedby'); }
  progressButton = null;
  document.querySelector('#progress-pop')?.remove();
}
function positionProgress(){
  if(!progressButton) return;
  const pop = document.querySelector('#progress-pop');
  if(!pop || !progressButton.isConnected){ closeProgress(); return; }
  const anchor = progressButton.getBoundingClientRect();
  if(anchor.bottom < 0 || anchor.top > innerHeight){ closeProgress(); return; }
  const box = pop.getBoundingClientRect();
  pop.style.left = `${Math.max(8, Math.min(anchor.left, innerWidth - box.width - 8))}px`;
  pop.style.top = `${anchor.bottom + box.height <= innerHeight - 8 ? anchor.bottom : Math.max(8, anchor.top - box.height)}px`;
}
function showProgress(button){
  if(progressButton === button) return;
  closeProgress();
  progressButton = button;
  button.setAttribute('aria-expanded', 'true');
  button.setAttribute('aria-describedby', 'progress-pop');
  const pop = document.createElement('div');
  pop.id = 'progress-pop'; pop.className = 'progress-pop'; pop.setAttribute('role', 'tooltip');
  pop.textContent = button.dataset.progress;
  document.body.append(pop);
  positionProgress();
}
document.addEventListener('mouseover', e => {
  const button = e.target.closest('.progress-more');
  if(button) showProgress(button);
});
document.addEventListener('mouseout', e => {
  if(!progressButton || !e.target.closest('.progress-more, #progress-pop')) return;
  if(e.relatedTarget?.closest?.('.progress-more, #progress-pop')) return;
  closeProgress();
});
document.addEventListener('click', e => {
  const button = e.target.closest('.progress-more');
  if(!button){ if(progressButton && !e.target.closest('#progress-pop')) closeProgress(); return; }
  e.preventDefault(); e.stopPropagation();
  showProgress(button);
}, true);
document.addEventListener('keydown', e => { if(e.key === 'Escape') closeProgress(); });
window.addEventListener('scroll', positionProgress, true);
window.addEventListener('resize', positionProgress);
window.addEventListener('hashchange', closeProgress);

// ---- 목록 ----

let actionSession = null;
const pendingActions = new Set();
// 판정은 quick_actions.js의 quickAction 하나가 한다(DEV-243)
const actionKind=i=>quickAction(i,{pending:pendingActions.has(i.ref)}).kind;
function actionOff(kind,s,i){ return kind==='review' ? s.auto_review : kind==='execute' ? s.auto_execute : kind==='task-approve' ? s.auto_approve===true : kind==='decision' && i.status==='triage' && s.auto_plan_approve_available===true && s.auto_plan_approve===true; }
function actionHtml(i){
  const kind=actionKind(i); if(!kind) return '';
  const settings=actionSession?.settings;
  return `<div class="issue-actions" aria-label="Action">${(['review','execute'].includes(kind)?['claude','codex']:['']).map(provider=>{
    const label=provider ? `${provider==='claude'?'Claude':'Codex'}에게 ${kind==='review'?'검토':'실행'} 맡기기` : kind==='decision'?'계획 승인':kind==='task-approve'?'승인':'전체 완료';
    return `<button type="button" draggable="false" data-action="${kind}" data-ref="${esc(i.ref)}" data-provider="${provider}" title="${label}" aria-label="${label}" aria-disabled="${!settings || !!actionOff(kind,settings,i)}">${provider?`<svg class="ico brand-icon" aria-hidden="true"><use href="#i-${provider==='claude'?'claude':'openai'}"/></svg>`:`<svg class="ico" aria-hidden="true"><use href="#i-${({decision:'clipboard-check','task-approve':'check','complete-tree':'check-check'})[kind]}"/></svg>`}</button>`;
  }).join('')}</div>`;
}
function beginActions(current){ return actionSession={settings:null,items:new Map(),current}; }
async function loadActionSettings(session,items){
  items.forEach(i=>session.items.set(i.ref,i));
  // 보조 조회 오류는 Action 클릭 때 안내하며 발행 성공 알림을 덮지 않는다.
  try{ const s=await api('GET','/api/settings/auto',undefined,session.current,{notifyErrors:false});
    if(typeof s.auto_review!=='boolean' || typeof s.auto_execute!=='boolean') throw new Error('설정');
    session.settings=s;
  }catch(e){ session.settings=null; }
}
function bindActions(container,session,refresh){
  container.querySelectorAll('[data-action]').forEach(b=>{
    if(b.dataset.bound) return; b.dataset.bound='true';
    for(const type of ['pointerdown','mousedown','dragstart']) b.addEventListener(type,e=>{e.stopPropagation();if(type==='dragstart')e.preventDefault();});
    b.addEventListener('click',async e=>{
      e.stopPropagation();
      const i=session.items.get(b.dataset.ref),kind=b.dataset.action,provider=b.dataset.provider;
      const current=()=>actionSession===session && session.current();
      if(!current() || pendingActions.has(i.ref) || actionKind(i)!==kind) return;
      const settings=session.settings;
      if(!settings){toast('Auto 설정을 확인하지 못했어요. 새로고침해 다시 조회해 주세요.');return;}
      if(actionOff(kind,settings,i)){toast('Auto 모드에서는 해당 버튼이 비활성화됩니다.');return;}
      pendingActions.add(i.ref);
      container.querySelectorAll('[data-action]').forEach(x=>{if(x.dataset.ref===i.ref)x.disabled=true;});
      let checks=null;   // 실행 맡기기에서 사람이 고른 검사(DEV-238)
      try{
        if(kind==='complete-tree'){
          const root=await api('GET',`/api/issues/${encodeURIComponent(i.ref)}`,undefined,current);
          if(!current())return;
          const open=[root,...root.children].filter(x=>!['done','closed'].includes(x.status));
          if(!open.length){toast('이미 모두 끝났어요');return;}
          if(!await confirmCompleteTree(root)) return;
          if(!await confirmChecks(root.human_checks))return;
        }else if(!['decision','task-approve'].includes(kind)){
          const name=provider==='claude'?'Claude':'Codex';
          const detail=await api('GET',`/api/issues/${encodeURIComponent(i.ref)}`,undefined,current);
          const task=kind==='execute'?detail:null;
          if(!current())return;
          const limit=task?(provider==='codex'?`시간 제한은 ${Math.floor(task.execute.timeout_sec/60)}분이고 비용 상한은 없어요.`:`비용 상한은 $${task.execute.budget_usd}이에요.`):'';
          const scope=task ? '홈서버의 격리된 worktree에서 구현해요. 실행 후 기존 자동 병합 정책이 적용돼요.' : '홈서버에서 이슈와 코드를 읽고 계획서·질문을 남겨요(코드는 고치지 않아요).';
          const picked=await confirmDialog(`${i.ref}을(를) ${name}에게 ${kind==='review'?'검토':'실행'} 맡길까요?`, {description:task ? `${delegateLine(task, provider)}${scope}\n사용량은 이 서버에 로그인된 ${name} 계정에서 나가요.\n${limit}\n${QUEUE_LINE}` : reviewConfirmText(detail, provider), okText:kind==='review'?'검토 맡기기':'실행 맡기기', checklist:task?task.execute.checks:null});
          if(!picked)return;
          if(task) checks=picked;
        }
        if(!current())return;
        // 응답(대기열 등록·착수)까지 누른 버튼에 스피너를 보인다(DEV-221).
        b.innerHTML='<svg class="ico list-spinner" aria-hidden="true"><use href="#i-loader-circle"/></svg>'; b.setAttribute('aria-busy','true');
        await api('POST',`/api/issues/${encodeURIComponent(i.ref)}/${kind==='task-approve'?'status':kind}`,kind==='decision'?{verdict:'approve',note:'',plan_version:i.action_context.plan_version}:kind==='task-approve'?{status:'done'}:kind==='complete-tree'?{confirm_checks:true}:provider?(checks?{provider,checks}:{provider}):{},current);
        if(current())toast(kind==='decision'?'계획을 승인했어요.':kind==='task-approve'?'Task를 승인했어요.':kind==='complete-tree'?'묶음을 끝냈어요.':'작업을 대기열에 등록했어요.');
      }catch(e){ /* 최신 정보로 다시 판단한다. */ }
      finally{pendingActions.delete(i.ref);if(current())await refresh();}
    });
  });
}

function listState(){ return JSON.parse(localStorage.getItem('dev.list') || '{"statuses":[],"closed":false,"q":""}'); }
// 목록은 LIST_PAGE개씩 — 끝(#list-more)이 보이면 다음 묶음을 붙인다(DEV-7). 도구줄은 한 번만 그리고
// 필터·검색이 바뀌면 결과 칸만 처음부터 다시 받는다(검색 글칸의 포커스가 유지되게).
const LIST_PAGE = 50;
let listLoad = null;   // { seq, qs, offset, done, busy, seen }
let listObserver = null;
let listSearchTimer = null;
function disposeList(){
  clearTimeout(listSearchTimer);
  if(listLoad){
    clearTimeout(listLoad.timer);
    listLoad.body.setAttribute('aria-busy', 'false');
    listLoad.loading?.remove();
  }
  listLoad = null;
  listObserver?.disconnect();
  listObserver = null;
}
function processingAgentsHtml(i){
  const providers = i.processing_agents || [];
  if(!providers.length) return '<span class="dim">—</span>';
  const label = providers.length > 1 ? '협업 — Claude · Codex' : PROVIDER_NAME[providers[0]];
  return `<span class="processing-agents" role="img" aria-label="${esc(label)}" title="${esc(label)}">${providers.map(p => `<svg class="ico brand-icon" aria-hidden="true"><use href="#i-${p === 'claude' ? 'claude' : 'openai'}"/></svg>`).join('')}</span>`;
}
function formatDuration(seconds){
  if(seconds === null || seconds === undefined) return '미계측';
  const n = Math.floor(seconds), hours = Math.floor(n / 3600), minutes = Math.floor(n % 3600 / 60);
  return hours ? `${hours.toLocaleString()}시간 ${minutes}분` : minutes ? `${minutes}분 ${n % 60}초` : `${n}초`;
}
function completionTimeHtml(i){
  return ['done', 'closed'].includes(i.status) ? `<span class="completion-time" title="최초 작업 시작부터 종결까지 · 대기와 검토 시간 포함">${formatDuration(i.completion_seconds)}</span>` : '';
}
// 직접 실행분과 확정된 하위 Task 합계를 분리해 표시한다.
function issueTokensHtml(i, detail = false){
  const u = i.token_usage;
  const number = n => Number(n).toLocaleString('ko-KR');
  const describe = usage => {
    if(!usage) return {text:'미측정', explanation:'토큰 집계 정보가 아직 없어요.'};
    const u = usage;
    const measured = u.run_count > u.unmeasured_run_count;
    const partial = u.partial || u.unmeasured_run_count > 0 || u.running_run_count > 0;
    const total = number(u.total_tokens);
    let text = !u.run_count ? '실행 없음' : !measured ? '미측정' : `${partial ? '측정된 ' : ''}${formatTokens(u.total_tokens)}`;
    if(u.running_run_count) text += ' · 실행 중';
    return {text, explanation:`입력 ${number(u.input_tokens)} · 출력 ${number(u.output_tokens)} · 합계 ${total} 토큰 · 미측정 ${number(u.unmeasured_run_count)}회 · 실행 중 ${number(u.running_run_count)}회`};
  };
  const own = describe(u);
  const explanation = own.explanation + ' · 이 Issue의 직접 실행분이에요. 수집된 대화형 Claude Code 사용량을 포함해요.';
  const ownHtml = `<span class="issue-tokens" title="${esc(explanation)}" aria-label="${esc(own.text + ' · ' + explanation)}">${esc(own.text)}</span>`;
  const tasks = i.task_token_usage;
  if(!tasks || !tasks.task_count || tasks.running_run_count) return ownHtml + (detail ? `<p class="token-explanation">${esc(explanation)}</p>` : '');
  const task = describe(tasks);
  const taskExplanation = task.explanation + ` · 하위 Task ${number(tasks.task_count)}개의 직접 실행분 합계예요.`;
  const taskHtml = `<span class="issue-tokens issue-tokens-tasks" title="${esc(taskExplanation)}" aria-label="${esc('Task 합계 ' + task.text + ' · ' + taskExplanation)}">Task 합계 ${esc(task.text)}</span>`;
  return `<span class="issue-tokens-lines"><span class="issue-tokens-own"><span class="issue-tokens-label">자신</span>${ownHtml}</span>${taskHtml}</span>${detail ? `<p class="token-explanation">${esc(explanation)}<br>${esc(taskExplanation)}</p>` : ''}`;
}
// 점과 상세는 같은 판정과 Auto 설정을 쓴다. HTML을 텍스트로 풀어 속성에 넣는다.
function lcText(html){ const el = document.createElement('span'); el.innerHTML = html; return el.textContent.trim(); }
// 시계(.progress-clock)는 요소째 살려 다음 줄에 두어 tickClocks가 1초마다 센다.
function lifecycleDotsHtml(i){
  const lc = lifecycle(i, actionSession?.settings || {}), msg = lcText(lc.msg);
  const el = document.createElement('span'); el.innerHTML = lc.steps[lc.cur].meta;
  const clock = el.querySelector('.progress-clock'); clock?.remove();
  const meta = el.textContent.trim();
  return `<span class="lc-dots" title="${esc(msg)}" aria-label="${esc(`진행 ${lc.steps[lc.cur].label}: ${msg}`)}">${lc.steps.map(s => `<i data-s="${s.state}"></i>`).join('')}<em>${esc(meta)}</em>${clock ? `<span class="lc-clock">${clock.outerHTML}</span>` : ''}</span>`;
}
function applyTurnFilter(){
  const rows = [...view.querySelectorAll('#list-body tr.row')], turn = listState().turn || 'all';
  for(const row of rows){
    const parent = rows.find(r => r.dataset.ref === row.dataset.parent);
    row.hidden = turn === 'all' ? !!parent && collapsedSet().has(parent.dataset.ref) : row.dataset.o !== (turn === 'mine' ? 'human' : 'agent');
  }
  const count = rows.filter(r => r.dataset.o === 'human').length;
  view.querySelectorAll('[data-turn]').forEach(b => {
    b.setAttribute('aria-pressed', String(b.dataset.turn === turn));
    if(b.dataset.turn === 'mine') b.textContent = `내 차례만 · ${count}${listLoad && !listLoad.done ? '+' : ''}`;
  });
}
function issueRowHtml(i){
  const lc = lifecycle(i, actionSession?.settings || {});
  return `<tr class="row${lc.owner === 'human' ? ' mine' : ''}${i.parent_ref ? ' child' : ''}" data-ref="${esc(i.ref)}" data-o="${lc.owner}" data-id="${i.id}"${i.parent_ref ? ` data-parent="${esc(i.parent_ref)}"` : ''}><td class="ref">${esc(i.ref)}</td>
    <td class="title-cell"><span class="issue-title-line" title="${esc(i.title || '')}">${(i.labels || []).includes('goal') ? '<span class="goal-badge">[Goal]</span> ' : ''}${titleHtml(i)}</span><div class="issue-badges">${i.rating ? `<span class="row-rating" title="평점 ${i.rating}/5" aria-label="평점 ${i.rating}점">★${i.rating}</span>` : ''}${labelsHtml((i.labels || []).filter(l => l !== 'goal'))}${typesHtml(i, false)}</div>${i.parent_id ? '<div class="sub">TASK</div>' : ''}<div class="issue-history-mobile"><span class="issue-meta-agent">${processingAgentsHtml(i)}</span><span class="issue-meta-time">${completionTimeHtml(i)}</span><span class="issue-meta-tokens">${issueTokensHtml(i)}</span></div></td>
    <td class="issue-col-status">${statusHtml(i.status)} ${approvalHtml(i.approval)}${progressHtml(i)}<span class="lc-mobile">${lifecycleDotsHtml(i)}</span></td>
    <td class="issue-col-progress">${lifecycleDotsHtml(i)}</td>
    <td class="hide-m issue-col-agent">${processingAgentsHtml(i)}</td><td class="hide-m issue-col-time">${completionTimeHtml(i)}</td><td class="hide-m token-cell issue-col-tokens">${issueTokensHtml(i)}</td><td class="hide-m dim issue-col-updated">${fmtTime(i.updated_at)}</td><td class="action-cell">${actionHtml(i)}</td></tr>`;
}
// Task는 부모 바로 아래에 들여써서 붙이고(부모가 아직 안 왔으면 오는 순간 끌어온다) 접고 펼 수 있다. 접힘은 부모 ref별로 기억한다.
function collapsedSet(){ return new Set(JSON.parse(localStorage.getItem('dev.collapsed') || '[]')); }
function kidRows(tbody, ref){ return [...tbody.querySelectorAll('tr.row[data-parent]')].filter(r => r.dataset.parent === ref); }
function refreshKids(tbody, parentTr){
  const kids = kidRows(tbody, parentTr.dataset.ref), open = !collapsedSet().has(parentTr.dataset.ref);
  if(!kids.length) return;
  if(!parentTr.querySelector('.tog')){
    parentTr.querySelector('td.ref').insertAdjacentHTML('afterbegin', '<button class="tog" type="button"><i></i></button>');
    parentTr.querySelector('.title-cell').insertAdjacentHTML('beforeend', '<div class="sub kids"></div>');
  }
  const b = parentTr.querySelector('.tog');
  b.setAttribute('aria-expanded', String(open)); b.setAttribute('aria-label', open ? 'Task 접기' : 'Task 펼치기');
  parentTr.querySelector('.kids').textContent = `Tasks ${kids.length}`;
  kids.forEach(k => { k.hidden = !open; });
}
function orderKids(tbody, parentTr){
  const direction = listLoad?.qs.sort === 'id_desc' ? -1 : 1;
  const kids = kidRows(tbody, parentTr.dataset.ref).sort((a, b) => direction * (Number(a.dataset.id) - Number(b.dataset.id)));
  let anchor = parentTr;
  for(const kid of kids){
    anchor.after(kid);
    anchor = orderKids(tbody, kid);
  }
  refreshKids(tbody, parentTr);
  return anchor;
}
function placeRow(tbody, i){
  const tr = document.createElement('tbody'); tr.innerHTML = issueRowHtml(i);
  const row = tr.firstElementChild;
  tbody.append(row);
  const parentTr = i.parent_ref && [...tbody.children].find(r => r.dataset.ref === i.parent_ref);
  orderKids(tbody, parentTr || row);
  applyTurnFilter();
}
let issueSummaryPeriod = 'today', issueSummaryRequest = 0;
const ISSUE_SUMMARY_PERIODS = { today: '오늘', yesterday: '어제', week: '이번 주', last_week: '저번 주', month: '이번 달', last_month: '저번 달', year: '올해', all: '전체' };
function summaryNumber(n){ return Number(n || 0).toLocaleString('ko-KR'); }
function summaryDuration(seconds){
  const minutes = Math.floor(Number(seconds || 0) / 60);
  return minutes >= 60 ? `${summaryNumber(Math.floor(minutes / 60))}시간 ${minutes % 60}분` : `${minutes}분`;
}

// Trend는 서버가 서울 날짜로 나눈 값을 그대로 그린다. 필터는 이 화면에서만 유지한다.
let trendRange = 7, trendDates = null, trendRequest = 0, trendHuman = false;
const TREND_METRICS = [
  ['created_issue_count', '생성 Issue', '개'],
  ['completed_issue_count', '완료 Issue', '개'],
  ['agent_seconds', '에이전트 시간', ''],
  ['total_tokens', '토큰', '개']
];
function seoulToday(){ return new Intl.DateTimeFormat('en-CA', {timeZone:'Asia/Seoul', year:'numeric', month:'2-digit', day:'2-digit'}).format(new Date()); }
function trendDateBefore(today, days){
  const d = new Date(today + 'T00:00:00Z');
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}
function trendValue(key, value){
  if(key === 'total_tokens') return tokenHtml(value);
  if(key !== 'agent_seconds') return summaryNumber(value);
  return Number(value) > 0 && Number(value) < 60 ? `${Math.round(value)}초` : summaryDuration(value);
}
function trendText(key, value){ return key === 'total_tokens' ? formatTokens(value) : String(trendValue(key, value)); }
// 표시 옵션(line·marks·values)과 날짜 정렬은 Trend 화면을 쓰는 동안 유지한다. 차트 시간축은 항상 과거→현재.
let trendMarks = 'values', trendSortAsc = true, trendData = null;
const TREND_AGENTS = {all:'All Agents', claude:'Claude', codex:'Codex'};
let trendAgent = 'all';
let trendIssuesView = false;
let trendStageData = null, trendStageSort = 'total_machine_seconds', trendStageAsc = false;
const STAGE_COLUMNS = [['queue','대기열'], ['review','검토'], ['execute','구현'], ['qa','QA'], ['completion','완료 처리']];
function stageDuration(value){
  if(value == null) return '—';
  const minutes = Math.round(Number(value) / 60);
  return minutes >= 60 ? `${Math.floor(minutes / 60)}시간 ${minutes % 60}분` : `${minutes}분`;
}
function stageDelta(now, before){
  if(now == null || before == null || before === 0) return '';
  const percent = Math.round((now - before) / before * 100);
  return ` <span class="trend-stage-delta" title="이전 기간 대비">${percent > 0 ? '▲' : percent < 0 ? '▼' : '='} ${Math.abs(percent)}%</span>`;
}
function stageDonut(stages, field, label, count){
  const values = stages.map(s => Math.max(0, Number(s[field]?.mean || 0)));
  const total = values.reduce((a, b) => a + b, 0), circumference = 2 * Math.PI * 42;
  let offset = 0;
  const arcs = stages.map((s, i) => {
    const length = total ? values[i] / total * circumference : 0;
    const value = field === 'seconds' ? stageDuration(values[i]) : formatTokens(values[i]);
    const arc = length ? `<circle class="trend-stage-${s.key}" cx="60" cy="60" r="42" fill="none" stroke="var(--stage-color)" stroke-width="16" stroke-dasharray="${length.toFixed(3)} ${(circumference - length).toFixed(3)}" stroke-dashoffset="${(-offset).toFixed(3)}" transform="rotate(-90 60 60)" role="img" aria-label="${esc(s.name)} ${esc(value)}"><title>${esc(s.name)} · ${esc(value)}</title></circle>` : '';
    offset += length; return arc;
  }).join('');
  const legend = stages.map((s, i) => {
    const value = field === 'seconds' ? stageDuration(values[i]) : formatTokens(values[i]);
    const percent = total ? Math.round(values[i] / total * 100) : 0;
    return `<li class="trend-stage-${s.key}"><span class="trend-stage-dot" aria-hidden="true"></span><span class="trend-donut-name">${esc(s.name)}</span><span class="trend-donut-value">${esc(value)} · ${percent}%</span></li>`;
  }).join('');
  return `<div class="trend-donut-layout"><svg class="trend-donut" viewBox="0 0 120 120" role="img" aria-label="${esc(label)}: ${esc(count ? field === 'seconds' ? stageDuration(total) : formatTokens(total) : '측정 없음')}"><circle cx="60" cy="60" r="42" fill="none" stroke="var(--border)" stroke-width="16"/>${arcs}<text x="60" y="60" dominant-baseline="middle" text-anchor="middle">${esc(count ? field === 'seconds' ? stageDuration(total) : formatTokens(total) : '측정 없음')}</text></svg><ul class="trend-donut-legend" aria-label="${esc(label)} 단계별 평균">${legend}</ul></div>`;
}
function stageCell(row, key){
  const seconds = row.stages?.[key]?.seconds;
  const retry = key === 'execute' && row.execute_runs > 1 ? ` <span class="dim">(${row.execute_runs}회)</span>` : '';
  return `<td>${stageDuration(seconds)}${retry}</td>`;
}
function stageIssueRow(row, task = false){
  const partial = row.partial ? `<span title="${esc(row.partial_reason || '일부 실행이 측정되지 않았어요')}">≈ </span>` : '';
  const detail = !task && row.tasks?.length ? `<button type="button" class="trend-expand" data-stage-expand="${esc(row.ref)}" aria-expanded="false" aria-label="${esc(row.ref)} Task 펼치기">Task</button>` : '';
  return `<tr class="${task ? 'trend-task-row' : 'trend-issue-row'}"${task ? ` data-stage-parent="${esc(row.parent)}" hidden` : ''}><th scope="row">${detail}<a href="#/issue/${encodeURIComponent(row.ref)}"><span class="ref">${esc(row.ref)}</span> <span class="trend-row-title" title="${esc(row.title)}">${esc(row.title)}</span></a></th><td>${task ? '—' : esc(row.project)}</td><td>${task ? '—' : esc(row.done_at?.slice(0, 16).replace('T', ' ') || '—')}</td><td>${task ? '—' : row.task_count}</td>${STAGE_COLUMNS.map(([key]) => stageCell(row, key)).join('')}<td>${partial}${stageDuration(row.total_machine_seconds)}</td>${trendIssuesView ? stageBar(row) : ''}${trendHuman ? `<td>${task ? '—' : stageDuration(row.stages?.plan_wait?.seconds)}</td><td>${task ? '—' : stageDuration(row.stages?.done_wait?.seconds)}</td>` : ''}<td>${partial}${tokenHtml(row.total_tokens)}</td></tr>`;
}
function stageRows(){
  const data = trendStageData;
  const rows = [...data.issues].sort((a, b) => {
    const value = r => ['ref', 'project', 'done_at', 'task_count', 'total_tokens', 'total_machine_seconds'].includes(trendStageSort) ? r[trendStageSort] : r.stages?.[trendStageSort]?.seconds;
    const x = value(a), y = value(b), compare = typeof x === 'string' ? x.localeCompare(y) : (x || 0) - (y || 0);
    return (trendStageAsc ? compare : -compare) || a.ref.localeCompare(b.ref);
  });
  return rows.map(row => stageIssueRow(row) + (row.tasks || []).map(task => stageIssueRow({...task, parent:row.ref}, true)).join('')).join('');
}
function paintStageTable(root){
  const table = root.querySelector('.trend-stage-table'); if(!table || !trendStageData) return;
  table.querySelector('tbody').innerHTML = stageRows();
  table.querySelectorAll('th[aria-sort]').forEach(th => th.removeAttribute('aria-sort'));
  table.querySelector(`[data-stage-sort="${trendStageSort}"]`)?.closest('th').setAttribute('aria-sort', trendStageAsc ? 'ascending' : 'descending');
}
// 일별 중앙값 차트 — 시간(seconds)·토큰(tokens) 공용. 모든 날짜에 연한 세로선, dev 병합일은 점선으로 강조한다.
function stageDailyChart(data, series = 'seconds'){
  const tokens = series === 'tokens', what = tokens ? '토큰' : '시간';
  const days = (tokens ? data.daily_task_tokens_median : data.daily_task_median) || [], measured = days.filter(day => day[series] != null);
  const format = tokens ? formatTokens : stageDuration;
  if(!days.length || (!measured.length && !(data.dev_changes || []).length)) return `<div class="empty">이 기간에 전체 ${what}을 측정한 Task가 없어요.</div>`;
  const width = 480, left = 48, right = 20, top = 22, bottom = 34, height = 200;
  const x = index => left + (days.length === 1 ? .5 : index / (days.length - 1)) * (width - left - right);
  const maximum = Math.max(...measured.map(day => day[series]), tokens ? 1 : 60);
  const y = value => top + (1 - value / maximum) * (height - top - bottom);
  const points = days.map((day, index) => day[series] == null ? null : [x(index), y(day[series]), day]).filter(Boolean);
  const runs = []; let run = [];
  days.forEach((day, index) => { if(day[series] == null){ if(run.length) runs.push(run); run = []; } else run.push([x(index), y(day[series])]); });
  if(run.length) runs.push(run);
  const base = height - bottom, dayStep = Math.ceil(days.length / 6);
  const grid = days.map((day, index) => `<line class="trend-stage-day" x1="${x(index)}" x2="${x(index)}" y1="${top}" y2="${base}"/>`).join('');
  const hlines = [0, .5, 1].map(ratio => `<line class="trend-stage-hline" x1="${left}" x2="${width-right}" y1="${y(maximum * ratio)}" y2="${y(maximum * ratio)}"/><text x="${left-6}" y="${y(maximum * ratio) + 4}" text-anchor="end">${esc(format(Math.round(maximum * ratio)))}</text>`).join('');
  const xlabels = days.map((day, index) => index % dayStep === 0 || index === days.length - 1 ? `<text x="${x(index)}" y="${height-10}" text-anchor="${index === 0 ? 'start' : index === days.length - 1 ? 'end' : 'middle'}">${esc(day.date.slice(5))}</text>` : '').join('');
  const areas = runs.filter(r => r.length > 1).map(r => `<polygon class="trend-stage-area" points="${r.map(p => p.map(v => v.toFixed(1)).join(',')).join(' ')} ${r.at(-1)[0].toFixed(1)},${base} ${r[0][0].toFixed(1)},${base}"/>`).join('');
  const changes = (data.dev_changes || []).map(change => {
    const index = days.findIndex(day => day.date === change.date);
    return index < 0 ? null : {...change, index};
  }).filter(Boolean);
  const marks = [...new Set(changes.map(change => change.index))].map(index => {
    const refs = changes.filter(change => change.index === index).map(change => change.ref);
    return `<line class="trend-dev-mark" x1="${x(index)}" x2="${x(index)}" y1="${top}" y2="${height-bottom}"><title>${esc(days[index].date)} · dev 병합 ${esc(refs.join(', '))}</title></line>`;
  }).join('');
  return `<svg class="trend-stage-daily-chart" data-series="${series}" viewBox="0 0 ${width} ${height}" role="img" aria-label="일별 Task 전체 ${what} 중앙값과 dev 병합일">
    ${grid}${hlines}${marks}${areas}
    ${runs.map(r => r.length > 1 ? `<polyline class="trend-stage-line" points="${r.map(p => p.map(v => v.toFixed(1)).join(',')).join(' ')}"/>` : '').join('')}
    ${points.map(([px, py, day]) => `<circle class="trend-stage-point" cx="${px}" cy="${py}" r="4" aria-label="${esc(day.date)} 중앙값 ${esc(format(day[series]))}, ${day.n}건"><title>${esc(day.date)} · 중앙 ${esc(format(day[series]))} · ${day.n}건</title></circle><text class="trend-stage-value" x="${px}" y="${Math.max(10, py - 9)}" text-anchor="middle">${esc(format(day[series]))}</text>`).join('')}
    ${xlabels}</svg>${measured.length ? '' : `<p class="dim">이 기간에 전체 ${what}을 측정한 Task가 없어요.</p>`}`;
}
// 분포 히스토그램 — 구간(bins)마다 세로 막대, 위에 건수, 아래에 구간 시작값. series: seconds | tokens
function stageDistChart(dist, series){
  const bins = dist?.bins || [], format = series === 'tokens' ? formatTokens : stageDuration;
  const what = series === 'tokens' ? '토큰' : '시간';
  if(!bins.length) return '<div class="empty">이 기간에 측정된 Issue가 없어요.</div>';
  const width = 480, left = 12, right = 12, top = 22, bottom = 34, height = 200, slot = (width - left - right) / bins.length;
  const maximum = Math.max(1, ...bins.map(b => b.count)), base = height - bottom;
  const bars = bins.map((b, i) => {
    const h = (b.count / maximum) * (base - top), x = left + i * slot, label = `${format(b.from)} ~ ${format(b.to)}`;
    return `<rect class="trend-dist-bar" x="${(x + 3).toFixed(1)}" y="${(base - h).toFixed(1)}" width="${(slot - 6).toFixed(1)}" height="${h.toFixed(1)}" role="img" aria-label="${esc(label)} ${b.count}건"><title>${esc(label)} · ${b.count}건</title></rect>
      <text class="trend-dist-count" x="${(x + slot / 2).toFixed(1)}" y="${Math.max(10, base - h - 5).toFixed(1)}" text-anchor="middle">${b.count}</text>
      <text x="${x.toFixed(1)}" y="${base + 14}" text-anchor="start">${esc(format(b.from))}</text>`;
  }).join('');
  return `<svg class="trend-dist-chart" data-series="${series}" viewBox="0 0 ${width} ${height}" role="img" aria-label="Issue 소요 ${what} 분포">
    <line class="trend-stage-hline" x1="${left}" x2="${width - right}" y1="${base}" y2="${base}"/>${bars}</svg>`;
}
function stageResults(data){
  const visible = data.stages.filter(s => trendHuman || s.kind === 'machine');
  const label = s => `<li class="trend-stage-${s.key}"><span class="trend-stage-dot" aria-hidden="true"></span><div><strong>${esc(s.name)}</strong><span>시간 중앙 ${stageDuration(s.seconds.median)}${stageDelta(s.seconds.median, s.previous.seconds.median)}</span>${s.tokens ? `<span>토큰 중앙 ${formatTokens(s.tokens.median)}${stageDelta(s.tokens.median, s.previous.tokens.median)}</span>` : ''}${s.key === 'queue' ? `<span>그중 빈 대기열 대기 평균 ${stageDuration(data.queue_idle_seconds.mean)}</span>` : ''}</div></li>`;
  return `<p class="trend-context">${data.project ? esc(data.project) : '전체 프로젝트'} · ${esc(TREND_AGENTS[data.agent] || TREND_AGENTS.all)} · ${esc(data.from_date)} ~ ${esc(data.to_date)} · 이전 기간 ${esc(data.previous.from_date)} ~ ${esc(data.previous.to_date)}</p>
    <label class="trend-human-toggle"><input id="trend-human" type="checkbox" ${trendHuman ? 'checked' : ''}> 사람 대기 포함</label>
    ${data.partial_count ? `<p class="trend-partial" role="status">부분 측정 ${data.partial_count}건 · 평균과 중앙값에서 제외해요.</p>` : ''}
    <div class="trend-stage-grid"><section class="trend-stage-card"><h2>평균 시간 구성</h2>${stageDonut(visible, 'seconds', '평균 시간 구성', data.issues.length - data.partial_count)}</section><section class="trend-stage-card"><h2>평균 토큰 구성</h2>${stageDonut(visible.filter(s => s.tokens), 'tokens', '평균 토큰 구성', data.issues.length - data.partial_count)}</section></div>
    <div class="trend-stage-grid trend-dist-grid"><section class="trend-stage-card"><h2>소요 시간 분포</h2><p class="dim">대상 ${data.distribution?.seconds?.n || 0}건</p>${stageDistChart(data.distribution?.seconds, 'seconds')}</section><section class="trend-stage-card"><h2>소요 토큰 분포</h2><p class="dim">대상 ${data.distribution?.tokens?.n || 0}건</p>${stageDistChart(data.distribution?.tokens, 'tokens')}</section></div>
    ${data.agent && data.agent !== 'all' ? '<p class="dim">한 Issue를 두 에이전트가 나눠 했으면 각자 맡은 몫만 집계돼요.</p>' : ''}
    <details class="trend-stage-details"><summary>단계별 중앙값과 이전 기간 비교</summary><ul class="trend-stage-labels">${visible.map(label).join('')}</ul></details>
    <section class="trend-stage-section trend-stage-history"><h2>Task 전체 중앙값</h2><p class="dim">구현 대기열 등록부터 병합 완료까지 · 병합일 기준 · 진한 점선은 dev 변경이에요.${data.daily_task_unmeasured ? ` 시작 기록이 없는 Task ${data.daily_task_unmeasured}건은 제외했어요.` : ''}</p>
      <div class="trend-stage-daily-grid"><div><h3>시간 중앙값</h3>${stageDailyChart(data, 'seconds')}</div><div><h3>토큰 중앙값</h3>${stageDailyChart(data, 'tokens')}</div></div>
      <details class="trend-dev-changes"><summary>이 기간에 병합된 dev 변경 ${data.dev_changes?.length || 0}건</summary>${data.dev_changes?.length ? `<ul>${data.dev_changes.map(change => `<li><a href="#/issue/${encodeURIComponent(change.ref)}">${esc(change.ref)} · ${esc(change.title)}</a><time datetime="${esc(change.merged_at)}">${esc(change.merged_at_local || '')}</time></li>`).join('')}</ul>` : '<p class="dim">병합된 dev 변경이 없어요.</p>'}</details></section>
    <p class="trend-note">${esc(data.note)}</p>`;
}
// 이슈별 소요 탭 본문 — 단계별 소요와 같은 Issue 표(Task 펼치기·정렬)에 단계 구성 막대 열을 더한다.
function stageIssuesResults(data){
  return `<p class="trend-context">${data.project ? esc(data.project) : '전체 프로젝트'} · ${esc(TREND_AGENTS[data.agent] || TREND_AGENTS.all)} · ${esc(data.from_date)} ~ ${esc(data.to_date)} · ${data.issues.length}건</p>
    <label class="trend-human-toggle"><input id="trend-human" type="checkbox" ${trendHuman ? 'checked' : ''}> 사람 대기 포함</label>
    ${data.partial_count ? `<p class="trend-partial" role="status">부분 측정 ${data.partial_count}건 · 표에 ≈로 표시해요.</p>` : ''}
    ${data.agent && data.agent !== 'all' ? '<p class="dim">한 Issue를 두 에이전트가 나눠 했으면 각자 맡은 몫만 집계돼요.</p>' : ''}
    <section class="trend-stage-section"><h2>이슈별 소요</h2>${data.issues.length ? `<div class="trend-stage-scroll"><table class="trend-stage-table"><thead><tr><th><button data-stage-sort="ref" type="button">Issue</button></th><th><button data-stage-sort="project" type="button">프로젝트</button></th><th><button data-stage-sort="done_at" type="button">완료 시각</button></th><th><button data-stage-sort="task_count" type="button">Task</button></th>${STAGE_COLUMNS.map(([key,name]) => `<th><button data-stage-sort="${key}" type="button">${name}</button></th>`).join('')}<th aria-sort="${trendStageAsc ? 'ascending' : 'descending'}"><button data-stage-sort="total_machine_seconds" type="button">기계 합계</button></th><th>구성</th>${trendHuman ? '<th><button data-stage-sort="plan_wait" type="button">계획 결정 대기</button></th><th><button data-stage-sort="done_wait" type="button">완료 확인 대기</button></th>' : ''}<th><button data-stage-sort="total_tokens" type="button">토큰 합계</button></th></tr></thead><tbody>${stageRows()}</tbody></table></div>` : '<div class="empty">이 기간에 완료된 Issue가 없어요. 기간이나 프로젝트를 바꿔 보세요.</div>'}</section><p class="trend-note">${esc(data.note)}</p>`;
}
function stageBar(row){
  const parts = STAGE_COLUMNS.map(([key, name]) => [key, name, Math.max(0, Number(row.stages?.[key]?.seconds || 0))]);
  const total = parts.reduce((sum, p) => sum + p[2], 0);
  if(!total) return '<td>—</td>';
  return `<td><span class="trend-stage-bar" role="img" aria-label="${esc(parts.filter(p => p[2]).map(p => `${p[1]} ${stageDuration(p[2])}`).join(', '))}">${parts.filter(p => p[2]).map(([key, name, s]) => `<i class="trend-stage-${key}" style="width:${(s / total * 100).toFixed(1)}%" title="${esc(name)} ${esc(stageDuration(s))}"></i>`).join('')}</span></td>`;
}
// 종류별 사용량을 같은 날짜의 합계로 쌓아 표시한다.
function trendKindChart(days, key, label){
  const kinds = [['issue','이슈 작업']];
  const field = key === 'total_tokens' ? 'tokens' : 'agent_seconds';
  const width = Math.max(580, days.length * (trendMarks === 'values' ? 52 : 18)), height = 130;
  const slot = width / days.length, max = Math.max(1, ...days.map(d => kinds.reduce((v,[k]) => v + Number(d.by_kind?.[k]?.[field] || 0), 0)));
  const bars = days.map((d,i) => {
    let sum = 0;
    return kinds.map(([k,name]) => {
      const value = Number(d.by_kind?.[k]?.[field] || 0), h = value / max * 110;
      sum += h;
      return `<rect class="trend-kind-${k}" data-kind="${k}" x="${i * slot + slot * .2}" y="${122 - sum}" width="${slot * .6}" height="${h}" role="img" aria-label="${esc(d.date)} ${esc(name)} ${esc(trendText(key,value))}"><title>${esc(d.date)} · ${esc(name)} ${esc(trendText(key,value))}</title></rect>`;
    }).join('') + (trendMarks === 'values' ? `<text x="${(i + .5) * slot}" y="${Math.max(12, 116 - sum)}" text-anchor="middle">${esc(trendText(key, d[key]))}</text>` : '');
  }).join('');
  return `<div class="trend-kind-legend">${kinds.map(([k,name]) => `<span class="trend-kind-${k}">${esc(name)}</span>`).join('')}</div><div class="trend-scroll"><svg class="trend-chart" viewBox="0 0 ${width} ${height}"${width > 580 ? ` style="width:${width}px;max-width:none"` : ''} role="img" aria-label="${esc(label)} 종류별 일별 사용량">${bars}</svg></div><div class="trend-axis"><span>${esc(days[0].date)}</span><span>${esc(days.at(-1).date)}</span></div>`;
}
function trendChart(days, key, label){
  if(['total_tokens', 'agent_seconds'].includes(key) && days.some(d => d.by_kind)) return trendKindChart(days, key, label);
  const values = days.map(d => Number(d[key] || 0)), n = values.length;
  const max = Math.max(1, ...values), height = 130, marks = trendMarks !== 'line';
  const step = trendMarks === 'values' ? 52 : 18, width = marks ? Math.max(580, n * step) : 580, pad = marks ? 14 : 0;
  const xy = values.map((v, i) => [(n > 1 ? pad + i * (width - 2 * pad) / (n - 1) : (marks ? width / 2 : 0)), height - 8 - v * (height - 20) / max]);
  const points = xy.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
  const dots = marks ? xy.map(([x, y], i) => `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3.5" fill="currentColor" role="img" aria-label="${esc(days[i].date)} ${esc(label)} ${esc(trendText(key, values[i]))}"><title>${esc(days[i].date)} · ${esc(trendText(key, values[i]))}</title></circle>`).join('') : '';
  const texts = trendMarks === 'values' ? xy.map(([x, y], i) => `<text x="${x.toFixed(1)}" y="${Math.max(12, y - 8).toFixed(1)}" text-anchor="middle" aria-hidden="true">${esc(trendText(key, values[i]))}</text>`).join('') : '';
  const wide = width > 580 ? ` style="width:${width}px;max-width:none"` : '';
  return `<div class="trend-scroll"><svg class="trend-chart" viewBox="0 0 ${width} 130"${wide} role="img" aria-label="${esc(label)} 일별 추세, ${esc(days[0].date)}부터 ${esc(days.at(-1).date)}까지"><polyline points="${points}" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>${dots}${texts}</svg></div>
    <div class="trend-axis"><span>${esc(days[0].date)}</span><span>${esc(days.at(-1).date)}</span></div>`;
}
function paintTrendCharts(root){
  if(!trendData) return;
  root.querySelectorAll('[data-trend-chart]').forEach(el => {
    const [, label] = TREND_METRICS.find(m => m[0] === el.dataset.trendChart);
    el.innerHTML = trendChart(trendData.days, el.dataset.trendChart, label);
  });
}
function trendRows(){
  return (trendSortAsc ? trendData.days : [...trendData.days].reverse()).map(day => `<tr><th scope="row">${esc(day.date)}</th>${TREND_METRICS.map(([key]) => `<td>${trendValue(key, day[key])}</td>`).join('')}</tr>`).join('');
}
function paintTrendTable(root){
  if(!trendData) return;
  root.querySelector('.trend-daily tbody').innerHTML = trendRows();
  root.querySelector('.trend-sort').closest('th').setAttribute('aria-sort', trendSortAsc ? 'ascending' : 'descending');
  root.querySelector('.trend-sort-mark').textContent = trendSortAsc ? '▲' : '▼';
}
async function renderTrends(tab = ''){
  // 이슈별 소요는 단계별 소요와 같은 API·표를 쓰고 도넛·분포·일별 그래프만 뺀다.
  trendIssuesView = tab === 'issues';
  const stageTab = tab === 'stages' || trendIssuesView;
  const today = seoulToday();
  if(!view.querySelector('.trend-page')){
    trendRange = 7;
    trendDates = [trendDateBefore(today, 6), today];
    trendMarks = 'values';
    localStorage.setItem('dev.project', '');
    paintProjectFilter();
  }
  view.innerHTML = `<div class="trend-page"><header class="trend-heading"><div><p class="eyebrow">OVERVIEW</p><h1>Trend</h1><p class="dim">${trendIssuesView ? '완료된 Issue별 단계 시간과 토큰을 표로 살펴봐요.' : tab === 'stages' ? '완료된 Issue의 단계별 시간과 토큰을 살펴봐요.' : '보존된 기록의 기간별 사용 현황을 서울 날짜로 살펴봐요.'}</p></div></header>
    <nav class="trend-subtabs" aria-label="Trend 하위 메뉴"><a href="#/trends" ${tab ? '' : 'aria-current="page"'}>기간별 사용</a><a href="#/trends/stages" ${tab === 'stages' ? 'aria-current="page"' : ''}>단계별 소요</a><a href="#/trends/issues" ${trendIssuesView ? 'aria-current="page"' : ''}>이슈별 소요</a></nav>
    <form id="trend-filter" class="trend-filter"><label>프로젝트<select id="trend-project">${projectOptionsHtml()}</select></label>
      ${stageTab ? `<label>에이전트<select id="trend-agent">${Object.entries(TREND_AGENTS).map(([k, v]) => `<option value="${k}">${v}</option>`).join('')}</select></label>` : '<label>차트 표시<select id="trend-marks"><option value="line">추세선</option><option value="marks">마크</option><option value="values">마크 및 수치</option></select></label>'}
      <div class="trend-periods" role="group" aria-label="빠른 기간">${[7,30,90].map(n => `<button type="button" data-trend-days="${n}" aria-pressed="${trendRange === n}">최근 ${n}일</button>`).join('')}</div>
      <label>시작일<input id="trend-from" type="date" required value="${esc(trendDates[0])}"></label>
      <label>종료일<input id="trend-to" type="date" required value="${esc(trendDates[1])}"></label>
      <button type="submit" class="primary">조회</button></form>
    <p id="trend-validation" class="error" role="alert" hidden></p><div id="trend-results" aria-live="polite"></div></div>`;
  const root = view.querySelector('.trend-page'), form = root.querySelector('form');
  root.querySelector('#trend-project').value = currentProject();
  if(stageTab){ root.querySelector('#trend-agent').value = trendAgent;
    root.querySelector('#trend-agent').addEventListener('change', e => { trendAgent = e.target.value; loadTrend(); }); }
  if(!tab){ root.querySelector('#trend-marks').value = trendMarks;
    root.querySelector('#trend-marks').addEventListener('change', e => { trendMarks = e.target.value; paintTrendCharts(root); }); }
  root.querySelector('#trend-project').addEventListener('change', e => {
    localStorage.setItem('dev.project', e.target.value); paintProjectFilter(); loadTrend();
  });
  root.querySelectorAll('[data-trend-days]').forEach(button => button.addEventListener('click', () => {
    trendRange = Number(button.dataset.trendDays); trendDates = [trendDateBefore(seoulToday(), trendRange - 1), seoulToday()];
    form.elements['trend-from'].value = trendDates[0]; form.elements['trend-to'].value = trendDates[1];
    root.querySelectorAll('[data-trend-days]').forEach(b => b.setAttribute('aria-pressed', String(b === button)));
    loadTrend();
  }));
  form.addEventListener('submit', e => {
    e.preventDefault(); trendRange = 0;
    root.querySelectorAll('[data-trend-days]').forEach(b => b.setAttribute('aria-pressed', 'false'));
    trendDates = [form.elements['trend-from'].value, form.elements['trend-to'].value]; loadTrend();
  });
  root.addEventListener('click', e => {
    if(e.target.closest('#trend-retry')) loadTrend();
    if(e.target.closest('.trend-sort')){ trendSortAsc = !trendSortAsc; paintTrendTable(root); }
    const sort = e.target.closest('[data-stage-sort]');
    if(sort){ trendStageAsc = trendStageSort === sort.dataset.stageSort ? !trendStageAsc : false; trendStageSort = sort.dataset.stageSort; paintStageTable(root); }
    const expand = e.target.closest('[data-stage-expand]');
    if(expand){ const open = expand.getAttribute('aria-expanded') !== 'true'; expand.setAttribute('aria-expanded', String(open)); expand.setAttribute('aria-label', `${expand.dataset.stageExpand} Task ${open ? '접기' : '펼치기'}`); root.querySelectorAll('[data-stage-parent]').forEach(row => { if(row.dataset.stageParent === expand.dataset.stageExpand) row.hidden = !open; }); }
  });
  root.addEventListener('change', e => { if(e.target.id === 'trend-human'){ trendHuman = e.target.checked; root.querySelector('#trend-results').innerHTML = trendIssuesView ? stageIssuesResults(trendStageData) : stageResults(trendStageData); paintStageTable(root); } });
  await loadTrend();
}
// 토큰 카드 보조 줄 — 구독 사용분을 공개 API 단가로 어림한 값(요약에 키가 없으면 그리지 않는다)
function trendCostNote(s){
  if(s.estimated_api_cost_usd == null) return '';
  const cost = Number(s.estimated_api_cost_usd) || 0, unpriced = Number(s.unpriced_tokens) || 0;
  const tip = '구독 사용분을 공개 API 요금으로 어림한 값이에요. 실제 청구액이 아니에요.';
  const money = cost >= 100 ? Math.round(cost) : cost.toFixed(2);
  return `<small class="trend-cost dim" title="${tip}">API 환산 약 $${money}${s.cost_approx_run_count ? ' (어림)' : ''}${unpriced > 0 ? ` · 환산 못 한 토큰 ${unpriced.toLocaleString('ko-KR')}개` : ''}</small>`;
}
async function loadTrend(){
  const root = view.querySelector('.trend-page'); if(!root) return;
  const [from, to] = [root.querySelector('#trend-from').value, root.querySelector('#trend-to').value];
  const error = root.querySelector('#trend-validation'), results = root.querySelector('#trend-results');
  const days = from && to ? Math.round((Date.parse(to + 'T00:00:00Z') - Date.parse(from + 'T00:00:00Z')) / 86400000) + 1 : 0;
  error.hidden = true;
  if(!days || days < 1 || days > 366){
    ++trendRequest; results.innerHTML = ''; error.textContent = '시작일과 종료일을 선택하고 1~366일 범위로 지정해 주세요.'; error.hidden = false; return;
  }
  const request = ++trendRequest, project = currentProject();
  const current = () => request === trendRequest && root === view.querySelector('.trend-page') && project === currentProject();
  results.setAttribute('aria-busy', 'true');
  results.innerHTML = '<div class="usage-loading" role="status">Trend를 불러오는 중이에요…<div class="usage-skeleton" aria-hidden="true"></div></div>';
  try{
    const stages = ['#/trends/stages', '#/trends/issues'].includes(location.hash.split('?')[0]);
    const data = await api('GET', (stages ? '/api/trends/stages?' : '/api/trends?') + new URLSearchParams({from, to, ...(project ? {project} : {}), ...(stages ? {human:trendHuman ? '1' : '0', agent:trendAgent} : {})}), undefined, current, {notifyErrors:false});
    if(!current()) return;
    if(stages){ trendStageData = data; results.innerHTML = trendIssuesView ? stageIssuesResults(data) : stageResults(data); paintStageTable(root); return; }
    trendData = data;
    const s = data.summary, noActivity = !data.days.some(d => TREND_METRICS.some(([key]) => Number(d[key] || 0) > 0));
    results.innerHTML = `<p class="trend-context">${project ? esc(project) : '전체 프로젝트'} · ${esc(data.from_date)} ~ ${esc(data.to_date)} · ${esc(new Date(data.as_of).toLocaleString('ko-KR', {timeZone:'Asia/Seoul'}))} 조회</p>
      ${s.partial ? `<p class="trend-partial" role="status">부분 집계 · 토큰 미계측 실행 ${summaryNumber(s.unmeasured_run_count)}건, 시간 미계측 실행 ${summaryNumber(s.unmeasured_duration_run_count)}건, 진행 중 ${summaryNumber(s.running_run_count)}건</p>` : ''}
      ${noActivity ? '<div class="empty">이 기간에는 기록된 활동이 없어요. 기간이나 프로젝트를 바꿔 보세요.</div>' : `<div class="trend-grid">${TREND_METRICS.map(([key,label,unit]) => `<section class="trend-card" aria-label="${label}"><h2>${label}</h2><strong>${trendValue(key, s[key])}</strong><span class="dim">${unit ? unit + ' · ' : ''}기간 합계</span>${key === 'total_tokens' ? trendCostNote(s) : ''}<div data-trend-chart="${key}">${trendChart(data.days, key, label)}</div></section>`).join('')}</div>`}
      <section class="trend-ranking"><h2>토큰 상위 작업</h2><p class="dim">선택 기간의 Issue와 Task 직접 실행 토큰을 순위에 포함해요.</p>
      ${data.top_issues.length ? `<ol>${data.top_issues.map(item => `<li><a href="#/issue/${encodeURIComponent(item.ref)}"><span class="ref">${esc(item.ref)}</span> ${esc(item.title)}</a><strong>${tokenHtml(item.total_tokens)}</strong></li>`).join('')}</ol>` : '<div class="empty">이 기간에 측정된 작업 토큰이 없어요.</div>'}</section>
      <details class="trend-daily"><summary>일별 값 표로 보기</summary><div class="trend-table-wrap"><table><thead><tr><th aria-sort="${trendSortAsc ? 'ascending' : 'descending'}"><button type="button" class="trend-sort" aria-label="날짜 정렬 바꾸기">날짜 <span class="trend-sort-mark" aria-hidden="true">${trendSortAsc ? '▲' : '▼'}</span></button></th>${TREND_METRICS.map(([,label]) => `<th>${label}</th>`).join('')}</tr></thead><tbody>${trendRows()}</tbody></table></div></details>
      <p class="dim trend-note">${esc(data.note)} 완료 Issue는 현재 Done 상태와 마지막 완료일을 기준으로 해요. 진행 중 실행은 조회 시각까지 포함해요. 미계측 값과 삭제된 기록은 합계에 포함되지 않아요.</p>`;
  }catch(e){ if(current()) results.innerHTML = `<div class="empty" role="alert">Trend를 불러오지 못했어요. ${esc(e.message)} <button type="button" id="trend-retry">다시 불러오기</button></div>`; }
  finally{ if(current()) results.setAttribute('aria-busy', 'false'); }
}
async function loadIssueSummary(){
  const box = view.querySelector('#issue-summary');
  if(!box) return;
  const request = ++issueSummaryRequest, project = currentProject(), period = issueSummaryPeriod;
  const current = () => request === issueSummaryRequest && box === view.querySelector('#issue-summary') &&
    currentProject() === project && issueSummaryPeriod === period;
  box.querySelectorAll('[data-summary-period]').forEach(button =>
    button.setAttribute('aria-pressed', String(button.dataset.summaryPeriod === period)));
  const content = box.querySelector('.issue-summary-content');
  content.setAttribute('aria-busy', 'true');
  content.innerHTML = '<span class="dim">현황을 불러오는 중이에요…</span>';
  try{
    const data = await api('GET', '/api/issue-summary?' + new URLSearchParams({period, ...(project ? {project} : {})}),
      undefined, current, {notifyErrors: false});
    if(!current()) return;
    const s = data.summary;
    content.innerHTML = `<div class="issue-summary-values">
      <div><span>생성 Issue</span><strong>${summaryNumber(s.created_issue_count)}</strong></div>
      <div><span>완료 Issue</span><strong>${summaryNumber(s.completed_issue_count)}</strong></div>
      <div><span>에이전트 시간</span><strong>${summaryDuration(s.agent_seconds)}</strong></div>
      <div><span>토큰</span><strong>${tokenHtml(s.total_tokens)}</strong></div>
    </div>`;
  }catch(e){
    if(current()) content.innerHTML = `<span class="issue-summary-error">현황을 불러오지 못했어요. ${esc(e.message)}</span> <button type="button" class="issue-summary-retry">다시 불러오기</button>`;
  }finally{ if(current()) content.setAttribute('aria-busy', 'false'); }
}
async function renderList(){
  const st = listState();
  view.innerHTML = `
    <div class="toolbar">
      <select id="list-project" title="프로젝트별로 보기">${projectOptionsHtml()}</select>
      <input class="grow" id="q" placeholder="제목·본문 검색" value="${esc(st.q)}">
      <div class="chips">${STATUSES.map(s => `<span class="chip${st.statuses.includes(s) ? ' on' : ''}" data-st="${s}">${STATUS_LABEL[s]}</span>`).join('')}</div>
      <span class="checks"><label class="dim"><input type="checkbox" id="show-closed" ${st.closed ? 'checked' : ''}> 끝난 것도</label>
      <label class="dim"><input type="checkbox" id="only-approved" ${st.approved ? 'checked' : ''}> 승인된 것만</label></span>
    </div>
    <div class="ops-split"><section id="issue-summary" class="issue-summary" aria-label="Issue 처리 현황">
      <div class="issue-summary-head"><span class="issue-summary-title">처리 현황</span><div class="issue-summary-periods" role="group" aria-label="집계 기간">
        ${Object.entries(ISSUE_SUMMARY_PERIODS).map(([key, label]) => `<button type="button" data-summary-period="${key}" aria-pressed="${key === issueSummaryPeriod}">${label}</button>`).join('')}
      </div></div><div class="issue-summary-content" role="status" aria-live="polite"></div></section><div class="ops-main"><div class="turn-filter" role="group" aria-label="목록 필터">${[['all','전체'],['mine','내 차례만 · 0'],['agent','에이전트 작업 중']].map(([v,t]) => `<button type="button" data-turn="${v}" aria-pressed="${(st.turn || 'all') === v}">${t}</button>`).join('')}</div><div id="list-body"></div><div id="list-more" class="list-more"></div></div><aside id="jobs-box" class="ops-side" aria-label="대기열"></aside></div>`;
  view.querySelector('#issue-summary').addEventListener('click', e => {
    const period = e.target.closest('[data-summary-period]');
    if(period){ issueSummaryPeriod = period.dataset.summaryPeriod; loadIssueSummary(); }
    else if(e.target.closest('.issue-summary-retry')) loadIssueSummary();
  });
  view.querySelector('.turn-filter').addEventListener('click', e => {
    const b = e.target.closest('[data-turn]'); if(!b) return;
    localStorage.setItem('dev.list', JSON.stringify({...listState(), turn:b.dataset.turn}));
    applyTurnFilter();
    if(listLoad && !listLoad.done && listLoad.more.getBoundingClientRect().top < innerHeight + 400) loadMoreIssues();
  });
  loadIssueSummary();
  loadJobs().catch(() => {});
  const save = (patch) => { localStorage.setItem('dev.list', JSON.stringify({ ...listState(), ...patch })); loadList(); };
  view.querySelector('#q').addEventListener('input', (e) => {
    clearTimeout(listSearchTimer);
    listSearchTimer = setTimeout(() => save({ q: e.target.value }), 300);
  });
  view.querySelectorAll('[data-st]').forEach(ch => ch.addEventListener('click', () => {
    const s = ch.dataset.st, cur = listState().statuses;
    ch.classList.toggle('on', !cur.includes(s));
    save({ statuses: cur.includes(s) ? cur.filter(x => x !== s) : [...cur, s] });
  }));
  view.querySelector('#show-closed').addEventListener('change', (e) => save({ closed: e.target.checked }));
  view.querySelector('#only-approved').addEventListener('change', (e) => save({ approved: e.target.checked }));
  // 프로젝트 필터(DEV-11) — 탭 줄의 전역 필터와 같은 값(dev.project)을 쓴다. 여기서 바꾸면 그쪽도 따라간다.
  view.querySelector('#list-project').addEventListener('change', (e) => {
    localStorage.setItem('dev.project', e.target.value); projectSel.value = e.target.value; loadList(); loadIssueSummary();
  });
  view.querySelector('#list-body').addEventListener('click', (e) => {
    if(e.target.closest('#list-id-sort')){
      const sort = listState().sort;
      save({ sort: sort === 'id_asc' ? 'id_desc' : sort === 'id_desc' ? '' : 'id_asc' });
      return;
    }
    if(e.target.closest('[data-action]')) return;
    const tog = e.target.closest('.tog');
    if(tog){
      const tr = tog.closest('tr.row'), set = collapsedSet();
      set.has(tr.dataset.ref) ? set.delete(tr.dataset.ref) : set.add(tr.dataset.ref);
      localStorage.setItem('dev.collapsed', JSON.stringify([...set]));
      refreshKids(tr.parentElement, tr);
      applyTurnFilter();
      return;
    }
    const r = e.target.closest('tr.row');
    if(r) location.hash = `#/issue/${r.dataset.ref}`;
  });
  const sentinel = view.querySelector('#list-more');
  listObserver = new IntersectionObserver((entries) => {
    if(sentinel === view.querySelector('#list-more') && entries.some(en => en.isIntersecting)) loadMoreIssues();
  }, { rootMargin: '400px' });
  listObserver.observe(sentinel);
  await loadList();
  const body = view.querySelector('#list-body');
  followProgress(async () => {
    const L = listLoad;
    if(!L || L.busy || pendingActions.size) return;
    const data = await api('GET', '/api/issues?' + new URLSearchParams({...L.qs, limit: Math.max(L.offset, LIST_PAGE), offset: 0}));
    if(L !== listLoad || body !== view.querySelector('#list-body')) return;
    await loadActionSettings(L.actions, data.issues);
    if(L !== listLoad) return;
    const tbody = body.querySelector('tbody');
    if(tbody){
      closeProgress();
      tbody.innerHTML = '';
      data.issues.forEach(i => placeRow(tbody, i));
      bindActions(tbody, L.actions, loadList);   // 새로 그린 Action 버튼에 핸들러를 다시 붙인다(DEV-97)
      L.seen = new Set(data.issues.map(i => i.ref));
      L.offset = data.issues.length; L.done = !data.has_more;
      applyTurnFilter();
      if(!L.done && L.more.getBoundingClientRect().top < innerHeight + 400) loadMoreIssues();
    }else if(data.issues.length) await loadList();
    const queued = await loadJobs();
    if(L !== listLoad || body !== view.querySelector('#list-body')) return;
    if(!data.issues.some(activeProgress) && !queued) stopLiveRefresh();
  }, () => body === view.querySelector('#list-body'));
}
async function loadList(){
  const body = view.querySelector('#list-body'), more = view.querySelector('#list-more');
  if(!body || !more || location.hash.replace(/^#\/?/, '').split('?')[0]) return;
  if(listLoad){ clearTimeout(listLoad.timer); listLoad.loading?.remove(); }
  const st = listState();
  const statuses = st.statuses.length ? st.statuses : (st.closed ? [] : OPEN_STATUSES);
  listLoad = { body, more, qs: { project: currentProject(), status: statuses.join(','), q: st.q, ...(['id_asc', 'id_desc'].includes(st.sort) ? { sort: st.sort } : {}), ...(st.approved ? { approved: 'true' } : {}) }, offset: 0, done: false, busy: false, seen: new Set() };
  listLoad.actions = beginActions(() => listLoad === L && body === view.querySelector('#list-body'));
  const L = listLoad;
  body.innerHTML = ''; more.textContent = '';
  await loadMoreIssues();
}
async function loadMoreIssues(){
  const L = listLoad, body = view.querySelector('#list-body'), more = view.querySelector('#list-more');
  if(!L || L.done || L.busy || !body || body !== L.body || more !== L.more) return;
  const current = () => L === listLoad && body === view.querySelector('#list-body') && more === view.querySelector('#list-more');
  L.busy = true;
  body.setAttribute('aria-busy', 'true');
  L.timer = setTimeout(() => {
    if(!current()) return;
    L.loading = document.createElement('div');
    L.loading.className = 'list-loading'; L.loading.setAttribute('role', 'status');
    L.loading.innerHTML = '<svg class="ico list-spinner" aria-hidden="true"><use href="#i-loader-circle"/></svg><span>Issue를 불러오는 중이에요</span>';
    (L.offset ? more : body).append(L.loading);
  }, 300);
  const finish = () => {
    clearTimeout(L.timer); L.loading?.remove(); L.busy = false;
    if(current()) body.setAttribute('aria-busy', 'false');
  };
  let data;
  try{ data = await api('GET', '/api/issues?' + new URLSearchParams({ ...L.qs, limit: LIST_PAGE, offset: L.offset }), undefined, current); }
  catch(e){
    finish();
    if(!current()) return;
    L.done = true;   // 실패 후 교차 관찰로 자동 요청이 반복되지 않게 한다.
    (L.offset ? more : body).innerHTML = '<div class="list-error" role="status">Issue를 불러오지 못했어요. 새로고침해 보세요.</div>';
    return;
  }
  if(!current()){ finish(); return; }   // 필터 변경과 탭 이동 후의 응답은 버린다.
  // 고친 순 정렬이라 사이에 바뀐 이슈가 두 번 올 수 있다 — 이미 그린 것은 건너뛴다.
  await loadActionSettings(L.actions, data.issues);
  finish();   // 설정 조회까지 끝나야 행을 그릴 수 있으므로 그동안 로딩을 유지한다.
  if(!current()) return;
  const items = data.issues.filter(i => !L.seen.has(i.ref));
  items.forEach(i => L.seen.add(i.ref));
  L.offset += data.issues.length;
  L.done = !data.has_more;   // 예전 서버(has_more 없음)면 한 번으로 끝
  if(!body.querySelector('table, .empty')){
    body.innerHTML = items.length ? `<table class="issues"><thead><tr><th aria-sort="${L.qs.sort === 'id_asc' ? 'ascending' : L.qs.sort === 'id_desc' ? 'descending' : 'none'}"><button type="button" id="list-id-sort" class="list-sort" title="${L.qs.sort === 'id_asc' ? 'ID 내림차순으로 정렬해요' : L.qs.sort === 'id_desc' ? '최근 고친 순으로 정렬해요' : 'ID 오름차순으로 정렬해요'}">ID${L.qs.sort ? `<svg class="ico${L.qs.sort === 'id_desc' ? ' sort-desc' : ''}" aria-hidden="true"><use href="#i-arrow-up"/></svg>` : ''}</button></th><th>Title</th><th>Status</th><th class="issue-col-progress">진행</th>
      <th class="hide-m history-heading issue-col-agent">AGENT</th><th class="hide-m history-heading issue-col-time">TIME</th><th class="hide-m issue-col-tokens">TOKENS</th><th class="hide-m issue-col-updated">Updated</th><th>Action</th></tr></thead><tbody></tbody></table>` : '<div class="empty">이슈가 없어요.</div>';
  }
  const tbody = body.querySelector('tbody');
  if(tbody){ items.forEach(i => placeRow(tbody, i)); bindActions(tbody, L.actions, loadList); }
  applyTurnFilter();
  more.textContent = '';
  // 한 화면이 다 안 찼으면 바로 다음 묶음
  if(!L.done && more.getBoundingClientRect().top < window.innerHeight + 400) loadMoreIssues();
}

// ---- 칸반 ----
let cleanupBoard = () => {};
function enableBoardPan(board){
  const events = new AbortController();
  let pan = null;
  const excluded = '.card, h3 .status, h3 .ref, a, button, input, textarea, select, [contenteditable], [role="button"]';
  function stop(){
    const id = pan?.id;
    pan = null;
    board.classList.remove('panning');
    if(id != null && board.hasPointerCapture(id)) board.releasePointerCapture(id);
  }
  const on = (type, fn) => board.addEventListener(type, fn, { signal: events.signal });
  on('pointerdown', e => {
    if(e.pointerType !== 'mouse' || e.button !== 0 || e.target.closest(excluded)) return;
    pan = { id: e.pointerId, x: e.clientX, left: board.scrollLeft, active: false };
    board.setPointerCapture(e.pointerId);
    board.focus({ preventScroll: true });
  });
  on('pointermove', e => {
    if(!pan || pan.id !== e.pointerId) return;
    if(!(e.buttons & 1)){ stop(); return; }
    const dx = e.clientX - pan.x;
    if(!pan.active && Math.abs(dx) < 5) return;
    pan.active = true;
    board.classList.add('panning');
    e.preventDefault();
    board.scrollLeft = pan.left - dx;
  });
  for(const type of ['pointerup', 'pointercancel', 'lostpointercapture']) on(type, stop);
  on('keydown', e => {
    if(e.target !== board || !['ArrowLeft', 'ArrowRight'].includes(e.key)) return;
    e.preventDefault();
    board.scrollLeft += (e.key === 'ArrowRight' ? 1 : -1) * 272;
  });
  cleanupBoard = () => { stop(); events.abort(); cleanupBoard = () => {}; };
}
async function renderBoard(){
  cleanupBoard();
  const key = currentProject(), hash = location.hash;
  const session = beginActions(() => currentProject() === key && location.hash === hash);
  const cols = STATUSES.filter(s => s !== 'closed');
  const items = (await api('GET', '/api/issues?' + new URLSearchParams({ project: currentProject(), status: cols.join(',') }))).issues;
  await loadActionSettings(session, items);
  if(actionSession !== session || !session.current()) return;
  view.innerHTML = `<div class="ops-split"><div class="ops-main"><div class="kanban" tabindex="0" role="region" aria-label="Issue 보드 — 빈 영역을 끌거나 좌우 방향키로 이동해요">${cols.map(s => {
    const mine = items.filter(i => i.status === s);
    return `<div class="col" data-col="${s}"><h3>${statusHtml(s)}<span class="ref">${mine.length}</span></h3><div class="cards">
      ${mine.map(i => `<div class="card" draggable="true" data-ref="${esc(i.ref)}"><div class="ref">${esc(i.ref)}${i.parent_id ? ' · Task' : ''}</div>
        <div class="t">${titleHtml(i)}</div><div class="meta">${approvalHtml(i.approval)}${labelsHtml(i.labels)}${typesHtml(i)}
        ${processingAgentsHtml(i)}${completionTimeHtml(i)}${issueTokensHtml(i)}${clockHtml(i)}${lifecycleDotsHtml(i)}</div>${actionHtml(i)}</div>`).join('')}
    </div></div>`;
  }).join('')}</div></div><aside id="jobs-box" class="ops-side" aria-label="대기열"></aside></div>`;
  loadJobs().catch(() => {});
  bindActions(view.querySelector('.kanban'), session, renderBoard);
  enableBoardPan(view.querySelector('.kanban'));
  if(items.some(i => i.status === 'in_progress')) followProgress(renderBoard, () => location.hash === hash);
  view.querySelectorAll('.card').forEach(card => {
    card.addEventListener('click', () => { location.hash = `#/issue/${card.dataset.ref}`; });
    card.addEventListener('dragstart', (e) => { e.dataTransfer.setData('text/plain', card.dataset.ref); });
  });
  view.querySelectorAll('.col').forEach(col => {
    col.addEventListener('dragover', (e) => { e.preventDefault(); col.classList.add('drop'); });
    col.addEventListener('dragleave', () => col.classList.remove('drop'));
    col.addEventListener('drop', async (e) => {
      e.preventDefault(); col.classList.remove('drop');
      const ref = e.dataTransfer.getData('text/plain');
      if(ref){ await api('POST', `/api/issues/${ref}/status`, { status: col.dataset.col }).catch(() => {}); renderBoard(); }
    });
  });
}

// ---- 이슈 상세 ----
function attachmentsHtml(items){
  if(!items?.length) return '';
  return `<section class="panel attachments"><h2>Attachments</h2><p class="dim">미디어 재생과 에이전트의 자료 이해는 브라우저와 사용 도구에 따라 달라요.</p>${items.map(a => {
    const url = `/api/attachments/${encodeURIComponent(a.id)}/content`;
    if(a.kind === 'url') return `<article class="attachment"><a href="${esc(/^https?:\/\//i.test(a.url) ? a.url : '#')}" target="_blank" rel="noopener noreferrer">${esc(a.url)}</a></article>`;
    const preview = a.kind === 'image' ? `<img src="${url}" alt="${esc(a.name)}" loading="lazy">` :
      ['video','audio'].includes(a.kind) ? `<${a.kind} src="${url}" controls preload="metadata" aria-label="${esc(a.name)}"></${a.kind}><p class="dim">재생이 안 되면 다운로드해 주세요.</p>` :
      `<details data-text-url="${url}"><summary>텍스트 미리보기 (최대 64KiB)</summary><pre role="status"></pre></details>`;
    return `<article class="attachment"><div class="attachment-heading"><span>${esc(a.name)} · ${Math.ceil(a.size / 1024)} KiB</span><a href="${url}" download>다운로드</a></div>${preview}</article>`;
  }).join('')}</section>`;
}
function bindAttachmentPreviews(){
  view.querySelectorAll('[data-text-url]').forEach(el => el.addEventListener('toggle', async () => {
    if(!el.open || el.dataset.loading) return;
    el.dataset.loading = 'true';
    const pre = el.querySelector('pre'); pre.textContent = '불러오는 중…';
    try{
      const r = await fetch(el.dataset.textUrl, {headers:{Range:'bytes=0-65535'}});
      if(!r.ok) throw new Error();
      pre.textContent = await r.text();
      if(r.status === 206) pre.append(document.createTextNode('\n[최대 64KiB 미리보기예요.]'));
    }catch{ pre.textContent = '미리보기를 불러오지 못했어요. 접었다 펼쳐 다시 시도해 주세요.'; delete el.dataset.loading; }
  }));
}

// 댓글·재검토 메모에 붙은 첨부 이름(DEV-242) — 내용은 위 Attachments 패널에 있다
let eventAttachments = new Map();
function eventFilesHtml(ids){
  const items = (ids || []).map(id => eventAttachments.get(id)).filter(Boolean);
  return items.length ? `<div class="event-files">${items.map(a => `<a href="${a.kind === 'url' ? esc(/^https?:\/\//i.test(a.url) ? a.url : '#') : `/api/attachments/${encodeURIComponent(a.id)}/content`}"${a.kind === 'url' ? ' target="_blank" rel="noopener noreferrer"' : ' download'}><svg class="ico" aria-hidden="true"><use href="#i-paperclip"/></svg>${esc(a.kind === 'url' ? a.url : a.name)}</a>`).join('')}</div>` : '';
}
function eventHtml(e){
  const who = actorHtml(e.actor, e.data && e.data.model);
  const when = `<span class="when" title="${esc(e.created_at)}">${fmtTime(e.created_at)}</span>`;
  const d = e.data || {};
  let what;
  if(e.kind === 'comment') return `<li class="comment">${who}${when}${md(e.body)}${eventFilesHtml(d.attachment_ids)}</li>`;
  if(e.kind === 'status') what = `상태 ${statusHtml(d.from)} → ${statusHtml(d.to)}`;
  else if(e.kind === 'plan') what = `Plan v${esc(d.version)}을(를) 올렸어요`;
  else if(e.kind === 'commit') what = `커밋 <code>${esc(String(d.sha).slice(0, 7))}</code>${d.repo ? ` (${esc(d.repo)})` : ''} ${esc(e.body)}`;
  else if(e.kind === 'claim') what = d.action === 'release' ? '작업을 놓았어요' : `작업을 잡았어요(${esc(d.minutes)}분)`;
  else if(e.kind === 'assign') what = d.agent_id ? `담당: ${esc(actorName('agent:' + d.agent_id))}` : '담당을 비웠어요';
  else if(e.kind === 'edit') what = `고침: ${esc((d.fields || []).join(', '))}`;
  else what = esc(e.kind);
  return `<li class="sys">${who}${when} · ${what}${e.kind === 'status' && e.body ? md(e.body) : ''}${e.kind === 'status' ? eventFilesHtml(d.attachment_ids) : ''}</li>`;
}
// ---- 계획서 결정: 승인 · 메모 붙여 승인 · 메모 붙여 재검토 · 거절 ----
const VERDICT_LABEL = { approve: '승인됨', approve_notes: '조건부 승인됨', reject: '거절됨' };
const VERDICT_COLOR = { approve: 'done', approve_notes: 'in_review', reject: 'changes_requested' };
function decisionQuestions(body){   // 계획서의 "정해야 할 것" 절의 목록 항목만
  const m = /^#{1,6}[ \t]*(?:사람이 )?정해야 할 것.*$/m.exec(body);
  if(!m) return '';
  const rest = body.slice(m.index + m[0].length), next = rest.search(/^#{1,6}[ \t]/m);
  return (next < 0 ? rest : rest.slice(0, next)).split('\n').filter(l => /^\s*(\d+[.)]|[-*])\s/.test(l)).map(l => `> ${l.trim()}\n→ `).join('\n\n');
}
// ---- 실행 기록(DEV-29) ----
const RUN_STATUS = { running: ['in_progress', '도는 중'], ok: ['done', '완료'], failed: ['changes_requested', '실패'], timeout: ['in_progress', '시간 초과'], orphaned: ['on_hold', '끊김'] };
const PROVIDER_NAME = { claude: 'Claude', codex: 'Codex' };
const RUN_MODE = { review: '검토', execute: '실행' };
function runsHtml(runs){
  if(!runs.length) return '<div class="dim hint">아직 실행 기록이 없어요.</div>';
  return `<ul class="runs">${runs.map(r => {
    const [color, label] = RUN_STATUS[r.status] || ['closed', r.status];
    const sec = r.ended_at ? Math.round((new Date(r.ended_at) - new Date(r.started_at)) / 1000) : null;
    const dur = sec == null ? '' : sec >= 60 ? `${Math.floor(sec / 60)}분 ${sec % 60}초` : `${sec}초`;
    const tok = r.input_tokens == null && r.output_tokens == null ? '' : `토큰 ${tokenHtml(r.input_tokens)} → ${tokenHtml(r.output_tokens)}${r.cost_usd != null ? ` · $${r.cost_usd.toFixed(2)}` : ''}`;
    return `<li><div><span class="status" style="--sc:var(--s-${color})">${label}</span> ${PROVIDER_NAME[r.provider] || 'Claude'} ${RUN_MODE[r.mode] || r.mode}${r.model ? `<span class="model">${esc(r.model)}</span>` : ''} <span class="dim">${fmtTime(r.started_at)}</span></div>
      <div class="dim">${[dur, tok].filter(Boolean).join(' · ')}</div>
      ${r.note ? `<div class="dim">${esc(r.note)}</div>` : ''}${r.log_file ? `<div class="dim"><code>${esc(r.log_file)}</code></div>` : ''}</li>`;
  }).join('')}</ul>`;
}
// ---- 대기열(DEV-43): 바쁠 때 누른 것은 줄에 서고 하나씩 차례로 ----
function delegateLine(it, provider){
  const d = it.delegates?.[provider];
  if(!d) return '';
  return `모델 ${d.model ? `${d.model_name || d.model} (${d.model})` : 'CLI 기본값'}${d.agent ? ` · Agent "${d.agent}"` : ''}\n`;
}
function reviewConfirmText(it, provider){
  const timeout = it.delegates?.review_timeout_sec;
  return `${delegateLine(it, provider)}계획서만 올리고 코드는 고치지 않아요${timeout == null ? '' : ` · 최대 ${Math.floor(timeout / 60)}분`}\n${it.review_busy ? '앞 작업이 끝나면 시작해요' : '바로 시작해요'}`;
}
const QUEUE_LINE = '다른 일이 돌고 있으면 끝난 뒤 차례로 시작해요.';
function jobHtml(j, edges){   // 이 이슈가 줄에 있으면 버튼 자리에 "대기 n번째 · 취소"
  return `<div class="queued"><span class="status" style="--sc:var(--s-waiting)">Waiting · ${PROVIDER_NAME[j.provider] || 'Claude'} 대기 ${j.position}번째</span>
    ${moveJobHtml(j, edges)}<button class="ghost cancel-job" data-job="${j.id}">취소</button></div>${j.note ? `<div class="dim hint">${esc(j.note)}</div>` : ''}`;
}
// 화면에서 본 이웃과 양 끝을 함께 보내 그 사이 줄이 바뀌었으면 서버가 409로 거절한다.
function moveJobHtml(j, edges){
  const first = edges?.first ?? (j.prev_id == null ? j.id : null);
  const last = edges?.last ?? (j.next_id == null ? j.id : null);
  return [['top', first, 'arrow-up-to-line', '맨 위로', j.prev_id != null],
    ['up', j.prev_id, 'arrow-up', '앞으로 한 칸', j.prev_id != null],
    ['down', j.next_id, 'arrow-down', '뒤로 한 칸', j.next_id != null],
    ['bottom', last, 'arrow-down-to-line', '맨 아래로', j.next_id != null]].map(([dir, nb, ic, word, enabled]) =>
    `<button class="ghost move-job" data-job="${j.id}" data-dir="${dir}" data-neighbor="${nb ?? ''}"${!enabled || nb == null ? ' disabled' : ''} title="${esc(j.ref)} ${word}" aria-label="${esc(j.ref)} 대기 순서 ${word}"><svg class="ico" aria-hidden="true"><use href="#i-${ic}"/></svg></button>`).join('');
}
let movingJob = false;
async function moveJob(b){
  if(movingJob) return;   // 요청 중 다른 화살표도 막는다 — 응답 전 순서로 또 옮기면 409만 난다
  movingJob = true;
  const { job, dir, neighbor } = b.dataset, inList = !!b.closest('#jobs-box');
  view.querySelectorAll('.move-job').forEach(x => x.disabled = true);
  try{ await api('POST', `/api/jobs/${job}/move`, { direction: dir, neighbor_id: neighbor ? +neighbor : null }); }
  catch(e){ /* api()가 알림을 띄웠다 — 최신 순서를 다시 그린다 */ }
  finally{ movingJob = false; }
  await (inList ? loadJobs() : route()).catch(() => {});
  const q = d => view.querySelector(`.move-job[data-job="${job}"][data-dir="${d}"]:not([disabled])`);
  (q(dir) || q(dir === 'top' || dir === 'up' ? 'down' : 'up'))?.focus();
}
function queuedMsg(r, started){ return r && r.queued ? `대기열에 넣었어요 — ${r.position}번째` : started; }
async function loadJobs(){   // 목록·Board 위의 대기열(DEV-91) — 늘 펼친 상자: 위는 In Progress, 아래는 대기 항목. 항목 수를 돌려준다
  const box = view.querySelector('#jobs-box');
  if(!box) return 0;
  const { jobs, in_progress: prog = [] } = await restartJobsSnapshot();
  if(box !== view.querySelector('#jobs-box')) return 0;
  const edges = {first: jobs[0]?.id, last: jobs.at(-1)?.id};
  const progHtml = prog.map(p => `<li><span class="status" style="--sc:var(--s-in_progress)">In Progress</span> <a href="#/issue/${esc(p.ref)}">${esc(p.ref)}</a> <span class="job-title">${esc(p.title)}</span>
    <span class="dim">${p.started_at ? `${PROVIDER_NAME[p.provider] || 'Claude'} ${RUN_MODE[p.mode] || esc(p.mode)} 중 · ${fmtTime(p.started_at)}` : ''}</span></li>`).join('');
  const waitHtml = jobs.map(j => `<li><span class="ref">${j.position}</span> <a href="#/issue/${esc(j.ref)}">${esc(j.ref)}</a> ${PROVIDER_NAME[j.provider] || 'Claude'} ${RUN_MODE[j.mode] || esc(j.mode)}
      ${j.note ? `<span class="dim">${esc(j.note)}</span>` : ''}<span class="job-btns">${moveJobHtml(j, edges)}<button class="ghost cancel-job" data-job="${j.id}">취소</button></span></li>`).join('');
  box.innerHTML = `<section class="jobs" aria-label="대기열"><div class="jobs-head">대기열 · In Progress ${prog.length} · Waiting ${jobs.length}</div>
    ${prog.length || jobs.length ? `<div class="jobs-list">${prog.length ? `<ul class="jobs-prog">${progHtml}</ul>` : ''}${jobs.length ? `<ul class="jobs-wait">${waitHtml}</ul>` : ''}</div>` : '<div class="dim jobs-empty">대기 중인 일이 없어요.</div>'}</section>`;
  return prog.length + jobs.length;
}
view.addEventListener('click', (e) => {
  const m = e.target.closest('.move-job');
  if(m){ e.stopPropagation(); moveJob(m); return; }
  const b = e.target.closest('.cancel-job');
  if(!b) return;
  e.stopPropagation();
  whileBusy(b, async () => { await api('DELETE', `/api/jobs/${b.dataset.job}`); toast('대기를 취소했어요'); route(); });
});
// ---- Task 실행(DEV-32): 격리된 worktree에서 구현, 결과는 브랜치로 ----
// 선행 대기는 서버 문구로 가린다 — panel이 대기/불가를 따로 주면 그걸로
function waitOnly(blocked){ return !!blocked && blocked.startsWith('선행 Task'); }
const pendingExecutions = new Map();
function executeHtml(it, liveMode, liveProvider, edges){
  const x = it.execute, running = it.review_running && liveMode === 'execute';
  const job = it.job && it.job.mode === 'execute' ? it.job : null;
  const provider = pendingExecutions.get(it.ref) || (running ? liveProvider || 'claude' : null);
  const stopped = x.blocked && !waitOnly(x.blocked);   // 기다려도 못 누르면 버튼 대신 사유만
  const hint = x.blocked ? esc(x.blocked) + (waitOnly(x.blocked) ? ' 눌러 두면 선행이 끝난 뒤 차례로 시작해요.' : '')
    : it.review_busy && !running ? `다른 이슈에서 Agent가 일하는 중이에요 — ${QUEUE_LINE}` : '';
  const b = x.branch;
  const buttons = `<div class="agent-btns" id="exec-btns"><button id="ask-execute" title="격리된 worktree에서 구현해요 — Claude 비용 상한 $${x.budget_usd}."><svg class="ico brand-icon" aria-hidden="true"><use href="#i-claude"/></svg>Claude에게 실행 맡기기</button>
    <button id="ask-codex-execute" title="Codex: 시간 제한 ${Math.floor(x.timeout_sec / 60)}분 · 비용 상한 없음."><svg class="ico brand-icon" aria-hidden="true"><use href="#i-openai"/></svg>Codex에게 실행 맡기기</button></div>`;
  const merge = (b ? `<div class="branch"><code>${esc(b.branch)}</code> <span class="dim">커밋 ${b.commits}개</span>
      ${b.diff_stat ? `<pre>${esc(b.diff_stat)}</pre>` : ''}</div>` : '');
  return `<div class="exec"><div class="agent-sub">실행</div>${job ? jobHtml(job, edges) : provider ? busyLine(provider, '실행') : `${stopped ? '' : buttons}${hint ? `<div class="dim hint">${hint}</div>` : ''}`}
    ${merge ? `<div class="agent-sub">병합</div>${merge}` : ''}</div>`;
}
// 검토·실행이 도는 동안 버튼 자리에 보이는 상태 줄(DEV-113) — 누를 수 없는 버튼은 그리지 않는다
function busyLine(provider, word){
  return `<div class="agent-busy" role="status">${provider ? `<svg class="ico brand-icon" aria-hidden="true"><use href="#i-${provider === 'codex' ? 'openai' : 'claude'}"/></svg>${PROVIDER_NAME[provider]} ` : ''}${word} 중…</div>`;
}
function decisionHtml(it){
  const a = it.approval;
  const state = !a ? '<span class="dim">아직 결정하지 않았어요.</span>'
    : a.stale ? `<span class="status" style="--sc:var(--s-in_progress)">v${a.plan_version} 결정은 무효</span> <span class="dim">${a.revision_requested ? '— 재검토 요청으로 기존 승인을 보류했어요.' : `— 새 계획서(v${it.plan.version})가 올라왔어요. 다시 결정해 주세요.`}</span>`
    : `<span class="status" style="--sc:var(--s-${VERDICT_COLOR[a.verdict]})">${VERDICT_LABEL[a.verdict]} · v${a.plan_version}</span> <span class="dim">${esc(actorName(a.actor))} · ${fmtTime(a.created_at)}</span>${a.note ? md(a.note) : ''}`;
  const busy = it.review_running || it.job?.mode === 'review';
  if(['done','closed'].includes(it.status)) return `<div class="decision"><div class="decision-state">${state}</div></div>`;
  return `<div class="decision" id="decision"><div class="decision-state">${state}</div>
    ${busy ? '<div class="dim hint">검토가 도는 중이라 끝나면 결정할 수 있어요.</div>' : `
    ${it.plan.revision_note ? '<p class="dim hint">이 계획서는 재검토를 요청했어요. 새 계획서가 올라온 뒤 승인해 주세요.</p>' : ''}
    <div id="decision-form" hidden><textarea id="decision-note" aria-label="계획서 결정 또는 재검토 메모"></textarea>
      <div id="decision-attach" hidden><div id="decision-files" class="cmp-files"></div><button type="button" id="decision-attach-btn" class="secondary">파일 첨부</button><input id="decision-file" type="file" multiple hidden accept=".png,.jpg,.jpeg,.gif,.webp,.mp4,.webm,.mp3,.wav,.ogg,.m4a,.md,.json"></div>
      <div id="revision-provider-row" hidden><label for="revision-provider">검토 Agent</label> <select id="revision-provider"><option value="claude">Claude</option><option value="codex">Codex</option></select><p class="dim hint">메모를 반영해 계획서를 다시 작성해요. 코드는 수정하지 않으며 선택한 Agent 계정의 사용량을 사용해요.</p></div>
      <div class="row-end"><button id="decision-cancel">취소</button><button id="decision-send" class="primary"></button></div></div>
    <div class="review-actions" id="decision-actions">
      ${a && !a.stale ? '' : `<button data-verdict="approve"${it.status === 'in_review' ? '' : ' class="primary"'}${it.plan.revision_note ? ' disabled' : ''}><svg class="ico" aria-hidden="true"><use href="#i-check"/></svg><span>계획 승인</span></button>
      <button data-verdict="approve_notes"${it.plan.revision_note ? ' disabled' : ''}><svg class="ico" aria-hidden="true"><use href="#i-clipboard-check"/></svg><span>메모 붙여 승인</span></button>`}
      ${it.parent_ref ? '' : '<button data-verdict="review"><svg class="ico" aria-hidden="true"><use href="#i-pencil"/></svg><span>메모 붙여 재검토</span></button>'}
      ${a && !a.stale ? '' : '<button data-verdict="reject" class="danger"><svg class="ico" aria-hidden="true"><use href="#i-x"/></svg><span>계획 거절</span></button>'}</div>`}</div>`;
}
// ---- 결과 결정(DEV-16): in_review에서 승인 → Done · 수정 요청 → Changes Requested · 거절 → Closed ----
const RESULT_FORM = {
  changes_requested: { placeholder: '무엇을 더 해야 하나요? (에이전트가 읽는 사람의 지시예요)', send: '수정 요청', cls: 'primary' },
  closed: { placeholder: '거절 이유 (예: 방향이 달라서 접기로 함)', send: '거절하고 닫기', cls: 'danger' },
};
function resultHtml(it){
  if(it.status !== 'in_review') return '';
  return `<div class="panel" id="panel-result"><h2>Result <span class="meta">결과를 확인해 주세요</span></h2>
    <div class="decision" id="result"><div id="result-form" hidden><textarea id="result-note"></textarea>
      <div id="result-attach" hidden><div class="cmp-files" id="result-files"></div>
        <button type="button" id="result-attach-btn">이미지 첨부</button>
        <input type="file" id="result-file" accept="image/png,image/jpeg,image/gif,image/webp" multiple hidden></div>
      <div class="row-end"><button id="result-cancel">취소</button><button id="result-send"></button></div></div>
    <div class="review-actions" id="result-actions"><button id="approve" class="primary" ${it.execute?.result_approval_blocked ? 'disabled title="후속 검사·병합·운영 반영 확인이 끝나야 승인할 수 있어요."' : ''}><svg class="ico" aria-hidden="true"><use href="#i-check-check"/></svg><span>결과 승인</span></button>
      ${it.children.length || it.parent_ref ? '<button class="complete-tree"><svg class="ico" aria-hidden="true"><use href="#i-list-checks"/></svg><span>전체 완료</span></button>' : ''}
      <button data-to="changes_requested" id="request-changes"><svg class="ico" aria-hidden="true"><use href="#i-pencil"/></svg><span>수정 요청</span></button>
      <button data-to="closed" class="danger"><svg class="ico" aria-hidden="true"><use href="#i-x"/></svg><span>결과 거절</span></button></div></div></div>`;
}
// ---- 이슈 주기 5단계(DEV-240) — 판정은 lifecycle 하나, 스테퍼·다음 할 일 줄·목록 점이 모두 이 결과를 쓴다 ----
const LC_NAMES = ['발행', '계획', '실행', '검사', '완료'];
const LC_PANELS = ['panel-description', 'panel-plan', 'panel-tasks', 'panel-result', 'panel-result'];
function lcDate(t, time = true){
  if(!t) return '';
  const d = new Date(t), p = n => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}/${p(d.getDate())}${time ? ` ${p(d.getHours())}:${p(d.getMinutes())}` : ''}`;
}
function lcDuration(sec){
  const m = Math.round(Math.max(0, sec) / 60), h = Math.floor(m / 60);
  return h ? `${h}시간 ${m % 60}분` : `${m}분`;
}
function lcClock(sec){
  return sec == null ? '' : ` <span class="progress-clock" data-elapsed="${sec}" data-at="${performance.now()}">${fmtClock(sec)}</span>`;
}
// 실패 검사 label에서 도구 이름과 파일 경로를 뽑는다
function lcCheckTool(label){ return /pytest/.test(label) ? 'pytest' : /node/.test(label) ? 'node' : '검사'; }
function lcCheckTarget(label){ return (String(label).match(/[\w./-]+\.(?:py|js|mjs|ts|css|html)\b/) || [label])[0]; }
// 반환: {steps:[{key,label,state,meta,bars?,loop?}×5], cur, owner, msg, go?}. auto는 Auto 설정(없으면 꺼짐으로 본다).
function lifecycle(it, auto = {}){
  const f = it.lifecycle_facts || {}, c = it.action_context || {}, a = it.approval, x = it.execute, plan = it.plan || (it.action_context?.plan_version ? {version:it.action_context.plan_version, revision_note:a?.revision_requested} : null);
  const task = !!it.parent_ref, tasks = f.tasks || [], total = tasks.length;
  const doneN = tasks.filter(t => t.status === 'done').length;
  const pv = plan?.version || a?.plan_version, ok = a && !a.stale && a.verdict !== 'reject';
  const run = f.running || (it.review_running ? {mode: x ? 'execute' : 'review', provider: 'claude'} : null);
  const prov = PROVIDER_NAME[run?.provider] || 'Claude';
  const s = ['todo', 'todo', 'todo', 'todo', 'todo'], m = ['', '', '', '', ''];
  let cur = 0, owner = 'human', msg = '', go = null;
  const at = (i, state, meta) => {
    for(let k = 0; k < i; k++) s[k] = 'done';
    m[0] = lcDate(it.created_at);
    if(i > 1 && pv) m[1] = `v${pv} 승인`;
    s[i] = state; if(meta != null) m[i] = meta; cur = i;
  };
  const count = (n = doneN) => total ? `${n}/${total}` : '';
  const ownerBy = on => on ? 'agent' : 'human';
  // 목업에 없는 상황의 문장 — 옛 stageHtml 문장을 그대로 쓴다
  const oldMsg = () => {
    if(x || task) return x?.blocked ? `실행 대기 — ${esc(x.blocked)}` : '실행할 수 있어요 → Claude나 Codex에게 실행을 맡겨 주세요.';
    if(!plan) return '계획서가 없어요 → Claude나 Codex에게 검토를 맡겨 계획서를 받아 보세요.';
    if(plan.revision_note) return '계획서 재검토 요청 → 새 계획서를 기다려 주세요. 검토가 실패했으면 메모 붙여 재검토로 다시 맡겨 주세요.';
    if(!ok) return '계획서 승인 대기 → 계획서 아래에서 승인·메모 붙여 승인·메모 붙여 재검토·거절을 골라 주세요.';
    const next = (it.children || []).find(c => !['done', 'closed', 'in_review'].includes(c.status));
    return next ? `계획 승인됨 → <a href="#/issue/${esc(next.ref)}">${esc(next.ref)}</a>에서 실행 맡기기` : total ? '모든 Task가 실행됐어요 — 결과를 확인해 주세요.' : '계획 승인됨.';
  };
  const mergeWait = x ? x.retry_merge_available || x.manual_merge_confirmable || x.manual_merge_available
    : it.status === 'on_hold' && it.merge_state && !LIVE_MERGE.has(it.merge_state) && it.merge_state !== '병합됨';
  if(it.status === 'done'){
    at(4, 'done', `${lcDate(it.last_done_at || it.closed_at || it.updated_at, false)}${it.completion_seconds != null ? ` · ${lcDuration(it.completion_seconds)}` : ''}`);
    m[2] = `${count(total)} 병합됨`.trim(); m[3] = '검사 통과'; owner = 'none';
    msg = it.completion_seconds != null ? `끝난 이슈예요. 발행부터 완료까지 ${lcDuration(it.completion_seconds)} 걸렸어요.` : '끝난 이슈예요.';
  }else if(it.status === 'closed'){
    const rejected = a?.verdict === 'reject', stop = rejected ? 1 : total || task ? 2 : 0;
    at(stop, 'fail', rejected ? `<b>v${a.plan_version || pv} 거절</b>` : '<b>닫힘</b>');
    if(stop < 2) m[1] = rejected ? m[1] : '';
    for(let k = stop + 1; k < 5; k++) s[k] = 'skip';
    owner = 'none';
    msg = rejected ? `계획서를 거절해 닫았어요.${f.note ? ` 사유: ${esc(f.note)}` : ''}` : '닫힌 이슈예요.';
  }else if(LIVE_MERGE.has(it.merge_state)){
    at(3, 'agent', `<b>${esc(it.merge_state)}</b>`); m[2] = count(); owner = 'agent';
    msg = `${esc(it.merge_state)} — 운영 반영 확인이 끝나면 결과를 확인할 수 있어요.`;
  }else if(run?.mode === 'review'){
    at(1, 'agent', `<b>${prov} 검토 중</b>${lcClock(run.seconds)}`); owner = 'agent';
    msg = `${prov}가 코드를 읽고 계획서를 쓰는 중이에요. 끝나면 계획서가 올라와요.`;
  }else if(run?.mode === 'execute'){
    const lead = n => task || !total ? '' : `${n}/${total} · `;
    owner = 'agent';
    if(run.phase === 'rework'){
      const failed = run.failed || [];
      at(2, 'agent', `<b>${lead(run.sub_number ?? doneN)}재작업 중</b>${lcClock(run.seconds)}`);
      s[3] = 'fail'; m[3] = failed.length ? `<b>${lcCheckTool(failed[0])} ${failed.length}개 실패</b>` : '<b>검사 실패</b>';
      msg = `${esc(run.ref || it.ref)} 검사에서 ${esc(failed.length ? lcCheckTarget(failed[0]) : '검사')}가 실패해 ${prov}가 한 번 더 고치는 중이에요.`;
    }else if(run.phase === 'checks'){
      at(2, 'agent', `<b>${lead(doneN)}검사 중</b>${lcClock(run.seconds)}`);
      msg = `${prov}가 구현하는 중이에요 — 자동 병합이 켜져 있으면 운영 반영 확인 후 In Review로 올라와요.`;
    }else{
      at(2, 'agent', `<b>${lead(doneN)}${prov} 구현 중</b>${lcClock(run.seconds)}`);
      msg = task ? `부모 계획서 v${f.parent_plan_version ?? ''}의 ${it.sub_number ?? run.sub_number ?? ''}번째 Task예요. ${prov}가 구현하고 있어요.`
        : `${esc(run.ref || it.ref)}를 ${prov}가 구현하고 있어요. 끝나면 서버가 검사를 다시 돌리고 병합해요.`;
    }
  }else if(it.job){
    at(it.job.mode === 'review' ? 1 : 2, 'agent', `대기 ${it.job.position}번째`); owner = 'agent';
    msg = `${statusHtml('waiting')} ${PROVIDER_NAME[it.job.provider] || 'Claude'} ${RUN_MODE[it.job.mode]} 대기 ${it.job.position}번째예요 — ${esc(it.job.note || '앞의 일이 끝나면 차례로 시작해요.')}`;
  }else if(mergeWait){
    const hold = it.status === 'on_hold';
    at(3, hold ? 'hold' : 'human', hold ? '<b>병합 보류</b>' : x?.manual_merge_confirmable ? '<b>수동 반영 확인 대기</b>' : '<b>병합 진행 대기</b>');
    m[2] = count();
    msg = x?.retry_merge_available || !x ? '병합이 보류됐어요. 병합 재시도를 눌러 다시 진행할 수 있어요.'
      : x.manual_merge_confirmable ? '자동 병합이 끝나지 않았어요 — 직접 main에 반영했으면 “수동 반영 확인”을 눌러 주세요.'
      : '수동 커밋 병합 대기 — 병합 진행을 누르면 운영 반영을 시작해요.';
  }else if(it.status === 'in_review' || it.merge_state === '병합됨'){
    at(3, 'human', '<b>결과 확인 대기</b>'); m[2] = `${count(total)} 병합됨`.trim();
    msg = it.status !== 'in_review' ? '병합됨 — 결과를 확인해 주세요.'
      : task ? '결과 확인 대기 → 아래 “결과”에서 승인·수정 요청·거절을 골라 주세요.'
      : '모든 Task가 검사를 통과하고 병합됐어요. 운영 화면에서 확인하고 결과를 승인해 주세요.';
  }else if(it.status === 'on_hold'){
    at(task || ok ? 2 : plan ? 1 : 0, 'hold', '<b>보류</b>'); msg = oldMsg();
  }else if(it.status === 'changes_requested'){
    at(2, 'fail', '<b>수정 요청됨</b>'); owner = ownerBy(auto.auto_execute);
    msg = f.note ? `"${esc(f.note)}"라는 수정 요청이 남아 있어요. 에이전트에게 다시 맡겨 주세요.` : '수정 요청이 남아 있어요. 에이전트에게 다시 맡겨 주세요.';
  }else if(f.failed_tasks > 0){
    at(2, 'fail', `<b>실행 실패 ${f.failed_tasks}개</b>`); owner = ownerBy(auto.auto_execute); msg = oldMsg();
  }else if(c.claim_active && !(plan && !ok && !plan.revision_note && !task)){
    // 대화형 세션 등이 잡고 일하는 중(run·job 없음). 단 계획서 승인은 사람이 할 일이라 아래 승인 대기가 이긴다.
    const who = esc(actorName(c.claimed_by) || '에이전트'), name = `<b>${who} 작업 중</b>`;
    at(task || ok ? 2 : plan ? 1 : 0, 'agent', name); if(!task && !ok && !plan) m[0] = name; owner = 'agent';
    const until = c.lease_until ? new Date(c.lease_until).toLocaleTimeString('ko-KR', {hour: '2-digit', minute: '2-digit', hour12: false}) : '';
    msg = `${who}이(가) 잡고 작업 중이에요${until ? `(점유 만료 ${until})` : ''}.`;
  }else if(x || task){
    if(x?.blocked){ at(2, 'todo', `<b>${esc(String(x.blocked).slice(0, 24))}</b>`); owner = 'agent'; }
    else{ at(2, 'human', '<b>실행 맡기기 대기</b>'); owner = ownerBy(auto.auto_execute); }
    msg = oldMsg();
  }else if(!plan){
    at(0, 'human', '<b>검토 맡기기 대기</b>'); m[0] = '<b>검토 맡기기 대기</b>'; owner = ownerBy(auto.auto_review);
    msg = '계획서가 없어요. Claude나 Codex에게 검토를 맡겨 계획서를 받아 보세요.';
  }else if(plan.revision_note){
    at(1, 'agent', `<b>v${pv} 재검토 대기</b>`); owner = 'agent'; msg = oldMsg();
  }else if(!ok){
    at(1, 'human', `<b>v${pv} 승인 대기</b>`);
    if(f.planned_tasks) m[2] = `Task ${f.planned_tasks}개 예정`;
    msg = `계획서 v${pv}이 올라왔어요. 승인·메모 붙여 승인·재검토·거절 중에서 골라 주세요.`;
  }else if(tasks.some(t => !['done', 'closed', 'in_review'].includes(t.status)) || !total){
    at(2, total ? 'human' : 'human', count()); owner = total ? ownerBy(auto.auto_execute) : 'human'; msg = oldMsg();
  }else{
    at(3, 'human', '<b>결과 확인 대기</b>'); m[2] = count(total); msg = oldMsg();
  }
  // go 버튼은 quickAction 결과로만 정한다(DEV-243) — 도는 중·대기 중·병합 중에는 두지 않는다
  if(!run && !it.job && !LIVE_MERGE.has(it.merge_state)){
    const kind = quickAction(it).kind;
    go = kind === 'review' ? {label: '검토 맡기기', target: 'review-btns'}
      : kind === 'decision' ? {label: '계획서 보기', target: 'panel-plan'}
      : ['task-approve', 'complete-tree'].includes(kind) ? {label: '결과 승인', target: 'panel-result'}
      : kind === 'execute' ? {label: '실행 맡기기', target: 'exec-btns'}
      : (it.children || []).some(ch => quickAction(ch).kind === 'execute') ? {label: '실행 맡기기', target: 'panel-tasks'} : null;
  }
  if(task){
    s[0] = s[1] = 'inherited';
    m[0] = `<a href="#/issue/${esc(it.parent_ref)}">${esc(it.parent_ref)}</a>`;
    m[1] = f.parent_plan_version ? `부모 v${f.parent_plan_version}` : '';
  }
  const bars = total >= 2 ? tasks.map(t => t.status === 'done' ? 'done' : run?.ref === t.ref ? 'agent'
    : t.status === 'in_review' ? 'human' : t.status === 'changes_requested' ? 'fail' : 'todo') : null;
  const steps = s.map((state, i) => ({key: i, label: LC_NAMES[i], state, meta: m[i],
    bars: i === 2 ? bars : null, loop: i === 2 && f.loops > 0 ? f.loops + 1 : 0}));
  return {steps, cur, owner, msg, go};
}
function lifecycleHtml(it, auto){
  const lc = lifecycle(it, auto), x = it.execute;
  const icon = st => ({done: 'check', human: 'user', fail: 'x', hold: 'pause'})[st];
  const items = lc.steps.map((st, i) => {
    const prev = lc.steps[i - 1]?.state;
    const line = i && prev === 'done' && !['todo', 'skip'].includes(st.state) ? 'done' : '';
    const target = i >= 3 && it.status !== 'in_review' ? 'panel-tasks' : LC_PANELS[i];
    const plain = st.meta.replace(/<[^>]+>/g, '');
    return `<li class="lc-step" data-s="${st.state}" data-line="${line}"${i === lc.cur ? ' data-cur' : ''}>
      <button type="button" class="lc-jump" data-target="${target}" aria-label="${esc(`${st.label} — ${plain || '아직'} · 이동`)}"></button>
      <span class="lc-node">${icon(st.state) ? `<svg aria-hidden="true"><use href="#i-${icon(st.state)}"/></svg>` : ''}</span><span class="lc-name">${st.label}</span>
      ${st.loop ? `<span class="lc-loop" title="되돌아온 횟수"><svg aria-hidden="true"><use href="#i-rotate-ccw"/></svg>${st.loop}바퀴</span>` : ''}<span class="lc-meta">${st.meta || '&nbsp;'}</span>
      ${st.bars ? `<span class="lc-segs" aria-hidden="true">${st.bars.map(b => `<i data-s="${b}"></i>`).join('')}</span>` : ''}</li>`;
  }).join('');
  // 상황별 버튼은 이 줄 하나에만 둔다(DEV-113) — 도는 중·대기 중이면 숨긴다. id·동작은 그대로다.
  const idle = x && !it.review_running && !it.job && !LIVE_MERGE.has(it.merge_state);
  const pri = lc.owner === 'human' ? ' class="primary"' : '';
  const actions = (lc.go ? `<button type="button" class="lc-go${lc.owner === 'human' ? ' primary' : ''}" data-target="${lc.go.target}">${esc(lc.go.label)}</button>` : '')
    + (idle && x.manual_merge_available ? `<button id="manual-merge"${pri} title="연결된 수동 커밋을 기존 병합 경로로 반영해요.">병합 진행</button>` : '')
    + (idle && x.manual_merge_confirmable ? `<button id="confirm-manual-merge"${pri} title="Task 커밋이 main에 들어갔는지 서버가 확인하고 병합 완료로 표시해요. 재시작은 하지 않아요.">수동 반영 확인</button>` : '')
    + (idle && x.retry_merge_available ? `<button id="retry-merge"${pri} title="기존 구현 커밋으로 병합만 다시 진행해요. 자동 병합·재시작 정책이 적용돼요.">병합 재시도</button>` : '')
    + (idle && x.repair_available ? `<button class="register-repair${lc.owner === 'human' ? ' primary' : ''}" title="에이전트 재호출 없이 연결된 수정 커밋을 기존 병합 정책으로 처리해요.">수정 커밋 결과 등록</button>` : '');
  const turn = {agent: '에이전트 차례', human: '내 차례', none: '끝남'}[lc.owner];
  return `<section class="lc" aria-label="이슈 진행 단계"><ol class="lc-steps">${items}</ol>
    <div class="lc-next stage" id="stage"><span class="lc-turn" data-o="${lc.owner}">${turn}</span><span class="lc-msg">${lc.msg}</span>${actions ? `<span class="stage-actions">${actions}</span>` : ''}</div></section>`;
}
// 칸·go 버튼은 해당 패널로 내려가기만 한다(실제 동작은 각 패널의 버튼)
document.addEventListener('click', e => {
  const b = e.target.closest('.lc [data-target]');
  if(!b) return;
  const el = document.getElementById(b.dataset.target) || document.getElementById('panel-tasks');
  el?.scrollIntoView({block: 'start', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
});
// 부모 화면에서 바로 실행: quickAction이 execute일 때만(서버가 다시 확인한다). 바쁘거나 선행이 안 끝났으면 줄에 선다(DEV-43)
// — Task 여럿을 한 번에 눌러 두면 선행 순서대로 돈다.
const pendingTasks = new Set();
function taskActionsHtml(it, ch){
  const icon = provider => `<svg class="ico brand-icon" aria-hidden="true"><use href="#i-${provider === 'codex' ? 'openai' : 'claude'}"/></svg>`;
  if(ch.job){
    const provider = ch.job.provider || 'claude', label = `${PROVIDER_NAME[provider]} 실행 대기 중`;
    return `<div class="task-actions"><button class="run-task task-queued" data-ref="${esc(ch.ref)}" data-provider="${provider}" disabled aria-label="${label}" title="${esc(`${label} — 대기 ${ch.job.position}번째${ch.job.note ? ' — ' + ch.job.note : ''}`)}">${icon(provider)}<span class="hide-m">${label}</span></button></div>`;
  }
  if(quickAction(ch).kind !== 'execute') return '';
  return `<div class="task-actions">${['claude', 'codex'].map(provider => {
    const label = `${PROVIDER_NAME[provider]}에게 실행 맡기기`;
    return `<button class="run-task" data-ref="${esc(ch.ref)}" data-provider="${provider}"${pendingTasks.has(ch.ref) ? ' disabled' : ''} title="${label}" aria-label="${label}">${icon(provider)}<span class="hide-m">${PROVIDER_NAME[provider]}</span></button>`;
  }).join('')}</div>`;
}
// 갱신 중에도 아직 응답하지 않은 검토 요청의 provider를 유지한다.
const pendingReviews = new Map();
async function renderIssue(ref, background = false){
  const seq = ++issueRenderSeq, hash = location.hash;
  const [it, { runs }, catalog, , auto] = await Promise.all([api('GET', `/api/issues/${encodeURIComponent(ref)}`), api('GET', `/api/issues/${encodeURIComponent(ref)}/runs`), api('GET', '/api/issue-types?include_inactive=true'), agentsById.size ? null : loadAgents(),
    api('GET', '/api/settings/auto', undefined, undefined, {notifyErrors: false}).catch(() => ({}))]);
  if(seq !== issueRenderSeq || hash !== location.hash) return;
  const queue = it.job ? (await restartJobsSnapshot().catch(() => ({jobs: []}))).jobs : [];
  if(seq !== issueRenderSeq || hash !== location.hash) return;
  const edges = {first: queue[0]?.id, last: queue.at(-1)?.id};
  if(background && (pendingActions.size || pendingTasks.size || pendingExecutions.size || pendingReviews.size || view.querySelector('#dep-editor[open]') || document.activeElement?.matches('input, select'))) return;
  if(background && [...view.querySelectorAll('textarea, .edit-title')].some(el => el.id !== 'comment' && el.id !== 'rating-review' && el.offsetParent)) return;
  if(background && [...view.querySelectorAll('#labels, #issue-type-editor input, #rating-editor input, #rating-review')].some(el => ['checkbox', 'radio'].includes(el.type) ? el.checked !== el.defaultChecked : el.value !== el.defaultValue)) return;
  const previousEditor = view.querySelector('#issue-type-editor');
  if(background && previousEditor && JSON.stringify(selectedTypes(previousEditor)) !== previousEditor.dataset.initialTypes) return;
  const comment = background ? view.querySelector('#comment')?.value : null;
  const focused = background && document.activeElement?.id === 'comment';
  const selection = focused ? [document.activeElement.selectionStart, document.activeElement.selectionEnd] : null;
  const agentOpts = ['<option value="">(없음)</option>'].concat([...agentsById.values()].filter(a => a.kind === 'agent' || a.id === it.assignee_agent_id).map(a =>
    `<option value="${a.id}"${a.id === it.assignee_agent_id ? ' selected' : ''}>${esc(a.name)}${a.kind === 'chat' ? ' (Chat)' : ''}</option>`)).join('');
  const liveRun = runs.find(r => r.status === 'running') || {};
  const liveMode = liveRun.mode;
  const reviewProvider = pendingReviews.get(ref) || (it.review_running && liveMode === 'review' ? liveRun.provider : null);
  const reviewBusy = it.review_running || pendingReviews.has(ref);
  const reviewButton = (provider, id) => `<button id="${id}" title="홈서버에서 검토만 해요 — 계획서·질문을 남겨요(코드 수정 없음)."><svg class="ico brand-icon" aria-hidden="true"><use href="#i-${provider === 'codex' ? 'openai' : 'claude'}"/></svg>${PROVIDER_NAME[provider]}에게 검토 맡기기</button>`;
  // 검토 절은 Task가 아닐 때만 — 도는 중이면 버튼 대신 상태 줄, 버튼은 quickAction이 review일 때만(DEV-243)
  const qa = quickAction(it);
  const reviewIdle = qa.kind === 'review' ? `<div class="agent-btns" id="review-btns">${reviewButton('claude', 'ask-review')}${reviewButton('codex', 'ask-codex-review')}</div>${it.review_busy ? `<div class="dim hint">다른 이슈에서 Agent가 일하는 중이에요 — ${QUEUE_LINE}</div>` : ''}`
    : qa.why === 'claimed' ? `<div class="dim hint">${esc(actorName(it.action_context.claimed_by))}이(가) 잡고 있어요 — 점유가 풀리면 검토를 맡길 수 있어요.</div>`
    : it.plan && !['done', 'closed'].includes(it.status) ? '<div class="dim hint">계획서가 있어요 — 다시 검토하려면 계획서 아래 메모 붙여 재검토를 골라 주세요.</div>' : '';
  const reviewSection = it.execute ? '' : `<div class="agent-sub">검토</div>${it.job && it.job.mode === 'review' ? jobHtml(it.job, edges) : reviewBusy ? busyLine(reviewProvider, '검토') : reviewIdle}`;
  closeProgress();
  eventAttachments = new Map((it.attachments || []).map(a => [a.id, a]));
  view.innerHTML = `
    <div class="detail">
      <div>
        <div class="ref">${esc(it.ref)}${it.parent_ref ? ` · Task of <a href="#/issue/${esc(it.parent_ref)}">${esc(it.parent_ref)}</a>` : ''}</div>
        <h1 id="title">${titleHtml(it)}</h1>
        <div class="byline">${actorHtml(it.reporter)}<span>·</span><span>${fmtTime(it.created_at)}</span>${labelsHtml(it.labels)}${typesHtml(it)}</div>
        <div class="issue-deps">${blockedHtml(it.blocked_by)}<button type="button" id="edit-deps">연관 이슈 편집</button></div>
        <dialog id="dep-editor" class="dep-dialog"><form method="dialog"><fieldset class="dep-picker"><legend>연관 이슈 (선행)</legend>
          <p class="dim">최대 10개까지 선택할 수 있어요.</p><label for="dep-search">이슈 검색<input id="dep-search" type="search" placeholder="번호 또는 제목으로 검색" autocomplete="off"></label>
          <div id="dep-selected" class="dep-selected" aria-label="선택한 연관 이슈"></div><div id="dep-list" class="dep-list" aria-label="연관 이슈 후보"></div>
          <p id="dep-status" role="status"></p><div class="row-end"><button type="button" id="dep-cancel">취소</button><button type="button" class="primary" id="dep-save">저장</button></div>
        </fieldset></form></dialog>
        ${lifecycleHtml(it, auto || {})}
        <div class="panel" id="panel-description"><h2>Description<span class="right"><button id="edit-body">고치기</button></span></h2>
          <div id="body">${it.body ? md(it.body) : '<p class="dim">본문이 없어요.</p>'}</div></div>
        ${attachmentsHtml(it.attachments)}
        <div class="panel" id="panel-plan"><h2>Plan${it.plan ? ` <span class="meta">v${it.plan.version} · ${esc(actorName(it.plan.author))} · ${fmtTime(it.plan.created_at)}</span>` : ''}
          <span class="right">${it.plan && it.plan.version > 1 ? '<button id="plan-history">이전 판</button>' : ''}<button id="edit-plan">${it.plan ? '고쳐 쓰기' : '쓰기'}</button></span></h2>
          <div id="plan">${it.plan ? md(it.plan.body) : '<p class="dim">아직 계획서가 없어요.</p>'}</div>
          ${it.plan && !['done', 'closed'].includes(it.status) ? decisionHtml(it) : ''}</div>
        ${it.superseded_by_plan ? `<div class="stage">계획서 v${it.superseded_by_plan}으로 대체된 이전 Task예요. 기록은 보존되며 실행 대상에서 제외됐어요.</div>` : ''}
        ${it.parent_ref ? '' : resultHtml(it)}
        <div class="panel" id="panel-tasks"><h2>Tasks <span class="meta">${it.children.length}</span><span class="right"><a class="button" href="#/new?parent=${esc(it.ref)}">Task 추가</a></span></h2>
          ${it.children.length ? `<table class="issues tasks">${it.children.map(ch => `<tr class="row" data-ref="${esc(ch.ref)}"><td class="ref">${esc(ch.ref)}</td>
            <td>${titleHtml(ch)}${blockedHtml(ch.blocked_by)}<div class="task-history">${processingAgentsHtml(ch)} ${completionTimeHtml(ch)} ${issueTokensHtml(ch)}</div></td><td>${statusHtml(ch.status)}${progressHtml(ch)}</td>
            <td>${taskActionsHtml(it, ch)}</td></tr>`).join('')}</table>` : '<p class="dim">하위 Task가 없어요.</p>'}</div>
        ${it.superseded_children?.length ? `<details class="panel" id="superseded-tasks"><summary>이전 계획의 Task · ${it.superseded_children.length}개</summary>${it.superseded_children.map(ch => `<p><a href="#/issue/${esc(ch.ref)}">${esc(ch.ref)} · ${esc(ch.title)}</a> <span class="dim">계획서 v${ch.superseded_by_plan}으로 대체</span></p>`).join('')}</details>` : ''}
        <div class="panel"><h2>Activity</h2><ul class="timeline">${it.events.filter(e => e.kind !== 'claim').map(eventHtml).join('') || '<li class="dim empty-line">아직 활동이 없어요.</li>'}</ul>
          <div class="comment-box"><textarea id="comment" placeholder="댓글(마크다운)"></textarea>
            <div class="row-end"><button id="send-comment" class="primary">댓글 달기</button></div></div></div>
        ${it.parent_ref ? resultHtml(it) : ''}
      </div>
      <aside class="side panel">
        <div class="field issue-token-detail"><span>Tokens</span><div>${issueTokensHtml(it, true)}</div></div>
        <div class="field"><span>Status</span><select id="status">${STATUSES.map(s => `<option value="${s}"${s === it.status ? ' selected' : ''}>${STATUS_LABEL[s]}</option>`).join('')}</select>
          ${clockHtml(it)}${it.children.length || it.parent_ref ? '<button class="complete-tree" title="최상위 이슈와 모든 Task를 한 번에 Done으로">전체 완료</button>' : ''}</div>
        <div class="field" hidden><span>Priority</span><select id="priority">${PRIORITIES.map(p => `<option${p === it.priority ? ' selected' : ''}>${p}</option>`).join('')}</select></div>
        <div class="field"><span>Assignee</span><select id="assignee">${agentOpts}</select></div>
        <div class="field" id="issue-type-editor">${typePicker(catalog.types, it.type_ids || [], {hint:false, legend:'종류', automatic:(it.types || []).some(t => t.source === 'agent')})}<button id="save-types" type="button">종류 저장</button><p id="types-status" role="status"></p></div><div class="field"><span>Labels</span><input id="labels" placeholder="쉼표로 구분" value="${esc(it.labels.join(', '))}"></div>
        <div class="field" id="rating-editor"><span>평가</span>
          <div class="stars" role="radiogroup" aria-label="평점">${[1, 2, 3, 4, 5].map(n => `<label title="${n}점"><input type="radio" name="rating" value="${n}"${it.rating === n ? ' checked' : ''} aria-label="${n}점"><span aria-hidden="true">${n <= (it.rating || 0) ? '★' : '☆'}</span></label>`).join('')}</div>
          <textarea id="rating-review" rows="3" maxlength="2000" aria-label="리뷰" placeholder="에이전트가 얼마나 잘 처리했나요?">${esc(it.review || '')}</textarea>
          <div class="row-end"><button id="rating-clear" type="button">지우기</button><button id="rating-save" type="button">평가 저장</button></div>
          <p id="rating-status" class="rating-status" role="status"></p>
          ${it.rated_at ? `<p class="dim">${esc(actorName(it.rated_by))} · ${fmtTime(it.rated_at)}</p>` : ''}</div>
        <div class="field"><span>AGENT</span>${processingAgentsHtml(it)}</div>
        ${['done', 'closed'].includes(it.status) ? `<div class="field"><span>TIME</span>${completionTimeHtml(it)}</div>` : ''}
        <div class="field"><span>Commits</span>${it.events.filter(e => e.kind === 'commit').map(e => `<div><code>${esc(e.data.sha.slice(0, 7))}</code> <span class="dim">${esc(e.data.repo)}</span></div>`).join('') || '<span class="dim">없음</span>'}</div>
        <div class="field"><span>Agent</span>
          ${reviewSection}
          ${it.execute ? executeHtml(it, liveMode, liveRun.provider, edges) : ''}</div>
        <div class="field"><span>실행 기록</span>${runsHtml(runs)}</div>
        <div class="field"><button id="delete" class="danger" title="이슈 지우기" aria-label="이슈 지우기"><svg class="ico"><use href="#i-trash"/></svg></button></div>
      </aside>
    </div>`;
  bindAttachmentPreviews();
  const editor = view.querySelector('#issue-type-editor');
  bindTypePicker(editor);
  editor.dataset.initialTypes = JSON.stringify(selectedTypes(editor));
  editor.querySelector('#save-types').onclick = async () => {
    const controls = [...editor.querySelectorAll('button')];
    if(controls.some(b => b.disabled)) return;
    controls.forEach(b => b.disabled = true);
    editor.querySelector('#types-status').textContent = '저장 중…';
    try{ await api('PATCH', `/api/issues/${encodeURIComponent(it.ref)}`, {type_ids:selectedTypes(editor)}); if(editor.isConnected) await renderIssue(it.ref); }
    catch(e){ if(editor.isConnected) editor.querySelector('#types-status').textContent = `${e.message} 다시 시도해 주세요.`; }
    finally{ controls.forEach(b => b.disabled = false); }
  };
  const R = encodeURIComponent(it.ref);
  const reload = () => renderIssue(it.ref);
  const $ = (id) => view.querySelector('#' + id);
  const depDialog = $('dep-editor'), depSelected = new Set((it.blocked_by || []).map(b => b.ref));
  let depCandidates = [];
  const drawDepEditor = () => {
    const query = $('dep-search').value.trim().toLocaleLowerCase();
    $('dep-selected').innerHTML = [...depSelected].map(ref => `<button type="button" class="dep-tag" data-dep-remove="${esc(ref)}" aria-label="${esc(ref)} 연관 이슈 제거">${esc(ref)} ×</button>`).join('');
    $('dep-list').innerHTML = depCandidates.filter(d => d.ref !== it.ref && `${d.ref} ${d.title}`.toLocaleLowerCase().includes(query)).map(d =>
      `<button type="button" class="dep-option" data-dep="${esc(d.ref)}" aria-pressed="${depSelected.has(d.ref)}"><span class="ref">${esc(d.ref)}</span><span>${esc(d.title || '(제목 없음)')}</span><span class="dim">${esc(d.status)}</span></button>`).join('') || '<p class="dim">선택할 이슈가 없어요.</p>';
  };
  $('edit-deps').onclick = async () => {
    depSelected.clear(); (it.blocked_by || []).forEach(b => depSelected.add(b.ref));
    depDialog.showModal(); $('dep-status').textContent = '연관 이슈를 불러오는 중…';
    try{ const result = await api('GET', `/api/projects/${encodeURIComponent(it.ref.split('-')[0])}/dep-candidates?include_review=true&all_projects=true`);
      if(!depDialog.isConnected || !depDialog.open) return;
      depCandidates = result.issues; $('dep-status').textContent = ''; drawDepEditor(); $('dep-search').focus();
    }catch(e){ if(depDialog.isConnected) $('dep-status').textContent = e.message; }
  };
  $('dep-search').oninput = drawDepEditor;
  depDialog.onclick = e => {
    const remove = e.target.closest('[data-dep-remove]'), option = e.target.closest('[data-dep]');
    if(remove) depSelected.delete(remove.dataset.depRemove);
    if(option){ if(depSelected.has(option.dataset.dep)) depSelected.delete(option.dataset.dep);
      else if(depSelected.size < 10) depSelected.add(option.dataset.dep);
      else { $('dep-status').textContent = '연관 이슈는 최대 10개까지 선택할 수 있어요.'; return; } }
    if(remove || option){ $('dep-status').textContent = ''; drawDepEditor(); }
  };
  $('dep-cancel').onclick = () => depDialog.close();
  $('dep-save').onclick = async () => {
    $('dep-save').disabled = true; $('dep-status').textContent = '저장 중…';
    try{ await api('PATCH', `/api/issues/${R}`, {blocked_by:[...depSelected]}, () => depDialog.isConnected, {notifyErrors:false}); depDialog.close(); await reload(); }
    catch(e){ if(depDialog.isConnected) $('dep-status').textContent = e.message; }
    finally{ if(depDialog.isConnected) $('dep-save').disabled = false; }
  };
  const ratingBox = $('rating-editor');
  const paintStars = () => ratingBox.querySelectorAll('.stars label').forEach(l => { l.querySelector('span').textContent = l.querySelector('input').checked || l.dataset.on ? '★' : '☆'; });
  const starsUpTo = (v) => ratingBox.querySelectorAll('.stars label').forEach((l, i) => { l.dataset.on = i < v ? '1' : ''; });
  let curRating = it.rating || 0;
  starsUpTo(curRating);
  ratingBox.querySelectorAll('.stars input').forEach(inp => inp.onclick = () => {
    const v = Number(inp.value);
    if(v === curRating){ inp.checked = false; curRating = 0; } else curRating = v;   // 같은 별을 다시 누르면 해제
    starsUpTo(curRating); paintStars();
  });
  const saveRating = async (rating, review) => {
    const btns = [...ratingBox.querySelectorAll('button')];
    if(btns.some(b => b.disabled)) return;
    btns.forEach(b => b.disabled = true);
    const st = $('rating-status'); st.textContent = '저장 중…';
    try{ await api('PUT', `/api/issues/${R}/rating`, {rating, review}); await reload(); const n = $('rating-status'); if(n) n.textContent = '저장했어요.'; }
    catch(e){ if(st.isConnected) st.textContent = `${e.message} 다시 시도해 주세요.`; }
    finally{ btns.forEach(b => b.disabled = false); }
  };
  $('rating-save').onclick = () => saveRating(curRating || null, $('rating-review').value.trim());
  $('rating-clear').onclick = () => saveRating(null, '');
  view.querySelectorAll('tr.row').forEach(r => r.addEventListener('click', (e) => { if(!e.target.closest('a')) location.hash = `#/issue/${r.dataset.ref}`; }));
  // done/closed로 끝내면 목록으로 돌아간다(DEV-5) — 끝난 이슈 화면에 머물 일은 없다.
  const setStatus = async (status, note, attachmentIds) => {
    let updated;
    if(status === 'done' && it.merge_state === '병합 대기'
      && !await confirmDialog('병합 전에 완료할까요?', {description:`${it.ref} 코드가 반영되지 않은 채 완료돼요.`, okText:'완료로 바꾸기', focusCancel:true})){
      $('status').value = it.status; return;
    }
    if(status === 'done' && !await confirmChecks(it.human_checks)){ reload(); return; }
    try{ updated = await api('POST', `/api/issues/${R}/status`, { status, ...(note === undefined ? {} : { note }), ...(attachmentIds?.length ? { attachment_ids: attachmentIds } : {}), ...(status === 'done' ? { confirm_checks: true } : {}) }); }
    catch(e){ reload(); return; }
    if(status === 'closed' && updated.status !== 'closed'){
      toast('롤백 후 운영 반영을 확인 중이에요 — 보류 상태로 남겨요.'); await reload(); return;
    }
    if(status === 'done' || status === 'closed'){ toast(`${it.ref}을(를) 끝냈어요`); location.hash = '#/'; }
    else reload();
  };
  $('status').addEventListener('change', async (e) => {
    if(e.target.value !== 'closed'){ setStatus(e.target.value); return; }
    // Closed는 "안 하기로 함" — 왜 닫는지 남긴다(DEV-6). 취소하거나 비우면 닫지 않는다.
    const reason = ((await confirmDialog('이슈를 닫을까요?', {description:'닫는 이유를 적어 주세요 (예: NS-3과 중복, 필요 없어짐).', input:true, okText:'닫기', danger:true})) || '').trim();
    if(!reason){ e.target.value = it.status; toast('사유가 없어서 닫지 않았어요'); return; }
    setStatus('closed', reason);
  });
  $('priority').addEventListener('change', async (e) => { await api('PATCH', `/api/issues/${R}`, { priority: e.target.value }).catch(() => {}); reload(); });
  $('assignee').addEventListener('change', async (e) => { await api('PATCH', `/api/issues/${R}`, { assignee_agent_id: e.target.value ? Number(e.target.value) : null }).catch(() => {}); reload(); });
  $('labels').addEventListener('change', async (e) => {
    await api('PATCH', `/api/issues/${R}`, { labels: e.target.value.split(',').map(s => s.trim()).filter(Boolean) }).catch(() => {}); reload();
  });
  // 전체 완료(DEV-44): 최상위 이슈와 모든 Task를 한 번에 done. 닫힐 목록과 주의할 Task를 보여 주고 확인받는다.
  view.querySelectorAll('.complete-tree').forEach(b => b.addEventListener('click', async () => {
    let root = it;
    while(root.parent_ref) root = await api('GET', `/api/issues/${encodeURIComponent(root.parent_ref)}`).catch(() => null) || {};
    if(!root.ref) return;
    // 최상위의 바로 아래 Task까지만 보여 준다(서버는 더 깊은 자손도 닫는다)
    const open = [root, ...root.children].filter(x => !['done', 'closed'].includes(x.status));
    if(!open.length){ toast('이미 모두 끝났어요'); return; }
    if(!await confirmCompleteTree(root)) return;
    if(!await confirmChecks(root.human_checks)) return;
    b.disabled = true; b.textContent = '완료하는 중…';
    try{ await api('POST', `/api/issues/${R}/complete-tree`, { confirm_checks: true }); }
    catch(e){ reload(); return; }
    toast(`${root.ref} 묶음을 끝냈어요`); location.hash = '#/';
  }));
  if($('approve')){
    $('approve').addEventListener('click', () => setStatus('done'));
    let to = null;
    const picker = attachPicker({list: $('result-files'), send: $('result-send'), button: $('result-attach-btn'), input: $('result-file'), textarea: $('result-note'), enabled: () => to === 'changes_requested', imagesOnly: true});
    $('result-actions').querySelectorAll('[data-to]').forEach(b => b.addEventListener('click', () => {
      to = b.dataset.to; const f = RESULT_FORM[to];
      $('result-form').hidden = false; $('result-actions').hidden = true;
      $('result-attach').hidden = to !== 'changes_requested';
      $('result-note').value = ''; $('result-note').placeholder = f.placeholder;
      $('result-send').textContent = f.send; $('result-send').className = f.cls; $('result-note').focus();
    }));
    $('result-cancel').addEventListener('click', () => { picker.clear(); $('result-form').hidden = true; $('result-actions').hidden = false; });
    $('result-send').addEventListener('click', (e) => {
      const note = $('result-note').value.trim();
      if(!note){ toast('메모를 적어 주세요'); return; }
      const ids = to === 'changes_requested' ? picker.ids() : [];
      // 보낸 뒤에는 화면을 새로 그리므로 이 목록은 비운다(떠날 때 연결된 첨부를 지우지 않게). 실패하면 임시 첨부는 만료로 정리된다.
      picker.take();
      whileBusy(e.currentTarget, () => setStatus(to, note, ids));
    });
  }
  if($('decision-actions')){
    let verdict = null;
    const picker = attachPicker({list: $('decision-files'), send: $('decision-send'), button: $('decision-attach-btn'), input: $('decision-file'), textarea: $('decision-note'), enabled: () => verdict === 'review'});
    const send = async (v, note) => {
      if(v === 'review') {
        if(!note.trim()){ toast('수정할 방향을 메모로 적어 주세요'); return; }
        const r = await api('POST', `/api/issues/${R}/review`, { provider: $('revision-provider').value, note, plan_version: it.plan.version, attachment_ids: picker.ids() });
        picker.take();
        toast(queuedMsg(r, '메모 붙여 재검토를 맡겼어요 — 새 계획서를 기다려 주세요'));
        await reload(); return;
      }
      const updated = await api('POST', `/api/issues/${R}/decision`, { verdict: v, note, plan_version: it.plan.version });
      if(v === 'reject' && updated.status !== 'closed'){ toast('롤백 후 운영 반영을 확인 중이에요 — 보류 상태로 남겨요.'); await reload(); return; }
      toast(v === 'reject' ? `${it.ref}을(를) 거절해서 닫았어요` : '결정을 남겼어요'); if(v === 'reject') location.hash = '#/'; else reload(); };
    $('decision-actions').querySelectorAll('button').forEach(b => b.addEventListener('click', (e) => {
      verdict = b.dataset.verdict;
      if(verdict === 'approve'){ whileBusy(e.currentTarget, () => send('approve', '')); return; }
      $('decision-form').hidden = false; $('decision-actions').hidden = true;
      $('revision-provider-row').hidden = verdict !== 'review';
      $('decision-attach').hidden = verdict !== 'review';
      $('revision-provider').value = runs.find(r => r.mode === 'review')?.provider || 'claude';
      $('decision-note').value = verdict === 'review' ? (it.plan.revision_note || it.approval?.note || '') : verdict === 'approve_notes' ? decisionQuestions(it.plan.body) : '';
      $('decision-note').placeholder = verdict === 'review' ? '예: SQLite는 임시예요. PostgreSQL 기준으로 계획서를 다시 작성해 주세요.' : verdict === 'approve_notes' ? '계획서가 물은 것에 대한 답, 고칠 내용 (예: 1번은 끄기로)' : '거절 이유 (예: NS-3과 중복, 필요 없어짐)';
      $('decision-send').textContent = verdict === 'review' ? '메모 붙여 재검토' : verdict === 'approve_notes' ? '메모 붙여 승인' : '거절하고 닫기';
      $('decision-send').classList.toggle('danger', verdict === 'reject'); $('decision-send').classList.toggle('primary', verdict !== 'reject');
      $('decision-note').focus();
    }));
    $('decision-cancel').addEventListener('click', () => { picker.clear(); $('decision-form').hidden = true; $('decision-actions').hidden = false; });
    $('decision-send').addEventListener('click', (e) => whileBusy(e.currentTarget, () => send(verdict, $('decision-note').value)));
  }
  view.querySelectorAll('.run-task').forEach(b => b.addEventListener('click', async (e) => {
    e.stopPropagation();
    const ref = b.dataset.ref, provider = b.dataset.provider;
    if(b.disabled || pendingTasks.has(ref)) return;
    pendingTasks.add(ref);
    b.closest('.task-actions').querySelectorAll('button').forEach(button => { button.disabled = true; });
    try{
      const task = await api('GET', `/api/issues/${encodeURIComponent(ref)}`);
      const name = PROVIDER_NAME[provider], x = task.execute;
      const limit = provider === 'codex' ? `시간 제한은 ${Math.floor(x.timeout_sec / 60)}분이고 비용 상한은 없어요.` : `비용 상한은 $${x.budget_usd}이에요.`;
      const checks = await confirmDialog(`${ref}을(를) ${name}에게 실행 맡길까요?`, {description:`${delegateLine(task, provider)}홈서버에서 relay/${ref} 브랜치의 worktree에 코드를 고치고 커밋해요(push·재시작은 하지 않아요).
${limit} 사용량은 이 서버에 로그인된 ${name} 계정에서 나가요.
${QUEUE_LINE}`, okText:'실행 맡기기', checklist:x.checks});
      if(!checks) return;
      const r = await api('POST', `/api/issues/${encodeURIComponent(ref)}/execute`, { provider, checks });
      toast(queuedMsg(r, '실행을 맡겼어요 — 끝나면 in_review로 올라와요'));
      await reload();
    }catch(e){ /* api()가 오류를 알린다 */ }
    finally{
      pendingTasks.delete(ref);
      view.querySelectorAll('.run-task').forEach(button => {
        if(button.dataset.ref === ref && !button.classList.contains('task-queued')) button.disabled = false;
      });
    }
  }));
  // Claude에게 검토 맡기기(DEV-13) — 도는 동안은 15초마다 이 화면을 다시 불러 계획서가 올라오면 보이게
  if($('confirm-manual-merge')) $('confirm-manual-merge').onclick = async e => {
    e.currentTarget.disabled = true;
    try{
      await api('POST', `/api/issues/${R}/confirm-manual-merge`, {});
      toast('main 반영을 확인해 병합 완료로 표시했어요.');
    }catch(e){ /* api()가 오류를 알린다. */ }
    finally{ if(location.hash === `#/issue/${it.ref}`) await reload(); }
  };
  if($('retry-merge')) $('retry-merge').onclick = async e => {
    e.currentTarget.disabled = true;
    try{
      await api('POST', `/api/issues/${R}/retry-merge`, {});
      toast('기존 커밋으로 병합 재시도를 시작했어요.');
    }catch(e){ /* api()가 오류를 알린다. */ }
    finally{ if(location.hash === `#/issue/${it.ref}`) await reload(); }
  };
  if($('manual-merge')) $('manual-merge').onclick = async e => {
    if(e.currentTarget.disabled) return;
    e.currentTarget.disabled = true;
    e.currentTarget.textContent = '병합 시작 중…';
    try{
      await api('POST', `/api/issues/${R}/manual-merge`, {});
      toast('수동 커밋의 병합을 시작했어요.');
    }catch(e){ /* api()가 오류를 알린다. */ }
    finally{ if(location.hash === `#/issue/${it.ref}`) await reload(); }
  };
  view.querySelectorAll('.register-repair').forEach(button => button.onclick = e => whileBusy(e.currentTarget, async () => {
    await api('POST', `/api/issues/${R}/register-repair`, {});
    toast('수정 커밋의 결과 등록을 시작했어요.');
    if(location.hash === `#/issue/${it.ref}`) await reload();
  }));
  for(const [id, provider] of [['ask-execute', 'claude'], ['ask-codex-execute', 'codex']]) {
    if($(id)) $(id).addEventListener('click', async (e) => {
    if(e.currentTarget.disabled || pendingExecutions.has(ref)) return;
    const name = PROVIDER_NAME[provider];
    const limit = provider === 'codex' ? `시간 제한은 ${Math.floor(it.execute.timeout_sec / 60)}분이고 비용 상한은 없어요.` : `비용 상한은 $${it.execute.budget_usd}이에요.`;
    const checks = await confirmDialog(`${it.ref}을(를) ${name}에게 실행 맡길까요?`, {description:`${delegateLine(it, provider)}홈서버에서 ${it.execute.branch ? it.execute.branch.branch : 'relay/' + it.ref} 브랜치의 worktree에 코드를 고치고 커밋해요(push·재시작은 하지 않아요).
${limit} 사용량은 이 서버에 로그인된 ${name} 계정에서 나가요.
${QUEUE_LINE}`, okText:'실행 맡기기', checklist:it.execute.checks});
    if(!checks) return;
    pendingExecutions.set(ref, provider);
    $('exec-btns').outerHTML = busyLine(provider, '실행');
    try {
      const r = await api('POST', `/api/issues/${R}/execute`, { provider, checks });
      toast(queuedMsg(r, '실행을 맡겼어요 — 끝나면 in_review로 올라와요'));
    } catch(e) { /* api()가 이미 오류를 알린다. */ }
    finally {
      pendingExecutions.delete(ref);
      if(location.hash === `#/issue/${it.ref}`) await reload();
    }
  });
  }
  for(const [id, provider] of [['ask-review', 'claude'], ['ask-codex-review', 'codex']]) {
    if($(id)) $(id).addEventListener('click', async (e) => {
      if(e.currentTarget.disabled || pendingReviews.has(ref)) return;
      const name = PROVIDER_NAME[provider];
      if(!await confirmDialog(`${it.ref}을(를) ${name}에게 검토 맡길까요?`, {description:reviewConfirmText(it, provider), okText:'검토 맡기기'})) return;
      pendingReviews.set(ref, provider);
      $('review-btns').outerHTML = busyLine(provider, '검토');
      try {
        const r = await api('POST', `/api/issues/${R}/review`, { provider });
        toast(queuedMsg(r, '검토를 맡겼어요 — 몇 분 뒤 계획서가 올라와요'));
      } catch(e) { /* api()가 이미 오류를 알린다. */ }
      finally {
        pendingReviews.delete(ref);
        if(location.hash === `#/issue/${it.ref}`) await reload();
      }
    });
  }
  if(comment != null && $('comment')) $('comment').value = comment;
  if(focused && $('comment')){ $('comment').focus({preventScroll:true}); $('comment').setSelectionRange(...selection); }
  if(activeProgress(it) || it.children.some(activeProgress)) followProgress(() => renderIssue(ref, true), () => location.hash === hash);
  else stopLiveRefresh();
  $('delete').addEventListener('click', async () => {
    if(!await confirmDialog(`${it.ref}을 지울까요?`, {description:'계획서·활동 기록도 같이 지워져요.', okText:'지우기', danger:true, focusCancel:true})) return;
    await api('DELETE', `/api/issues/${R}`); location.hash = '#/';
  });
  $('send-comment').addEventListener('click', (e) => whileBusy(e.currentTarget, async () => {
    const body = $('comment').value;
    if(!body.trim()) return;
    await api('POST', `/api/issues/${R}/comments`, { body }); reload();
  }));
  $('edit-body').addEventListener('click', () => editInPlace($('body'), it.body, async (v, title) => {
    await api('PATCH', `/api/issues/${R}`, title.trim() ? { body: v, title } : { body: v }); reload();   // 제목을 비워 두면 그대로(에이전트가 지음)
  }, it.title));
  $('edit-plan').addEventListener('click', () => editInPlace($('plan'), it.plan ? it.plan.body : '', async (v) => {
    await api('POST', `/api/issues/${R}/plans`, { body: v }); reload();
  }));
  if($('plan-history')) $('plan-history').addEventListener('click', async () => {
    const plans = (await api('GET', `/api/issues/${R}/plans`)).plans;
    $('plan').innerHTML = plans.map(p => `<details${p.version === it.plan.version ? ' open' : ''}><summary>v${p.version} · ${actorHtml(p.author)} · ${fmtTime(p.created_at)}</summary>${md(p.body)}</details>`).join('');
  });
}
function editInPlace(box, value, save, title){
  box.innerHTML = `${title !== undefined ? `<input class="edit-title" style="width:100%;margin-bottom:6px" value="${esc(title)}">` : ''}
    <textarea style="min-height:240px">${esc(value)}</textarea><div class="row-end" style="margin-top:6px"><button class="cancel">취소</button><button class="primary save">저장</button></div>`;
  box.querySelector('.cancel').addEventListener('click', route);
  const button = box.querySelector('.save');
  let saving = false;
  button.addEventListener('click', async () => {
    if(saving) return;
    const t = box.querySelector('.edit-title');
    const body = box.querySelector('textarea').value;
    const heading = t ? t.value : undefined;
    const controls = [...box.querySelectorAll('input, textarea, button')];
    saving = true;
    button.style.width = `${button.getBoundingClientRect().width}px`;
    controls.forEach(control => { control.disabled = true; });
    button.setAttribute('aria-label', '저장 중');
    button.setAttribute('title', '저장 중');
    button.setAttribute('aria-busy', 'true');
    button.innerHTML = '<svg class="ico list-spinner" aria-hidden="true"><use href="#i-loader-circle"/></svg>';
    try {
      await save(body, heading);
    } catch {
      // API 오류 알림 후 편집 내용을 그대로 두고 재시도를 허용한다.
      saving = false;
      controls.forEach(control => { control.disabled = false; });
      button.textContent = '저장';
      button.style.width = '';
      button.removeAttribute('aria-label');
      button.removeAttribute('title');
      button.removeAttribute('aria-busy');
    }
  });
  box.querySelector('textarea').focus();
}

// ---- 새 이슈 ----
async function showPublishedNotice(it, {hash = `#/issue/${it.ref}`, initial = '이슈를 발행했어요.', prefix = '이슈를 발행했어요'} = {}){
  const manual = `${prefix} — Claude나 Codex에게 검토를 맡길 수 있어요.`;
  if(it.status !== 'backlog' || it.parent_ref || (it.labels || []).includes('goal')){
    toast(manual); return;
  }
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 800);
  try {
    // 보조 조회 실패는 발행 오류로 표시하지 않는다. 늦은 응답도 다른 화면의 알림을 덮지 않는다.
    const res = await fetch('/api/settings/auto', {
      headers: { 'X-Requested-With': 'dev' }, signal: controller.signal,
    });
    if(!res.ok) return;
    const settings = await res.json();
    if(typeof settings.auto_review !== 'boolean') return;
    if(location.hash === hash && !document.getElementById('toast').hidden &&
        document.getElementById('toast').textContent === initial){
      toast(settings.auto_review ? `${prefix} — 잠시 후 에이전트가 계획서를 작성해요.` : manual);
    }
  } catch { /* 발행 성공 안내를 유지한다. */ }
  finally { clearTimeout(timeout); }
}
async function renderNew(params){
  const hash = location.hash;
  const {types} = await api('GET', '/api/issue-types');
  if(location.hash !== hash) return;
  const parent = params.get('parent') || '';
  const proj = parent ? parent.split('-')[0] : currentProject();
  if(!projects.length){ view.innerHTML = '<div class="empty">먼저 <a href="#/projects">Projects</a>에서 프로젝트를 만들어 주세요.</div>'; return; }
  view.innerHTML = `<form class="form" id="new-form">
    <h2 style="margin:0">${parent ? `New task · <span class="ref">${esc(parent)}</span>` : 'New issue'}</h2>
    <div class="line"><label>Project<select id="n-project"${parent ? ' disabled' : ''}>${projects.filter(p => !p.archived).map(p =>
      `<option value="${esc(p.key)}"${p.key === proj ? ' selected' : ''}>${esc(p.key)} · ${esc(p.name)}</option>`).join('')}</select></label>
      <label hidden>Priority<select id="n-priority">${PRIORITIES.map(p => `<option${p === 'none' ? ' selected' : ''}>${p}</option>`).join('')}</select></label>
      <label>Status<select id="n-status"><option>backlog</option><option>triage</option></select></label></div>
    <label>Title<input id="n-title" maxlength="300" placeholder="비워 두면 이슈를 맡은 에이전트가 본문을 보고 지어요"></label>
    <fieldset class="attachment-input"><legend>Attachments</legend>
      <p class="dim" id="attachment-help">이미지·영상·오디오·Markdown·JSON 파일과 http(s) URL을 함께 첨부해요. 기본 제한은 파일당 25MiB, 총 10개·100MiB예요.</p>
      <label>파일 선택<input id="n-files" type="file" multiple accept=".png,.jpg,.jpeg,.gif,.webp,.mp4,.webm,.mp3,.wav,.ogg,.m4a,.md,.json" aria-describedby="attachment-help"></label>
      <div class="line"><label>URL<input id="n-url" type="url" placeholder="https://example.com/reference"></label><button type="button" id="add-url">URL 추가</button></div>
      <div id="n-attachments" aria-live="polite"></div><p id="attachment-status" role="status"></p>
    </fieldset>
    <label>Description (마크다운)<textarea id="n-body" style="min-height:260px"></textarea></label>
    <fieldset class="dep-picker"><legend>연관 이슈 (선행)</legend>
      <p class="dim">선택한 이슈가 Done이 된 뒤 이 이슈의 작업을 시작해요. 최대 10개까지 선택할 수 있어요.</p>
      <label for="n-dep-search">이슈 검색<input id="n-dep-search" type="search" placeholder="번호 또는 제목으로 검색" autocomplete="off"></label>
      <div id="n-dep-selected" class="dep-selected" aria-label="선택한 연관 이슈"></div>
      <div id="n-dep-list" class="dep-list" aria-label="연관 이슈 후보"></div>
      <p id="n-dep-status" class="dim" role="status"></p>
    </fieldset>
    ${typePicker(types)}<label>Labels (쉼표로)<input id="n-labels"></label>
    <div class="row-end form-actions"><a class="button" id="cancel-new" href="#/">취소</a><button class="primary" type="submit">발행</button></div>
  </form>`;
  const form = view.querySelector('#new-form'), entries = [];
  let departed = false, published = false, saving = false, nextImage = 0;
  const selectedDeps = new Set(), depSearch = form.querySelector('#n-dep-search');
  const depList = form.querySelector('#n-dep-list'), depSelected = form.querySelector('#n-dep-selected');
  const depStatus = form.querySelector('#n-dep-status');
  let depCandidates = [], depRequest = 0;
  const drawDeps = () => {
    const query = depSearch.value.trim().toLocaleLowerCase();
    depSelected.innerHTML = [...selectedDeps].map(ref => `<button type="button" class="dep-tag" data-dep-remove="${esc(ref)}" aria-label="${esc(ref)} 연관 이슈 제거">${esc(ref)} ×</button>`).join('');
    const matches = depCandidates.filter(it => `${it.ref} ${it.title}`.toLocaleLowerCase().includes(query));
    depList.innerHTML = matches.map(it => `<button type="button" class="dep-option" data-dep="${esc(it.ref)}" aria-pressed="${selectedDeps.has(it.ref)}"><span class="ref">${esc(it.ref)}</span><span>${esc(it.title || '(제목 없음)')}</span><span class="dim">${esc(it.status)}</span></button>`).join('') || '<p class="dim">선택할 이슈가 없어요.</p>';
    depSelected.querySelectorAll('[data-dep-remove]').forEach(b => b.onclick = () => { selectedDeps.delete(b.dataset.depRemove); drawDeps(); });
    depList.querySelectorAll('[data-dep]').forEach(b => b.onclick = () => {
      if(selectedDeps.has(b.dataset.dep)) selectedDeps.delete(b.dataset.dep);
      else if(selectedDeps.size < 10) selectedDeps.add(b.dataset.dep);
      else { depStatus.textContent = '연관 이슈는 최대 10개까지 선택할 수 있어요.'; return; }
      depStatus.textContent = ''; drawDeps();
    });
  };
  const loadDeps = async () => {
    const request = ++depRequest, project = form.querySelector('#n-project').value;
    depCandidates = [];
    depList.innerHTML = ''; depStatus.textContent = '연관 이슈를 불러오는 중…';
    try{
      const result = await api('GET', `/api/projects/${encodeURIComponent(project)}/dep-candidates?all_projects=true`, undefined, () => !departed && request === depRequest);
      if(departed || request !== depRequest) return;
      depCandidates = result.issues; depStatus.textContent = ''; drawDeps();
    }catch(err){
      if(departed || request !== depRequest) return;
      depStatus.innerHTML = `${esc(err.message)} <button type="button" id="n-dep-retry">다시 불러오기</button>`;
      depStatus.querySelector('button').onclick = loadDeps;
    }
  };
  form.querySelector('#n-project').onchange = loadDeps;
  depSearch.oninput = drawDeps;
  void loadDeps();
  const status = form.querySelector('#attachment-status'), list = form.querySelector('#n-attachments');
  const lightbox = makeLightbox();
  const releasePreview = e => { if(e.preview){ URL.revokeObjectURL(e.preview); e.preview = null; } };
  const remove = a => api('DELETE', `/api/attachments/${encodeURIComponent(a.id)}`, undefined, () => !departed).catch(() => {});
  const leave = () => {
    departed = true; window.removeEventListener('hashchange', leave);
    lightbox.destroy(); entries.forEach(releasePreview);
    if(!published && !saving) entries.forEach(e => { if(e.data) void remove(e.data); });
  };
  window.addEventListener('hashchange', leave);
  const draw = () => {
    list.innerHTML = entries.map((e,i) => `<div class="attachment-heading">${e.imageNum ? `<button type="button" class="attachment-thumb" data-preview="${i}" aria-label="[이미지 ${e.imageNum}] 크게 보기"><img src="${e.preview}" alt=""></button>` : ''}<span class="attachment-name">${e.imageNum ? `[이미지 ${e.imageNum}] ` : ''}${esc(e.name)} — ${esc(e.busy ? '업로드 중…' : e.error || '첨부 준비됐어요')}</span><span class="attachment-actions">${e.imageNum ? `<button type="button" data-insert="${i}">본문에 넣기</button>` : ''}${e.error ? `<button type="button" data-retry="${i}">재시도</button>` : ''}<button type="button" data-remove="${i}" ${e.busy || saving ? 'disabled' : ''}>제거</button></span></div>`).join('');
    form.querySelector('[type=submit]').disabled = saving || entries.some(e => e.busy || e.error);
    list.querySelectorAll('[data-preview]').forEach(b => b.onclick = () => {
      const e = entries[+b.dataset.preview];
      lightbox.open(e.preview, `[이미지 ${e.imageNum}] ${e.name}`, b);
    });
    list.querySelectorAll('[data-insert]').forEach(b => b.onclick = () => {
      const body = form.querySelector('#n-body'), tag = `[이미지 ${entries[+b.dataset.insert].imageNum}]`;
      const start = body.selectionStart, end = body.selectionEnd;
      body.setRangeText(tag, start, end, 'end'); body.focus();
    });
    list.querySelectorAll('[data-remove]').forEach(b => b.onclick = async () => {
      if(saving) return;
      const e = entries[+b.dataset.remove];
      if(e.busy) return;
      e.busy = true; draw();
      if(e.data){ try{ await api('DELETE', `/api/attachments/${encodeURIComponent(e.data.id)}`); }catch{ e.busy = false; draw(); return; } }
      releasePreview(e); entries.splice(entries.indexOf(e),1); draw();
    });
    list.querySelectorAll('[data-retry]').forEach(b => b.onclick = () => upload(entries[+b.dataset.retry]));
  };
  const upload = async e => {
    if(e.busy || saving) return;
    e.busy = true; e.error = ''; draw();
    try{
      const body = e.file ? new FormData() : {url:e.name};
      if(e.file) body.append('file',e.file);
      e.data = await api('POST', e.file ? '/api/attachments' : '/api/attachments/url', body, () => !departed);
      if(departed) void remove(e.data);
    }catch(err){ e.error = `${err.message} 다시 시도하거나 제거해 주세요.`; }
    finally{ e.busy = false; if(!departed) draw(); }
  };
  const add = (name,file) => {
    if(saving || entries.some(e => e.file ? file && e.name === name && e.file.size === file.size && e.file.lastModified === file.lastModified : !file && e.name === name)) return;
    const e = {name,file};
    if(file?.type.startsWith('image/')){ e.imageNum = ++nextImage; e.preview = URL.createObjectURL(file); }
    entries.push(e); void upload(e);
  };
  form.querySelector('#n-files').onchange = e => { [...e.target.files].forEach(f => add(f.name,f)); e.target.value = ''; };
  form.querySelector('#add-url').onclick = () => {
    const input = form.querySelector('#n-url');
    if(!/^https?:\/\//i.test(input.value.trim())){ status.textContent = 'http(s) URL을 입력해 주세요.'; input.focus(); return; }
    add(input.value.trim()); input.value = ''; status.textContent = '';
  };
  bindTypePicker(form);
  form.querySelector(parent ? '#n-body' : '#n-project').focus();
  view.querySelector('#new-form').addEventListener('submit', (e) => { e.preventDefault(); if(saving || entries.some(e => e.busy || e.error)) return; whileBusy(submitBtn(e), async () => {
    saving = true; draw(); status.textContent = '발행 중…';
    form.querySelectorAll('input, textarea, select, button').forEach(el => el.disabled = true);
    form.querySelector('#cancel-new').onclick = ev => ev.preventDefault();
    try{
    const imageMap = entries.filter(e => e.imageNum).map(e => `[이미지 ${e.imageNum}] = ${e.name.replace(/\s+/g, ' ')}`);
    const writtenBody = view.querySelector('#n-body').value;
    const it = await api('POST', '/api/issues', {
      project: view.querySelector('#n-project').value, title: view.querySelector('#n-title').value,
      body: imageMap.length ? `${writtenBody.trimEnd()}\n\n---\n첨부 이미지:\n${imageMap.join('\n')}` : writtenBody,
      priority: view.querySelector('#n-priority').value, status: view.querySelector('#n-status').value,
      attachment_ids: entries.map(e => e.data.id),
      blocked_by: [...selectedDeps],
      type_ids: selectedTypes(form),
      labels: view.querySelector('#n-labels').value.split(',').map(s => s.trim()).filter(Boolean), parent: parent || undefined,
    });
    published = true;
    if(departed) return;
    toast('이슈를 발행했어요.');
    location.hash = `#/issue/${it.ref}`;
    void showPublishedNotice(it);
    }catch(err){ if(departed) entries.forEach(e => { if(e.data) void remove(e.data); }); else status.textContent = `${err.message} 입력과 첨부를 유지했어요. 다시 발행해 주세요.`; }
    finally{ saving = false; if(!departed){ form.querySelectorAll('input, textarea, select, button').forEach(el => el.disabled = false); form.querySelector('#n-project').disabled = !!parent; form.querySelector('#cancel-new').onclick = null; draw(); } }
  }); });
}

let sharedLightbox = null;
function makeLightbox(){
  if(sharedLightbox) return sharedLightbox;
  const box = document.createElement('div');
  box.className = 'attachment-lightbox';
  box.setAttribute('role', 'dialog'); box.setAttribute('aria-modal', 'true');
  box.setAttribute('aria-label', '첨부 이미지 크게 보기'); box.hidden = true;
  box.innerHTML = '<div class="attachment-lightbox-content"><button type="button" class="attachment-lightbox-close" aria-label="닫기" title="닫기"><svg class="ico" aria-hidden="true"><use href="#i-x"/></svg></button><img alt=""><p></p></div>';
  document.body.append(box);
  const closeButton = box.querySelector('button');
  let returnEl = null;
  const close = () => {
    if(box.hidden) return;
    box.hidden = true;
    box.querySelector('img').removeAttribute('src');
    if(returnEl?.isConnected) returnEl.focus();
    returnEl = null;
  };
  closeButton.onclick = close;
  box.onclick = e => { if(e.target === box) close(); };
  const onKey = e => {
    if(box.hidden) return;
    if(e.key === 'Escape'){ e.preventDefault(); e.stopPropagation(); close(); }
    if(e.key === 'Tab'){ e.preventDefault(); closeButton.focus(); }
  };
  document.addEventListener('keydown', onKey);
  sharedLightbox = {
    get isOpen(){ return !box.hidden; },
    open(src, label, trigger){
      box.querySelector('img').src = src;
      box.querySelector('img').alt = label;
      box.querySelector('p').textContent = label;
      returnEl = trigger; box.hidden = false; closeButton.focus();
    },
    close,
    destroy(){ close(); document.removeEventListener('keydown', onKey); box.remove(); sharedLightbox = null; },
  };
  return sharedLightbox;
}

// ---- 빠른 발행 입력창(DEV-93) — 목록·Board에서 백틱으로 열고(우하단 FAB는 정식 발행 페이지 #/new 링크), Enter로 #/new를 거치지 않고 바로 발행한다 ----
const composer = document.getElementById('composer'), cmpBody = document.getElementById('cmp-body');
// 입력창이 커져도 발행 알림이 입력창과 겹치지 않도록 높이를 공유한다.
new ResizeObserver(() => document.documentElement.style.setProperty('--composer-height', `${composer.offsetHeight}px`)).observe(composer);
const cmpFiles = [];   // {name, file, busy, error, data, preview}
let cmpSaving = false;
function composerAllowed(){ const n = location.hash.replace(/^#\/?/, '').split(/[/?]/)[0]; return !n || n === 'board'; }
function cmpDraw(){
  document.getElementById('cmp-files').innerHTML = cmpFiles.map((f, i) => `<span class="cmp-chip${f.error ? ' bad' : ''}">${f.preview ? `<button type="button" class="cmp-thumb" data-cmp-preview="${i}" aria-label="${esc(f.name)} 크게 보기"><img src="${f.preview}" alt=""></button>` : ''}${esc(f.name)}${f.busy ? ' · 업로드 중…' : f.error ? ' · 실패' : ''}
    <button type="button" data-cmp-remove="${i}" title="첨부 제거" aria-label="${esc(f.name)} 첨부 제거"${f.busy || cmpSaving ? ' disabled' : ''}><svg class="ico" aria-hidden="true"><use href="#i-x"/></svg></button></span>`).join('');
  document.getElementById('cmp-send').disabled = cmpSaving || !cmpBody.value.trim() || cmpFiles.some(f => f.busy || f.error);
}
const cmpDelete = f => {
  if(f.preview){ URL.revokeObjectURL(f.preview); f.preview = null; }
  if(f.data) void api('DELETE', `/api/attachments/${encodeURIComponent(f.data.id)}`).catch(() => {});
};
async function openComposer(){
  composer.hidden = false; cmpBody.focus(); cmpDraw();
  const active = projects.filter(p => !p.archived), proj = document.getElementById('cmp-project'), agent = document.getElementById('cmp-agent');
  const want = [proj.value, currentProject()].find(k => active.some(p => p.key === k)) || active[0]?.key || '';
  proj.innerHTML = active.map(p => `<option value="${esc(p.key)}">${esc(p.key)} · ${esc(p.name)}</option>`).join('');
  proj.value = want;
  const keep = agent.value, list = (await loadAgents().catch(() => [...agentsById.values()])).filter(a => a.kind === 'agent');
  agent.innerHTML = '<option value="">Agent 자동</option>' + list.map(a => `<option value="${a.id}">${esc(a.name)}</option>`).join('');
  agent.value = list.some(a => String(a.id) === keep) ? keep : '';
}
// 닫아도 초안은 남긴다. 목록·Board를 떠날 때(discard)만 발행 안 된 첨부를 지운다.
function closeComposer(discard){
  sharedLightbox?.close();
  composer.hidden = true;
  if(discard && !cmpSaving){ cmpFiles.splice(0).forEach(cmpDelete); cmpDraw(); }
}
document.addEventListener('keydown', (e) => {
  if(e.key !== '`' || e.ctrlKey || e.altKey || e.metaKey || e.isComposing || sharedLightbox?.isOpen || !composerAllowed() || document.getElementById('shell').hidden) return;
  const t = e.target;
  if(t.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName)) return;   // 검색칸 등에서는 글자 그대로
  e.preventDefault();
  if(composer.hidden) void openComposer(); else closeComposer();   // 열린 상태에서 백틱(입력칸 밖)이면 닫는다(DEV-96)
});
// 전역 탭 단축키는 한글 입력 상태에서도 물리 키로 판별한다(DEV-224).
const NAV_KEYS = {KeyI:'issues', KeyT:'trends', KeyA:'agents', KeyP:'projects', KeyS:'settings'};
document.addEventListener('keydown', (e) => {
  const nav = NAV_KEYS[e.code];
  if(!nav || e.ctrlKey || e.altKey || e.metaKey || e.repeat || e.isComposing || sharedLightbox?.isOpen || !composer.hidden || document.getElementById('shell').hidden) return;
  const t = e.target;
  if(t.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName)) return;
  if([...document.querySelectorAll('dialog[open], [aria-modal="true"]')].some(d => d.getClientRects().length && getComputedStyle(d).visibility !== 'hidden')) return;
  const tab = document.querySelector(`.tab-bar a[data-nav="${nav}"]`);
  if(tab){ e.preventDefault(); tab.click(); }
});
composer.addEventListener('keydown', (e) => { if(e.key === 'Escape'){ e.preventDefault(); e.stopPropagation(); closeComposer(); } });
cmpBody.addEventListener('keydown', (e) => {
  if(e.key !== 'Enter' || e.shiftKey) return;   // Shift+Enter는 줄바꿈
  if(e.isComposing || e.keyCode === 229) return;   // 한글 조합 중 Enter는 글자 확정만 — 보내면 마지막 글자가 두 번 들어간다
  e.preventDefault();
  cmpChainOnce = e.ctrlKey || e.metaKey;   // Ctrl+Enter는 토글과 무관하게 이번 한 번만 발행+검토+맨 위
  composer.requestSubmit();
});
// "발행+검토+맨 위" 토글 — 초안이 아니라 취향이라 localStorage에 기억한다(DEV-172-3)
let cmpChainOnce = false;
const cmpChainBtn = document.getElementById('cmp-chain');
const cmpChainOn = () => { try{ return localStorage.getItem('dev.cmpChain') === '1'; }catch(_){ return false; } };
const cmpChainDraw = () => cmpChainBtn.setAttribute('aria-pressed', String(cmpChainOn()));
cmpChainBtn.onclick = () => { try{ localStorage.setItem('dev.cmpChain', cmpChainOn() ? '0' : '1'); }catch(_){ /* 저장 불가면 이번 화면만 */ } cmpChainDraw(); };
cmpChainDraw();
// 발행한 이슈를 검토에 맡기고, 줄에 섰으면 맨 위로 보낸다. 실패해도 이슈는 backlog로 남는다.
async function cmpReviewTop(it){
  try{
    const r = await api('POST', `/api/issues/${it.ref}/review`, {});
    if(!r?.queued) return toast(`${it.ref} 발행 · 검토를 맡겼어요(바로 시작)`);
    const { jobs } = await api('GET', '/api/jobs'), first = jobs[0]?.id;
    if(first != null && first !== r.job_id) await api('POST', `/api/jobs/${r.job_id}/move`, { direction: 'top', neighbor_id: first });
    toast(`${it.ref} 발행 · 검토 맡김 · 대기열 맨 위`);
  }catch(ex){ toast(`${it.ref} 발행은 됐고 검토 맡기기는 실패했어요 — 이슈 화면에서 다시 맡겨 주세요`); }
}
cmpBody.addEventListener('input', () => { cmpBody.style.height = 'auto'; cmpBody.style.height = cmpBody.scrollHeight + 'px'; cmpDraw(); });
document.getElementById('cmp-attach').onclick = () => document.getElementById('cmp-file').click();
document.getElementById('cmp-file').onchange = (e) => {
  [...e.target.files].forEach(async file => {
    const f = { name: file.name, file, busy: true, error: '', preview: file.type.startsWith('image/') ? URL.createObjectURL(file) : null };
    cmpFiles.push(f); cmpDraw();
    try{
      const body = new FormData(); body.append('file', file);
      f.data = await api('POST', '/api/attachments', body);
      if(!cmpFiles.includes(f)) cmpDelete(f);   // 올리는 사이 지웠거나 화면을 떠났다
    }catch(err){ f.error = err.message; document.getElementById('cmp-error').textContent = `${file.name}: ${err.message} 제거하고 다시 첨부해 주세요.`; }
    finally{ f.busy = false; cmpDraw(); }
  });
  e.target.value = '';
};
document.getElementById('cmp-files').onclick = (e) => {
  const preview = e.target.closest('[data-cmp-preview]');
  if(preview){
    const f = cmpFiles[+preview.dataset.cmpPreview];
    if(f?.preview) makeLightbox().open(f.preview, f.name, preview);
    return;
  }
  const b = e.target.closest('[data-cmp-remove]');
  if(!b || cmpSaving) return;
  const [f] = cmpFiles.splice(+b.dataset.cmpRemove, 1);
  if(f){ sharedLightbox?.close(); cmpDelete(f); }
  document.getElementById('cmp-error').textContent = ''; cmpDraw();
};
composer.addEventListener('submit', async (e) => {
  e.preventDefault();
  const err = document.getElementById('cmp-error');
  if(cmpSaving || !cmpBody.value.trim() || cmpFiles.some(f => f.busy || f.error)) return;
  const chain = cmpChainOnce || cmpChainOn(); cmpChainOnce = false;
  cmpSaving = true; cmpDraw(); err.textContent = '';
  try{
    const agent = document.getElementById('cmp-agent').value;
    const it = await api('POST', '/api/issues', { project: document.getElementById('cmp-project').value, title: '', body: cmpBody.value,
      status: 'backlog', attachment_ids: cmpFiles.map(f => f.data.id), assignee_agent_id: agent ? Number(agent) : undefined });
    cmpBody.value = ''; cmpBody.style.height = '';
    sharedLightbox?.close(); cmpFiles.splice(0).forEach(f => { if(f.preview) URL.revokeObjectURL(f.preview); });
    closeComposer();
    toast(`${it.ref} 이슈를 발행했어요.`);
    void showPublishedNotice(it, {hash: location.hash, initial: `${it.ref} 이슈를 발행했어요.`, prefix: `${it.ref} 이슈를 발행했어요`});
    if(chain) await cmpReviewTop(it);
    if(composerAllowed()) void route();   // 페이지는 그대로, 목록·Board만 다시 읽는다
  }catch(ex){ err.textContent = `${ex.message} 입력과 첨부를 유지했어요. 다시 보내 주세요.`; }
  finally{ cmpSaving = false; cmpDraw(); if(!composer.hidden) cmpBody.focus(); }
});

// ---- 에이전트 ----
let agentNewKey = null;
function managementHeader(title, tabs, back = ''){
  const header = document.getElementById('management-header').content.cloneNode(true);
  header.querySelector('h2').textContent = title;
  header.querySelector('nav').innerHTML = tabs;
  if(back) header.querySelector('h2').insertAdjacentHTML('beforebegin', back);
  const container = document.createElement('div');
  container.append(header);
  return container.innerHTML;
}
async function renderAgents(newKey, selectedTab){
  if(newKey) agentNewKey = newKey;
  newKey = agentNewKey;
  const params = new URLSearchParams(location.hash.split('?')[1] || '');
  const tab = selectedTab || (params.get('tab') === 'chat' ? 'chat' : 'execution');
  const hash = `#/agents?tab=${tab}`;
  history.replaceState(null, '', hash);
  const [list, catalog] = await Promise.all([loadAgents(), api('GET', '/api/model-catalog')]);
  if(location.hash !== hash) return;
  const executionAgents = list.filter(a => a.kind === 'agent'), chats = list.filter(a => a.kind === 'chat');
  const vendorOf = (id) => catalog.vendors.find(v => v.vendor === id);
  const inCatalog = (a) => !!vendorOf(a.vendor)?.models.some(m => m.id === a.model);
  const vendorOptions = (cur) => catalog.vendors.map(v => `<option value="${esc(v.vendor)}" ${v.vendor === cur ? 'selected' : ''}>${esc(v.name)}</option>`).join('');
  const modelOptions = (vendor, cur) => (vendorOf(vendor)?.models || []).map(m =>
    `<option value="${esc(m.id)}" ${m.id === cur ? 'selected' : ''}>${esc(m.name)}${m.note ? ` · ${esc(m.note)}` : ''}</option>`).join('');
  // 목록 밖 Agent는 지금 값을 고를 수 없는 첫 항목으로 보여 주고, 드롭다운으로 다시 고르면 목록 안으로 들어간다
  const outside = (label) => `<option value="" selected disabled>${esc(label || '—')} (목록 밖)</option>`;
  const first = catalog.vendors[0]?.vendor || '';
  view.innerHTML = `<section class="panel management-card agents-page">
    ${managementHeader('Agents', ['execution', 'chat'].map(t => `<a href="#/agents?tab=${t}" data-agent-tab="${t}" aria-current="${tab === t ? 'page' : 'false'}">${t === 'execution' ? '실행 Agent' : 'Chat 연결'}</a>`).join(''))}
    ${newKey ? `<div class="keybox"><b>${esc(newKey.name)}</b>의 API 키예요. 지금 한 번만 보여 드려요 — 접속 설정에 넣어 주세요.<br>
      <code id="key">${esc(newKey.key)}</code> <button id="copy-key">복사</button></div>` : ''}
    <section class="agents-section" ${tab !== 'execution' ? 'hidden' : ''} aria-labelledby="execution-agents-title"><h2 id="execution-agents-title">실행 Agent</h2>
    <form class="form panel" id="agent-form"><div class="line">
      <label>Name<input id="a-name" required placeholder="claude-main"></label>
      <label>Vendor<select id="a-vendor">${vendorOptions(first)}</select></label>
      <label>Model<select id="a-model">${modelOptions(first)}</select></label>
      <label class="actions"><button class="primary" type="submit">에이전트 추가</button></label></div></form>
    ${executionAgents.length ? `<table class="issues agents-table" id="execution-agents"><thead><tr><th>Name</th><th>Vendor · Model</th><th class="hide-m">Key</th><th class="hide-m">Last seen</th><th>Enabled</th><th></th></tr></thead><tbody>
      ${executionAgents.map(a => { const ok = inCatalog(a); return `<tr data-agent="${a.id}"><td>${esc(a.name)}${ok ? '' : ' <span class="dim off-catalog">목록 밖</span>'}</td>
        <td><div class="agent-model"><select data-vendor="${a.id}" aria-label="${esc(a.name)} Vendor">${vendorOf(a.vendor) ? '' : outside(a.vendor)}${vendorOptions(a.vendor)}</select>
          <select data-model="${a.id}" aria-label="${esc(a.name)} Model">${ok ? '' : outside(a.model)}${modelOptions(a.vendor, a.model)}</select></div></td>
        <td class="hide-m ref">${esc(a.key_prefix)}…</td><td class="hide-m dim">${a.last_seen_at ? fmtTime(a.last_seen_at) : '—'}</td>
        <td><button type="button" class="auto-switch" data-toggle="${a.id}" role="switch" aria-label="${esc(a.name)} Enabled" aria-checked="${!!a.enabled}"><span class="switch-track" aria-hidden="true"></span><span>${a.enabled ? '켜짐' : '꺼짐'}</span></button></td>
        <td class="agent-actions"><button data-rotate="${a.id}" class="ghost">키 재발급</button><button data-move-kind="chat" data-id="${a.id}" class="ghost">Chat으로 옮기기</button></td></tr>`; }).join('')}
      </tbody></table>` : '<div class="empty">등록된 에이전트가 없어요.</div>'}</section>
    <section class="agents-section" ${tab !== 'chat' ? 'hidden' : ''} aria-labelledby="chat-connections-title"><h2 id="chat-connections-title">Chat 연결</h2>
      <p class="dim">Claude·ChatGPT 채팅에서 dev MCP에 접속할 때 쓰는 키예요. 실행·Assignee에는 나오지 않아요.</p>
      <form class="form panel" id="chat-form"><div class="line"><label>Name<input id="chat-name" required placeholder="예: claude-chat"></label><label class="actions"><button type="submit">Chat 연결 추가</button></label></div></form>
      ${chats.length ? `<table class="issues agents-table" id="chat-connections"><thead><tr><th>Name</th><th class="hide-m">Key</th><th class="hide-m">Last seen</th><th>Enabled</th><th></th></tr></thead><tbody>
        ${chats.map(a => `<tr data-agent="${a.id}"><td>${esc(a.name)}</td><td class="hide-m ref">${esc(a.key_prefix)}…</td><td class="hide-m dim">${a.last_seen_at ? fmtTime(a.last_seen_at) : '—'}</td>
          <td><button type="button" class="auto-switch" data-toggle="${a.id}" role="switch" aria-label="${esc(a.name)} Enabled" aria-checked="${!!a.enabled}"><span class="switch-track" aria-hidden="true"></span><span>${a.enabled ? '켜짐' : '꺼짐'}</span></button></td>
          <td class="agent-actions"><button data-rotate="${a.id}" class="ghost">키 재발급</button><button data-move-kind="agent" data-id="${a.id}" class="ghost">Agent로 옮기기</button></td></tr>`).join('')}
      </tbody></table>` : '<div class="empty">등록된 Chat 연결이 없어요.</div>'}</section></section>`;
  const $ = (s) => view.querySelector(s);
  if(newKey) $('#copy-key').addEventListener('click', () => navigator.clipboard.writeText(newKey.key).then(() => toast('복사했어요')));
  $('#a-vendor').addEventListener('change', () => { $('#a-model').innerHTML = modelOptions($('#a-vendor').value); });
  $('#agent-form').addEventListener('submit', (e) => { e.preventDefault(); whileBusy(submitBtn(e), async () => {
    const r = await api('POST', '/api/agents', { name: $('#a-name').value, vendor: $('#a-vendor').value, model: $('#a-model').value });
    renderAgents({ name: r.agent.name, key: r.key });
  }); });
  $('#chat-form').addEventListener('submit', e => { e.preventDefault(); whileBusy(submitBtn(e), async () => {
    const r = await api('POST', '/api/agents', {name: $('#chat-name').value, kind: 'chat'});
    await renderAgents({name: r.agent.name, key: r.key});
  }); });
  view.querySelectorAll('[data-move-kind]').forEach(b => b.addEventListener('click', () => whileBusy(b, async () => {
    const r = await api('PATCH', `/api/agents/${b.dataset.id}`, {kind: b.dataset.moveKind});
    await renderAgents(undefined, b.dataset.moveKind === 'chat' ? 'chat' : 'execution');
    toast(`${b.dataset.moveKind === 'chat' ? 'Chat 연결' : '실행 Agent'}으로 옮겼어요.${r.cleared_assignees ? ` Assignee ${r.cleared_assignees}개를 해제했어요.` : ''}`);
  })));
  view.querySelectorAll('[data-toggle]').forEach(b => b.addEventListener('click', () => whileBusy(b, async () => {
    await api('PATCH', `/api/agents/${b.dataset.toggle}`, {enabled: b.getAttribute('aria-checked') !== 'true'});
    await renderAgents();
  })));
  // Vendor를 바꾸면 Model 목록만 갈아 끼우고, Model을 고를 때 둘을 같이 저장한다
  view.querySelectorAll('[data-vendor]').forEach(s => s.addEventListener('change', () => {
    view.querySelector(`[data-model="${s.dataset.vendor}"]`).innerHTML = `<option value="" selected disabled>Model 선택</option>${modelOptions(s.value)}`;
  }));
  view.querySelectorAll('[data-model]').forEach(s => s.addEventListener('change', async () => {
    const id = s.dataset.model, vendor = view.querySelector(`[data-vendor="${id}"]`).value;
    await api('PATCH', `/api/agents/${id}`, { vendor, model: s.value }).then(() => toast('모델을 바꿨어요')).catch(() => {}); renderAgents();
  }));
  view.querySelectorAll('[data-rotate]').forEach(b => b.addEventListener('click', async () => {
    const a = agentsById.get(Number(b.dataset.rotate));
    if(!await confirmDialog(`${a.name}의 키를 새로 만들까요?`, {description:'지금 키는 바로 못 쓰게 돼요.', okText:'새로 만들기', danger:true, focusCancel:true})) return;
    const r = await api('POST', `/api/agents/${a.id}/rotate`);
    renderAgents({ name: a.name, key: r.key });
  }));
}

// ---- 프로젝트 ----
function projectSuggestion(name){
  const latest = [...projects].sort((a, b) => (Date.parse(b.created_at) - Date.parse(a.created_at)) || b.id - a.id)[0];
  if(!latest || (!latest.repo_url?.trim() && !latest.local_path?.trim())) return null;
  if(!name.trim() || /[\\/<>:"|?*\x00-\x1f]/.test(name) || /[. ]$/.test(name) || /^(\.|\.\.|con|prn|aux|nul|com[1-9]|lpt[1-9])(\.|$)/i.test(name))
    throw new Error('이 이름은 마지막 경로 요소로 쓸 수 없어요. Repository와 Local path를 직접 입력해 주세요.');
  const replace = (value, repo) => {
    if(!value?.trim()) return '';
    const match = value.trim().match(/^(.*[\\/])?([^\\/]+)([\\/]*)$/);
    if(!match) return '';
    // URL의 호스트나 드라이브만 있는 값에서는 경로를 추정하지 않는다.
    if(repo && (!match[1] || /^\w+:\/\/$/.test(match[1]))) return '';
    if(!repo && /^[A-Za-z]:$/.test(match[2])) return '';
    return (match[1] || '') + name + (repo && match[2].endsWith('.git') ? '.git' : '') + match[3];
  };
  const fields = {repo_url: replace(latest.repo_url, true), local_path: replace(latest.local_path, false)};
  return fields.repo_url || fields.local_path ? fields : null;
}

// 서비스 도메인 셀 — http(s) 주소만 새 창 링크로, 값이 없거나 다른 스킴이면 링크 없이 보인다.
function projectDomainCell(domain){
  const d = String(domain || '').trim();
  if(!d) return '<span class="dim">—</span>';
  const href = /^https?:\/\//i.test(d) ? d : /^[a-z][a-z0-9+.-]*:/i.test(d) ? '' : `https://${d}`;
  return href ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(d)}</a>` : esc(d);
}

// 목록의 마지막 행에서 생성하고 개별 관리 화면에서 수정한다.
function renderProjects(editKey, projectTab = 'manage'){
  const ed = projects.find(p => p.key === editKey);
  if(!['manage', 'usage'].includes(projectTab)) projectTab = 'manage';
  history.replaceState(null, '', ed ? `#/projects?project=${encodeURIComponent(ed.key)}&tab=${projectTab}` : '#/projects');
  const projectForm = `
    <form class="form panel" id="project-form" ${ed && projectTab !== 'manage' ? 'hidden' : ''}>${ed ? '' : '<h2 id="project-create-title">프로젝트 만들기</h2>'}${ed ? `<b>${esc(ed.key)} 고치기</b>` : ''}<div class="line">
      <label style="flex:0 0 90px">Key<input id="p-key" required placeholder="NS" maxlength="10" value="${esc(ed ? ed.key : '')}"${ed ? ' disabled' : ''}></label>
      <label>Name<input id="p-name" required placeholder="nightshift" value="${esc(ed ? ed.name : '')}"></label>
      <label>Repository<input id="p-repo" placeholder="https://github.com/…" value="${esc(ed ? ed.repo_url : '')}"></label>
      <label>Local path<input id="p-path" placeholder="C:\\Users\\…\\Projects\\…" value="${esc(ed ? ed.local_path : '')}"></label>
      <div class="project-description"><div class="line"><label>서비스 도메인<input id="p-domain" maxlength="253" placeholder="예: app.lomebrote.com" value="${esc(ed?.service_domain || '')}"></label>
      <label class="project-port">로컬 포트<span class="project-port-controls"><input id="p-port" type="number" min="1" max="65535" step="1" placeholder="예: 8400" value="${ed?.service_port || ''}" aria-describedby="p-port-message"><button type="button" id="p-free-port">빈 포트</button></span><span id="p-port-message" role="status" aria-live="polite"></span></label>
      <label>PM2 앱 이름<input id="p-service-app" maxlength="80" placeholder="예: myapp (이미 실행 중이면 비워요)" value="${esc(ed?.service_app || '')}"></label><label>실행 파일 생성 방식<select id="p-service-provider">${[["auto","자동 · 작업 에이전트 우선순위"],["codex","Codex"],["claude","Claude"],["none","기존 파일만 사용 · 유료 호출 없음"]].map(([value,label]) => `<option value="${value}"${(ed?.service_prepare_provider || "auto") === value ? " selected" : ""}>${label}</option>`).join("")}</select></label></div><p class="dim">로컬 포트는 다른 서비스가 쓰지 않는 번호를 정하고 앱 실행 설정에도 같은 값을 사용해 주세요. 이 값을 저장해도 앱의 포트가 자동으로 바뀌지는 않아요.</p></div>
      <label class="project-description">프로젝트 설명<textarea id="p-description" maxlength="200000" placeholder="목적, 대상 사용자와 원하는 기능을 적어 주세요.">${esc(ed?.description || '')}</textarea></label>
      ${ed ? '' : `<details class="project-description project-spec"><summary>기획서로 시작</summary><p class="dim">기획서로 실행 가능한 최소 앱·로컬 Git·Issue·Task를 만들어요. 기본 화면과 health가 로컬에서 응답해야 초기 생성이 완료돼요. 선택한 에이전트의 유료 분석을 시작하며, 제품 기능 Task 실행은 별도 승인이 필요해요.</p><fieldset class="project-templates"><legend>앱 종류</legend><div id="p-templates" class="template-cards" aria-live="polite">앱 종류를 불러오고 있어요…</div></fieldset><fieldset id="p-template-options" hidden><legend>기본 기능</legend><div class="template-options"></div></fieldset><fieldset id="p-spec-source" class="spec-source"><legend>기획서</legend><div class="spec-modes"><label><input type="radio" name="p-spec-mode" value="file" checked>파일 첨부</label><label><input type="radio" name="p-spec-mode" value="text">직접 입력</label></div><div id="p-spec-file" class="spec-file"><input id="p-spec" type="file" accept=".md,text/markdown" aria-label="기획서 파일" aria-describedby="p-spec-hint"><button type="button" id="p-spec-load">편집기로 불러오기</button></div><div id="p-spec-editor" hidden><textarea id="p-spec-text" rows="14" spellcheck="false" aria-label="기획서 Markdown" aria-describedby="p-spec-size p-spec-error" placeholder="# 프로젝트 이름&#10;&#10;## 목적&#10;…"></textarea><p id="p-spec-size" class="dim spec-size" aria-live="polite"></p><div class="spec-draft"><span id="p-spec-draft" class="dim" role="status"></span><button type="button" id="p-spec-clear" class="ghost" hidden>초안 지우기</button></div></div><p id="p-spec-error" class="error" aria-live="polite"></p><p id="p-spec-hint" class="dim">UTF-8 Markdown · 최대 200,000바이트. 직접 입력한 초안은 이 브라우저에만 보관해요.</p></fieldset><div class="line"><label>Projects 하위 폴더명<input id="p-folder" placeholder="예: moai" maxlength="80"></label><label>분석 도구<select id="p-init-provider"><option value="codex">Codex</option><option value="claude">Claude</option></select></label></div><button type="button" id="p-initialize">초기 프로젝트 생성</button><p id="p-init-message" role="status" aria-live="polite"></p></details>`}
      <label class="actions">${ed ? '<button type="button" id="p-cancel">취소</button><button class="primary" type="submit">저장</button>'
        : '<button type="button" id="p-cancel">취소</button><button class="primary" type="submit">등록</button>'}</label></div><p id="project-error" class="error" role="alert"></p></form>
`;
  view.innerHTML = `
    <div class="projects-page panel management-card">
    ${managementHeader(ed ? `${ed.name} · ${ed.key}` : 'Projects', ed ? ['manage', 'usage'].map((t, i) => `<button type="button" data-project-tab="${t}" aria-current="${projectTab === t ? 'page' : 'false'}">${['관리', '사용량'][i]}</button>`).join('') : '<a href="#/projects" aria-current="page">프로젝트 목록</a>', ed ? '<a href="#/projects" class="management-back">프로젝트 목록으로</a>' : '')}
    ${ed ? projectForm : ''}
    ${ed ? '<section class="panel" id="project-initialization" hidden aria-live="polite"></section>' : ''}
    ${ed && projectTab === 'manage' && me?.kind === 'human' ? `<section class="panel project-restart" aria-labelledby="app-restart-title"><div class="audit-toolbar"><h3 id="app-restart-title">앱 재시작</h3><button type="button" id="app-restart-copy" disabled aria-label="앱 재시작 진단 복사"><svg class="ico" aria-hidden="true"><use href="#i-copy"/></svg> 진단 복사</button></div><p class="dim">저장된 PM2 앱을 재시작하고 로컬 접속을 확인해요. Dev 자체 재시작은 진행 중인 작업이 끝난 뒤 처리하며 화면이 자동으로 다시 연결돼요.</p><div class="line"><button type="button" id="app-restart-start" disabled>앱 재시작</button><button type="button" id="app-restart-refresh">상태 확인</button></div><div id="app-restart-state" role="status" aria-live="polite">재시작 설정을 확인하고 있어요…</div><p id="app-restart-feedback" class="dim" role="status" aria-live="polite"></p><textarea id="app-restart-manual" aria-label="직접 복사할 앱 재시작 진단" readonly hidden></textarea></section>` : ''}
    ${ed && projectTab === 'manage' ? `<section class="panel project-service"><h3>서비스 연결</h3><p class="dim">도메인과 포트를 저장한 뒤 연결해 주세요. 실행·접속 검사는 일반 코드로 처리해요. 실행 설정이 없을 때만 선택한 에이전트가 유료로 파일을 생성해요. 기존 파일만 사용에서는 에이전트를 호출하지 않아요. 앱 확인 후 DNS·공용 터널을 반영하고 터널을 재시작해요.</p><div class="line"><button type="button" id="p-service-connect" disabled>서비스 연결</button><button type="button" id="p-service-disconnect" disabled>연결 해제</button><button type="button" id="p-service-refresh">새로고침</button></div><section class="service-diagnostic" aria-labelledby="service-diagnostic-title"><div class="service-diagnostic-head"><h4 id="service-diagnostic-title">연결 진단</h4><button type="button" id="p-service-copy" title="복사하기" aria-label="연결 진단 전체 내용 복사하기"><svg class="ico" aria-hidden="true"><use href="#i-copy"/></svg></button></div><div id="p-service-status" role="status" aria-live="polite"></div><p id="p-service-copy-message" role="status" aria-live="polite" class="dim"></p><textarea id="p-service-copy-text" aria-label="직접 복사할 연결 진단 전체 내용" readonly hidden></textarea></section><p class="dim">연결 해제는 터널 경로만 제거해요. 앱과 DNS 레코드는 유지해요. 도메인을 바꾸면 이전 DNS 레코드는 Cloudflare에서 정리해 주세요.</p></section>` : ''}
    ${ed && projectTab === 'usage' ? `<section class="panel project-usage" aria-labelledby="usage-title"><div class="usage-toolbar"><h3 id="usage-title">사용량</h3><button type="button" id="usage-refresh">새로고침</button></div><p class="dim">에이전트 실행 시간은 병렬 실행도 각각 합산해요. 완료 경과 시간은 최초 시작부터 마지막 Done까지 대기·승인 대기를 포함해요.</p><div id="usage-body" aria-live="polite"></div></section>` : ''}
    ${!ed ? `<table class="issues projects-table"><thead><tr><th>Key</th><th>Name</th><th>서비스 도메인</th><th class="hide-m">Repository</th><th class="hide-m">Local path</th><th class="col-narrow">Archived</th><th class="col-narrow"></th></tr></thead><tbody>
      ${projects.map(p => `<tr class="row" data-project-row="${esc(p.key)}"><td class="ref">${esc(p.key)}</td><td><a class="project-title" data-project-link="${esc(p.key)}" href="#/projects?project=${encodeURIComponent(p.key)}&tab=manage">${esc(p.name)}</a><div class="sub"><span data-project-service="${esc(p.key)}">상태 확인 중…</span></div></td><td data-project-domain="${esc(p.key)}">${projectDomainCell(p.service_domain)}</td><td class="hide-m">${esc(p.repo_url)}</td>
        <td class="hide-m dim">${esc(p.local_path)}</td><td class="col-narrow"><input type="checkbox" data-archive="${esc(p.key)}" ${p.archived ? 'checked' : ''}></td>
        <td class="col-narrow"><button class="danger" data-delete-project="${esc(p.key)}" title="프로젝트 삭제" aria-label="${esc(p.name)} 프로젝트 삭제"><svg class="ico" aria-hidden="true"><use href="#i-trash"/></svg></button></td></tr>`).join('')}
      ${projects.length ? '' : '<tr><td colspan="7"><div class="empty">프로젝트가 없어요. 아래 Add New Project 버튼으로 등록해 주세요.</div></td></tr>'}
      <tr class="project-create-row"><td colspan="7"><button type="button" id="project-add" title="프로젝트 추가" aria-label="프로젝트 추가"><svg class="ico" aria-hidden="true"><use href="#i-plus"/></svg><span>Add New Project</span></button></td></tr></tbody></table><dialog id="project-dialog" aria-labelledby="project-create-title"><button type="button" id="project-close" title="닫기" aria-label="닫기"><svg class="ico" aria-hidden="true"><use href="#i-x"/></svg></button>${projectForm}</dialog>` : ''}</div>`;
  view.querySelectorAll('[data-project-row]').forEach(row => row.addEventListener('click', e => {
    if(e.target.closest('a, button, input, label, select, textarea')) return;
    location.hash = `#/projects?project=${encodeURIComponent(row.dataset.projectRow)}&tab=manage`;
  }));
  const val = (s) => view.querySelector(s).value;
  const form = view.querySelector('#project-form');
  const portInput = form.querySelector('#p-port'), freePort = form.querySelector('#p-free-port');
  const portMessage = form.querySelector('#p-port-message');
  let portTouched = false, portRevision = 0, portLookup = false;
  const invalidatePort = () => { portTouched = true; portRevision++; portMessage.textContent = ''; };
  portInput.addEventListener('input', invalidatePort);
  portInput.addEventListener('change', invalidatePort);
  freePort.onclick = async () => {
    if(saving || portLookup) return;
    portLookup = true; portTouched = true;
    const revision = ++portRevision;
    const locked = [...form.querySelectorAll('[type=submit], #p-initialize')].filter(el => !el.disabled);
    locked.forEach(el => { el.disabled = true; });
    freePort.disabled = true; freePort.textContent = '찾는 중…';
    portMessage.textContent = '서버의 빈 포트를 확인하고 있어요…';
    try{
      const result = await api('GET', '/api/projects/free-port');
      if(!form.isConnected || revision !== portRevision) return;
      portInput.value = result.port;
      portInput.dispatchEvent(new Event('input', {bubbles:true}));
      portInput.dispatchEvent(new Event('change', {bubbles:true}));
      portMessage.textContent = '빈 포트를 채웠어요. 저장해 주세요.';
    }catch(error){
      if(form.isConnected && revision === portRevision) portMessage.textContent = `${error.message} 직접 입력하거나 다시 시도해 주세요.`;
    }finally{
      portLookup = false;
      freePort.disabled = saving; freePort.textContent = '빈 포트';
      if(!saving) locked.forEach(el => { el.disabled = false; });
      syncInitialize();
    }
  };
  function serviceLabel(state){
    if(state.status === 'running') return state.phase || '연결 중';
    if(state.online === true) return '서비스 중';
    if(state.online === false) return '접속 확인 실패';
    return state.status === 'disconnected' ? '연결 해제' : '미연결';
  }
  if(!ed) projects.forEach(async project => {
    const target = view.querySelector(`[data-project-service="${project.key}"]`);
    try{
      const state = await api('GET', `/api/projects/${encodeURIComponent(project.key)}/service`);
      if(!target?.isConnected) return;
      target.className = 'status'; target.style.setProperty('--sc', `var(--s-${state.online ? 'done' : state.online === false ? 'changes_requested' : 'backlog'})`);
      target.textContent = serviceLabel(state); target.title = state.url;
    }catch(error){ if(target?.isConnected) target.textContent = '상태 확인 실패'; }
  });
  if(ed && projectTab === 'manage'){
    const section = view.querySelector('.project-service'), message = section.querySelector('#p-service-status');
    const connect = section.querySelector('#p-service-connect'), disconnect = section.querySelector('#p-service-disconnect');
    const copy = section.querySelector('#p-service-copy'), feedback = section.querySelector('#p-service-copy-message');
    const manual = section.querySelector('#p-service-copy-text');
    let diagnosticText = '', diagnosticVersion = 0;
    function paintDiagnostic(state = {}, notice = ''){
      const label = notice || serviceLabel(state);
      const rows = [label, state.phase !== label ? state.phase : '',
        state.observed_port ? `로컬 포트 ${state.observed_port}` : '',
        state.changed ? '저장한 도메인·포트는 아직 반영되지 않았어요.' : '',
        state.error, state.setup_error, state.url].filter(Boolean);
      const stepLabels = {done:'완료', running:'진행 중', failed:'실패', pending:'대기'};
      const steps = state.steps || [];
      diagnosticText = [...steps.map(step => `${step.number}. ${step.label} — ${stepLabels[step.status] || '대기'}`), ...rows].join('\n'); diagnosticVersion++;
      feedback.textContent = ''; manual.hidden = true; manual.value = diagnosticText;
      message.replaceChildren();
      rows.forEach((text, index) => {
        if(text === state.error && steps.some(step => step.status === 'failed')) return;
        const row = document.createElement('div'); row.className = 'service-diagnostic-row';
        if(index === 0){
          row.classList.add('status');
          const color = state.status === 'running' ? 'in_progress' : state.online === true ? 'done' : state.online === false || state.error || state.setup_error ? 'changes_requested' : 'backlog';
          row.style.setProperty('--sc', `var(--s-${color})`);
        }
        if(text === state.url){
          // 외부 주소는 표시하되 HTTP(S) 링크만 활성화한다.
          const link = document.createElement('a');
          try { const url = new URL(text); if(['http:', 'https:'].includes(url.protocol)) link.href = url.href; } catch {}
          link.target = '_blank'; link.rel = 'noopener'; link.textContent = text; row.append(link);
        } else row.textContent = text;
        message.append(row);
        if(index === 0 && steps.length){
          const list = document.createElement('ol'); list.className = 'service-steps'; list.setAttribute('aria-label', '최근 연결 시도의 단계별 결과');
          steps.forEach(step => {
            const item = document.createElement('li');
            const color = {done:'done', running:'in_progress', failed:'changes_requested', pending:'backlog'}[step.status] || 'backlog';
            item.style.setProperty('--step-color', `var(--s-${color})`);
            if(step.status === 'running') item.setAttribute('aria-current', 'step');
            const number = document.createElement('span'); number.className = 'service-step-number'; number.textContent = step.number;
            const line = document.createElement('div'); line.className = 'service-step-line';
            const title = document.createElement('span'); title.textContent = step.label;
            const badge = document.createElement('span'); badge.className = 'status'; badge.style.setProperty('--sc', `var(--s-${color})`); badge.textContent = stepLabels[step.status] || '대기';
            line.append(title, badge); item.append(number, line);
            if(step.error){ const error = document.createElement('p'); error.className = 'service-step-error'; error.textContent = step.error; item.append(error); }
            list.append(item);
          });
          message.append(list);
        }
      });
    }
    copy.onclick = async () => {
      const version = diagnosticVersion, text = diagnosticText;
      copy.disabled = true;
      try{
        if(!navigator.clipboard?.writeText) throw new Error('clipboard unavailable');
        await navigator.clipboard.writeText(text);
        if(section.isConnected && version === diagnosticVersion) feedback.textContent = '복사했어요';
      }catch{
        if(section.isConnected && version === diagnosticVersion){
          feedback.textContent = '복사하지 못했어요. 아래 내용을 선택해 직접 복사해 주세요.';
          manual.hidden = false; manual.focus(); manual.select();
        }
      }finally{ copy.disabled = false; }
    };
    paintDiagnostic({}, '연결 상태를 확인하고 있어요…');
    let serviceBusy = false, serviceTimer;
    async function refreshService(){
      clearTimeout(serviceTimer);
      try{
        const state = await api('GET', `/api/projects/${encodeURIComponent(ed.key)}/service`);
        if(!section.isConnected) return;
        serviceBusy = state.status === 'running';
        connect.disabled = serviceBusy || !!ed.archived || !ed.service_domain || !ed.service_port || !!state.setup_error;
        disconnect.disabled = serviceBusy || !state.managed || !!state.setup_error;
        connect.textContent = serviceBusy ? '처리 중…' : (state.managed ? '연결 변경 반영' : '서비스 연결');
        paintDiagnostic(state);
        if(!ed.service_port && !portTouched && !portLookup && !saving && state.observed_port && !portInput.value) portInput.value = state.observed_port;
        if(serviceBusy) serviceTimer = setTimeout(() => { if(section.isConnected) refreshService(); }, 2000);
      }catch(error){ if(section.isConnected){ paintDiagnostic({error:error.message}, '상태 확인 실패'); if(serviceBusy) serviceTimer = setTimeout(() => { if(section.isConnected) refreshService(); }, 3000); } }
    }
    async function serviceAction(action){
      if(serviceBusy) return;
      if(action === 'connect' && (form.querySelector('#p-domain').value.trim() !== ed.service_domain || Number(form.querySelector('#p-port').value || 0) !== ed.service_port || form.querySelector('#p-service-app').value.trim() !== ed.service_app || form.querySelector('#p-path').value !== ed.local_path || form.querySelector('#p-service-provider').value !== (ed.service_prepare_provider || 'auto'))){
        paintDiagnostic({}, '변경한 서비스 정보와 Local path를 먼저 저장해 주세요.'); return;
      }
      serviceBusy = true; connect.disabled = disconnect.disabled = true;
      paintDiagnostic({status:'running'}, action === 'connect' ? '서비스 연결을 시작하고 있어요…' : '연결 해제를 시작하고 있어요…');
      try{ await api('POST', `/api/projects/${encodeURIComponent(ed.key)}/service`, {action}); }
      catch(error){ if(section.isConnected){ paintDiagnostic({error:error.message}, '서비스 요청 실패'); connect.disabled = false; disconnect.disabled = false; } serviceBusy = false; return; }
      await refreshService();
    }
    connect.onclick = () => serviceAction('connect'); disconnect.onclick = () => serviceAction('disconnect');
    section.querySelector('#p-service-refresh').onclick = refreshService;
    refreshService();
  }
  let saving = false;
  let initRequest = null;
  let suggestion = null;
  const clearSuggestion = () => { suggestion = null; form.querySelector('#project-suggestion')?.remove(); };
  const dialog = view.querySelector('#project-dialog');
  const add = view.querySelector('#project-add');
  const close = view.querySelector('#project-close');
  const closeCreation = () => {
    if(saving || !dialog?.open) return;
    portRevision++; portTouched = false; portMessage.textContent = '';
    form.reset(); clearSuggestion(); initRequest = null;
    specSeq++; restoreSpecDraft();
    selectedTemplate = 'blank';
    Object.keys(selectedGenre).forEach(key => delete selectedGenre[key]);
    if(templateCatalog) templateCatalog.forEach(t => { templateValues[t.id] = Object.fromEntries(Object.entries(t.options).map(([k, o]) => [k, o.default])); });
    setCreationStep('pick', false);
    form.querySelectorAll('details').forEach(el => { el.open = false; });
    form.querySelector('#project-error').textContent = '';
    form.querySelector('#p-init-message').textContent = '';
    dialog.close(); add.focus();
  };
  if(dialog){
    add.onclick = () => { dialog.showModal(); setCreationStep('pick'); };
    close.onclick = closeCreation;
    dialog.addEventListener('cancel', e => { e.preventDefault(); closeCreation(); });
    dialog.addEventListener('keydown', e => {
      if(e.key !== 'Tab') return;
      const targets = [...dialog.querySelectorAll('button, input, textarea, select, summary, [tabindex="0"]')].filter(el => !el.disabled && el.getClientRects().length);
      const first = targets[0], last = targets.at(-1);
      if(e.shiftKey && document.activeElement === first){ e.preventDefault(); last.focus(); }
      else if(!e.shiftKey && document.activeElement === last){ e.preventDefault(); first.focus(); }
    });
    // 창 안 여백은 유지하고 실제 배경 클릭만 닫는다.
    dialog.addEventListener('click', e => {
      const rect = dialog.getBoundingClientRect();
      if(e.target === dialog && (e.clientX < rect.left || e.clientX > rect.right || e.clientY < rect.top || e.clientY > rect.bottom)) closeCreation();
    });
  }
  form.addEventListener('input', e => {
    clearSuggestion(); if(!saving) initRequest = null;
    if(specText && e.target === specText){ saveSpecDraft(); paintSpec(); }
  });
  const initialize = view.querySelector('#p-initialize');
  let selectedTemplate = 'blank', templateCatalog = null;
  const templateValues = {}, selectedGenre = {};
  const currentTemplate = () => templateCatalog?.find(t => t.id === selectedTemplate);
  const currentGenre = () => currentTemplate()?.genres?.find(g => g.id === selectedGenre[selectedTemplate]);
  const allowedOptions = () => Object.entries(currentTemplate().options).filter(([key]) => !currentGenre() || currentGenre().options.includes(key));
  let creationMode = 'pick';
  const SPEC_DRAFT = 'dev.projectSpecDraft', SPEC_MAX = 200000;
  const specText = form.querySelector('#p-spec-text'), specFile = form.querySelector('#p-spec'), specLoad = form.querySelector('#p-spec-load');
  let specMemory = '', specSeq = 0;
  try{ specMemory = localStorage.getItem(SPEC_DRAFT) || ''; }catch{}
  if(dialog){
    const fields = form.querySelector(':scope > .line'); fields.id = 'p-details';
    const spec = form.querySelector('.project-spec');
    const content = document.createElement('section'); content.className = spec.className;
    spec.querySelector('summary').remove(); content.append(...spec.childNodes); spec.replaceWith(content);
    const picker = document.createElement('section'); picker.id = 'p-pick-step';
    picker.innerHTML = '<h3>어떤 앱으로 시작할까요?</h3><button type="button" id="p-register-mode" class="ghost">기존 저장소 등록하기</button>';
    picker.lastChild.before(form.querySelector('.project-templates'));
    const heading = document.createElement('div'); heading.id = 'p-step-heading';
    heading.innerHTML = '<button type="button" id="p-step-back">템플릿 바꾸기</button><strong id="p-selected-template"></strong>';
    const genres = document.createElement('section'); genres.id = 'p-genre-step'; genres.hidden = true;
    genres.innerHTML = '<h3>어떤 웹게임인가요?</h3><div id="p-genres" class="template-cards"></div><button type="button" id="p-genre-back" class="ghost">템플릿 바꾸기</button>';
    genres.querySelector('#p-genre-back').onclick = () => setCreationStep('pick');
    fields.before(picker, genres, heading);
    const service = form.querySelector('#p-domain').closest('.project-description');
    const details = document.createElement('details'); details.className = 'project-description';
    details.innerHTML = '<summary>서비스 설정</summary>'; service.before(details); details.append(service);
    form.querySelector('#p-register-mode').onclick = () => setCreationStep('register');
    form.querySelector('#p-step-back').onclick = () => setCreationStep(creationMode === 'template' && currentTemplate()?.genres?.length ? 'genre' : 'pick');
    const actions = form.querySelector('.actions'); actions.lastElementChild.id = 'p-register';
    actions.append(initialize); initialize.classList.add('primary');
    content.querySelector('p.dim').textContent = '기획서로 최소 앱·공식 문서·Issue·Task를 만들고 실행 검사해요. 선택한 도구의 유료 분석을 시작하며, 기능 Task 실행은 별도 승인이 필요해요.';
    form.querySelectorAll('[name="p-spec-mode"]').forEach(radio => radio.onchange = () => { specSeq++; form.querySelector('#p-spec-error').textContent = ''; paintSpec(); });
    specFile.addEventListener('change', () => { specSeq++; form.querySelector('#p-spec-error').textContent = ''; paintSpec(); });
    specLoad.onclick = loadSpecFile;
    form.querySelector('#p-spec-clear').onclick = async () => {
      if(!await confirmDialog('기획서 초안을 지울까요?', {description:'이 브라우저에 보관한 초안도 함께 지워요. 되돌릴 수 없어요.', okText:'지우기', danger:true, focusCancel:true}) || saving) return;
      specSeq++; specText.value = ''; initRequest = null; saveSpecDraft(); paintSpec(); specText.focus();
    };
    restoreSpecDraft();
    setCreationStep('pick', false);
  }
  // 기획서 직접 입력(DEV-225) — 초안은 메모리와 이 브라우저 localStorage 한 칸에 두고, 생성 요청이 성공해야 지운다.
  function specMode(){ return form.querySelector('[name="p-spec-mode"]:checked')?.value || 'file'; }
  function specProblem(text = specText.value){
    const bytes = new TextEncoder().encode(text).length;
    if(text.isWellFormed && !text.isWellFormed()) return '짝이 맞지 않는 문자가 있어요. 해당 부분을 지우고 다시 입력해 주세요.';
    if(bytes > SPEC_MAX) return `200,000바이트를 ${(bytes - SPEC_MAX).toLocaleString()}바이트 넘었어요. 내용을 줄여 주세요.`;
    if(!text.replace(/^﻿/, '').trim()) return '기획서 내용을 입력해 주세요.';
    return '';
  }
  function syncInitialize(){
    // 카탈로그 로딩·포트 조회·저장·편집기 상태를 한곳에서 계산해 비동기 콜백이 버튼을 잘못 켜지 않게 한다.
    if(initialize) initialize.disabled = saving || portLookup || !templateCatalog || creationMode !== 'template' || (specMode() === 'text' && !!specProblem());
  }
  function paintSpec(){
    if(!specText) return syncInitialize();
    const text = specText.value, bytes = new TextEncoder().encode(text).length, chars = [...text].length;
    const editing = specMode() === 'text', active = creationMode === 'template' && !saving;
    form.querySelector('#p-spec-file').hidden = editing; form.querySelector('#p-spec-editor').hidden = !editing;
    specText.disabled = !active || !editing; specFile.disabled = !active || editing; specLoad.disabled = !active || editing || !specFile.files.length;
    form.querySelector('#p-spec-size').textContent = `${chars.toLocaleString()}자 · ${bytes.toLocaleString()} / 200,000바이트 · 남은 ${Math.max(0, SPEC_MAX - bytes).toLocaleString()}바이트${bytes > chars ? ' · 한글 등은 한 글자가 2~4바이트라 바이트 한도가 먼저 적용돼요.' : ''}`;
    if(editing){
      const problem = specProblem(text);
      form.querySelector('#p-spec-error').textContent = problem;
      specText.setAttribute('aria-invalid', problem ? 'true' : 'false');
    }
    form.querySelector('#p-spec-clear').hidden = !text;
    syncInitialize();
  }
  function saveSpecDraft(){
    specMemory = specText.value;
    const note = form.querySelector('#p-spec-draft');
    try{
      if(specMemory) localStorage.setItem(SPEC_DRAFT, specMemory); else localStorage.removeItem(SPEC_DRAFT);
      note.textContent = specMemory ? '초안을 이 브라우저에 보관했어요.' : '';
    }catch{ note.textContent = '초안을 이 브라우저에 보관하지 못했어요. 입력한 내용은 이 화면에서 유지돼요.'; }
  }
  function forgetSpecDraft(){
    specMemory = '';
    try{ localStorage.removeItem(SPEC_DRAFT); }catch{}
  }
  function restoreSpecDraft(){
    specText.value = specMemory;
    if(specMemory) form.querySelector('[name="p-spec-mode"][value="text"]').checked = true;
    form.querySelector('#p-spec-draft').textContent = specMemory ? '이전에 입력한 초안을 불러왔어요.' : '';
    form.querySelector('#p-spec-error').textContent = '';
    paintSpec();
  }
  async function loadSpecFile(){
    const file = specFile.files[0], seq = ++specSeq, error = form.querySelector('#p-spec-error');
    if(!file || saving) return;
    error.textContent = '';
    if(file.size > SPEC_MAX){ error.textContent = '기획서 파일은 200,000바이트 이내여야 해요.'; return; }
    let text;
    try{ text = new TextDecoder('utf-8', {fatal:true}).decode(await file.arrayBuffer()); }
    catch{ if(seq === specSeq) error.textContent = 'UTF-8로 읽을 수 없는 파일이에요. UTF-8 Markdown으로 저장해 다시 골라 주세요.'; return; }
    // 읽는 사이 파일·입력 방식·창이 바뀌었으면 지난 결과를 적용하지 않는다.
    const current = () => seq === specSeq && dialog.open && !saving && specFile.files[0] === file;
    if(!current()) return;
    if(specText.value && specText.value !== text && !await confirmDialog('편집기 내용을 파일 내용으로 바꿀까요?', {description:'지금 편집기에 있는 초안은 사라져요.', okText:'바꾸기', danger:true, focusCancel:true})) return;
    if(!current()) return;
    specText.value = text; form.querySelector('[name="p-spec-mode"][value="text"]').checked = true;
    initRequest = null; saveSpecDraft(); paintSpec();
    form.querySelector('#p-spec-draft').textContent = `${file.name}을 불러왔어요. 고친 뒤 생성할 수 있어요.`;
    specText.focus();
  }
  function setCreationStep(mode, focus = true){
    if(!dialog || saving) return;
    creationMode = mode; initRequest = null; clearSuggestion();
    const genre = mode === 'genre', picking = mode === 'pick', choosing = picking || genre, template = mode === 'template';
    form.querySelector('#p-pick-step').hidden = !picking;
    form.querySelector('#p-genre-step').hidden = !genre;
    if(genre) renderGenres();
    form.querySelector('#p-details').hidden = choosing;
    form.querySelector('#p-step-heading').hidden = choosing;
    form.querySelector('#p-step-back').textContent = template ? (currentTemplate()?.genres?.length ? '장르 바꾸기' : '템플릿 바꾸기') : '처음으로';
    form.querySelector('#p-selected-template').textContent = template ? [currentTemplate()?.name, currentGenre()?.name].filter(Boolean).join(' · ') : '기존 저장소 등록';
    for(const selector of ['#p-path', '#p-description']) form.querySelector(selector).closest('label').hidden = template;
    form.querySelector('.project-spec').hidden = !template;
    form.querySelector('#p-register').hidden = choosing || template;
    initialize.hidden = !template;
    form.querySelector('#project-error').textContent = '';
    if(templateCatalog && template) renderTemplateOptions();
    form.querySelectorAll('#p-details input, #p-details textarea, #p-details select, #p-details button').forEach(el => {
      el.disabled = choosing || !!el.closest('[hidden]') || (el === freePort && portLookup);
    });
    paintSpec();
    if(focus) (genre ? form.querySelector(`[data-genre-pick="${selectedGenre[selectedTemplate] || currentTemplate().default_genre}"]`) : picking ? form.querySelector(`[data-template-pick="${selectedTemplate}"]`) || form.querySelector('#p-register-mode') : form.querySelector('#p-key')).focus();
  }
  function renderGenres(){
    const cards = form.querySelector('#p-genres');
    cards.innerHTML = currentTemplate().genres.map(g => `<button type="button" class="template-card" data-genre-pick="${esc(g.id)}"><span><strong>${esc(g.name)}</strong><span class="dim">${esc(g.description)}</span><ul class="dim">${g.provides.map(item => `<li>${esc(item)}</li>`).join('')}</ul></span></button>`).join('');
    cards.querySelectorAll('[data-genre-pick]').forEach(button => button.onclick = () => {
      selectedGenre[selectedTemplate] = button.dataset.genrePick; setCreationStep('template');
    });
  }
  function renderTemplateOptions(){
    const section = view.querySelector('#p-template-options');
    const template = templateCatalog.find(t => t.id === selectedTemplate);
    section.hidden = !allowedOptions().length;
    section.querySelector('.template-options').innerHTML = allowedOptions().map(([key, option]) => `<label><input type="checkbox" data-template-option="${esc(key)}" ${templateValues[selectedTemplate][key] ? 'checked' : ''}>${esc(option.name)}</label>`).join('');
    section.querySelectorAll('[data-template-option]').forEach(input => input.onchange = () => {
      templateValues[selectedTemplate][input.dataset.templateOption] = input.checked;
      initRequest = null;
    });
  }
  async function loadTemplateCards(){
    initialize.disabled = true;
    const cards = view.querySelector('#p-templates');
    try{
      const catalog = await api('GET', '/api/projects/templates');
      if(!form.isConnected) return;
      if(!Array.isArray(catalog) || !catalog.some(t => t.id === 'blank')) throw new Error('앱 종류 목록을 확인해 주세요.');
      templateCatalog = catalog;
      catalog.forEach(t => { templateValues[t.id] = Object.fromEntries(Object.entries(t.options).map(([key, option]) => [key, option.default])); });
      cards.innerHTML = catalog.map(t => `<button type="button" class="template-card" data-template-pick="${esc(t.id)}"><span><strong>${esc(t.name)}</strong><span class="dim">${esc(t.description)}</span><ul class="dim">${(t.provides || []).map(item => `<li>${esc(item)}</li>`).join('')}</ul></span></button>`).join('');
      cards.querySelectorAll('[data-template-pick]').forEach(button => button.onclick = () => {
        selectedTemplate = button.dataset.templatePick; setCreationStep(currentTemplate().genres?.length ? 'genre' : 'template');
      });
      renderTemplateOptions();
      syncInitialize();
      if(dialog?.open && creationMode === 'pick') setCreationStep('pick');
    }catch(error){
      if(!form.isConnected) return;
      cards.textContent = `${error.message} `;
      const retry = document.createElement('button'); retry.type = 'button'; retry.textContent = '다시 불러오기';
      retry.onclick = loadTemplateCards; cards.append(retry);
    }
  }
  if(initialize) void loadTemplateCards();
  if(initialize) initialize.onclick = () => {
    if(form.reportValidity()) form.dispatchEvent(new SubmitEvent('submit', {bubbles:true, cancelable:true, submitter:initialize}));
  };
  form.addEventListener('submit', async e => {
    e.preventDefault();
    if(saving || portLookup || (dialog && !['template', 'register'].includes(creationMode))) return;
    saving = true;
    const button = submitBtn(e), cancel = view.querySelector('#p-cancel');
    const original = button.textContent;
    button.disabled = cancel.disabled = true;
    if(close) close.disabled = true;
    form.querySelectorAll('input, textarea, select, button').forEach(el => { if(!el.disabled){ el.dataset.savingDisabled = '1'; el.disabled = true; } });
    button.textContent = '저장 중…'; form.setAttribute('aria-busy', 'true');
    view.querySelector('#project-error').textContent = '';
    try{
      if(!ed && (creationMode === 'template' || initRequest)){
        if(!templateCatalog) throw new Error('앱 종류를 먼저 불러와 주세요.');
        if(!initRequest){
          // 직접 입력은 spec_markdown만, 파일 첨부는 attachment_id만 보낸다.
          const editing = specMode() === 'text', file = view.querySelector('#p-spec').files[0];
          if(editing){ const problem = specProblem(); if(problem) throw new Error(problem); }
          else {
            if(!file || !file.name.toLowerCase().endsWith('.md')) throw new Error('Markdown 기획서를 첨부해 주세요.');
            if(file.size > SPEC_MAX) throw new Error('기획서는 200KB 이내로 첨부해 주세요.');
          }
          const folder = val('#p-folder').trim();
          if(!folder) throw new Error('Projects 하위 폴더명을 입력해 주세요.');
          view.querySelector('#p-init-message').textContent = editing ? '생성 요청을 등록하고 있어요…' : '기획서를 업로드하고 생성 요청을 등록하고 있어요…';
          let source = {spec_markdown: specText.value};
          if(!editing){
            const data = new FormData(); data.append('file', file);
            const attachment = await api('POST', '/api/attachments', data);
            if(!form.isConnected) return;
            source = {attachment_id: attachment.id};
          }
          initRequest = {request_id: crypto.randomUUID(), ...source,
            template: selectedTemplate, ...(currentGenre() ? {genre: currentGenre().id} : {}),
            options: Object.fromEntries(allowedOptions().map(([key]) => [key, templateValues[selectedTemplate][key]])),
            key: val('#p-key'), name: val('#p-name'), repo_url: val('#p-repo'), folder, provider: val('#p-init-provider'), service_domain: val('#p-domain'), service_port: val('#p-port') || 0, service_app: val('#p-service-app'), service_prepare_provider: val('#p-service-provider')};
        }
        const result = await api('POST', '/api/projects/initialize', initRequest);
        // 요청이 등록된 순간 초안을 지운다 — 뒤의 목록 갱신이 실패해도 다시 보관하지 않는다.
        if('spec_markdown' in initRequest) forgetSpecDraft();
        await loadProjects();
        if(!form.isConnected) return;
        dialog?.close();
        renderProjects(result.key);
        toast('초기 프로젝트 생성을 시작했어요. 진행 상태를 확인해 주세요.');
        return;
      }
      let fields = { name: val('#p-name'), repo_url: val('#p-repo'), local_path: val('#p-path'), description: val('#p-description'), service_domain: val('#p-domain'), service_port: val('#p-port') || 0, service_app: val('#p-service-app'), service_prepare_provider: val('#p-service-provider') };
      if(!ed && !fields.repo_url.trim() && !fields.local_path.trim()){
        if(suggestion){
          if(e.submitter?.id !== 'suggest-accept') return;
          fields = {...fields, ...suggestion};
        }
        else {
          const proposed = projectSuggestion(fields.name);
          if(proposed){
            suggestion = proposed;
            const panel = document.createElement('section'); panel.id = 'project-suggestion';
            panel.style.overflowWrap = 'anywhere';
            panel.innerHTML = `<p>최근 생성한 프로젝트를 참고한 값이에요.</p><p>Repository: <span>${esc(proposed.repo_url || '(비어 있어요)')}</span></p><p>Local path: <span>${esc(proposed.local_path || '(비어 있어요)')}</span></p><div class="line"><button type="submit" id="suggest-accept">이 값으로 생성</button><button type="button" id="suggest-reject">직접 입력</button></div>`;
            form.append(panel);
            panel.querySelector('#suggest-reject').onclick = () => { clearSuggestion(); view.querySelector('#p-repo').focus(); };
            panel.querySelector('#suggest-accept').focus();
            return;
          }
        }
      }
      if(ed) await api('PATCH', `/api/projects/${ed.key}`, fields);
      else await api('POST', '/api/projects', { key: val('#p-key'), ...fields });
      await loadProjects();
      if(!form.isConnected) return;
      dialog?.close();
      renderProjects(ed?.key); if(!ed) view.querySelector('#project-add').focus();
      toast(ed ? '고쳤어요' : '만들었어요');
    }catch(error){
      if(form.isConnected) view.querySelector('#project-error').textContent = `${error.message} 입력을 확인하고 다시 시도해 주세요.`;
    }finally{
      saving = false;
      form.querySelectorAll('[data-saving-disabled]').forEach(el => { el.disabled = false; delete el.dataset.savingDisabled; });
      button.disabled = cancel.disabled = false; button.textContent = original;
      if(close) close.disabled = false;
      form.removeAttribute('aria-busy');
      if(form.isConnected) syncInitialize();
    }
  });
  if(ed){
    const panel = view.querySelector('#project-initialization');
    const refreshInitialization = async () => {
      if(!panel.isConnected) return;
      try{
        const result = await api('GET', `/api/projects/${encodeURIComponent(ed.key)}/initialization`);
        if(!panel.isConnected) return;
        panel.hidden = false;
        panel.innerHTML = `<h3>초기 프로젝트 생성</h3><p>${esc(result.phase)}</p><p class="dim">${esc(result.local_path)}</p>${result.error ? `<p class="error" role="alert">${esc(result.error)}</p>` : ''}${['queued', 'running', 'failed'].includes(result.status) ? '<button type="button" id="p-init-stop">초기 생성 취소</button>' : ''}${result.status === 'failed' ? '<button type="button" id="p-init-retry">다시 생성</button>' : ''}${result.status === 'cancelled' ? '<p class="dim">초기 생성을 취소했어요. 프로젝트 이름 변경이나 삭제가 가능해요. 이미 만든 파일은 보존해요.</p>' : ''}${result.refs.length ? `<p>${result.refs.map(ref => `<a href="#/issue/${esc(ref)}">${esc(ref)}</a>`).join(' · ')}</p><p class="dim">생성된 Plan과 Task는 초안이에요. 검토 후 승인하여 실행해 주세요.</p>` : ''}`;
        const stop = panel.querySelector('#p-init-stop');
        if(stop) stop.onclick = () => whileBusy(stop, async () => {
          await api('POST', `/api/projects/${encodeURIComponent(ed.key)}/initialization/cancel`);
          await refreshInitialization();
        });
        const retry = panel.querySelector('#p-init-retry');
        if(retry) retry.onclick = () => whileBusy(retry, async () => {
          await api('POST', `/api/projects/${encodeURIComponent(ed.key)}/initialization/retry`);
          await refreshInitialization();
        });
        if(['queued', 'running', 'cancelling'].includes(result.status)) setTimeout(refreshInitialization, 2000);
      }catch(error){
        if(panel.isConnected && !panel.hidden) panel.textContent = `${error.message} 새로고침해 주세요.`;
      }
    };
    refreshInitialization();
  }
  view.querySelector('#p-cancel').addEventListener('click', () => {
    if(saving) return;
    if(ed){ renderProjects(); view.querySelector('#project-add').focus(); }
    else closeCreation();
  });
  view.querySelectorAll('[data-project-tab]').forEach(b => b.onclick = () => {
    renderProjects(ed.key, b.dataset.projectTab);
    view.querySelector(`[data-project-tab="${b.dataset.projectTab}"]`).focus();
  });
  if(ed && projectTab === 'manage' && me?.kind === 'human') bindProjectSecrets(ed, bindProjectRestart(ed));
  if(ed && projectTab === 'usage') bindProjectUsage(ed);
  view.querySelectorAll('[data-delete-project]').forEach(b => b.addEventListener('click', () => whileBusy(b, async () => {
    const url = `/api/projects/${encodeURIComponent(b.dataset.deleteProject)}`;
    const state = await api('GET', `${url}/delete-check`);
    if(!state.can_delete){ toast(state.blockers.join(' ')); return; }
    if(state.confirmation_required && !await confirmDialog(`${state.project.key}를 지울까요?`, {description:`Issue·Task ${state.issue_count}개\n${state.warning}\n다른 프로젝트의 의존 연결 ${state.external_dependency_count}개도 제거돼요. 복구할 수 없어요.`, okText:'지우기', danger:true, focusCancel:true})) return;
    // 409 응답에서는 자동 재시도하지 않는다. 다시 눌러 새 내용을 확인한다.
    await api('DELETE', url, {confirmation_token: state.confirmation_required ? state.confirmation_token : null});
    if(currentProject() === b.dataset.deleteProject) localStorage.removeItem('dev.project');
    await loadProjects();
    if(b.isConnected) renderProjects();
    toast('프로젝트를 삭제했어요');
  })));
  view.querySelectorAll('[data-archive]').forEach(cb => cb.addEventListener('change', async () => {
    await api('PATCH', `/api/projects/${cb.dataset.archive}`, { archived: cb.checked }).catch(() => {});
    await loadProjects(); renderProjects();
  }));
}

function bindProjectRestart(project){
  const panel = view.querySelector('.project-restart');
  panel.querySelector('h3').textContent = '앱 재시작·배포';
  panel.querySelector('.line').insertAdjacentHTML('beforeend', '<button type="button" id="app-deploy-start" disabled>빌드 후 배포</button>');
  panel.querySelector('.line').insertAdjacentHTML('afterend', '<div id="app-restart-release"></div><details id="app-restart-log" hidden><summary>배포 로그</summary><p class="dim"></p><pre></pre></details>');
  for(const [id, text] of [['app-restart-start', '코드·산출물 그대로 프로세스만 재시작해요.'], ['app-deploy-start', '현재 HEAD 커밋을 빌드하고 산출물 교체·재시작·릴리스 확인을 진행해요.']]){
    const button = panel.querySelector('#' + id), action = document.createElement('div');
    action.className = 'restart-action'; button.before(action); action.append(button);
    const description = document.createElement('p'); description.className = 'dim'; description.textContent = text; action.append(description);
  }
  const deploy = panel.querySelector('#app-deploy-start'), release = panel.querySelector('#app-restart-release'), log = panel.querySelector('#app-restart-log');
  const start = panel.querySelector('#app-restart-start'), refresh = panel.querySelector('#app-restart-refresh');
  const state = panel.querySelector('#app-restart-state'), copy = panel.querySelector('#app-restart-copy');
  const feedback = panel.querySelector('#app-restart-feedback'), manual = panel.querySelector('#app-restart-manual');
  const url = `/api/projects/${encodeURIComponent(project.key)}/restart`;
  let data = {}, busy = false, loading = false, timer, retries = 0, diagnostic = '', pendingMode = 'restart';
  const active = () => ['waiting','restarting','checking'].includes(data.status);
  function paint(){
    start.disabled = busy || loading || !data.available || active();
    deploy.disabled = busy || loading || !data.deploy_available || active();
    const mode = busy ? pendingMode : data.mode;
    start.textContent = (busy || active()) && mode !== 'deploy' ? '재시작 처리 중…' : '앱 재시작';
    deploy.textContent = (busy || active()) && mode === 'deploy' ? '배포 처리 중…' : '빌드 후 배포';
    release.textContent = ['현재 운영 릴리스', ...(data.current_release || []).map(r => `${r.out} · ${(r.revision || '').slice(0,7)} · ${fmtTime(r.built_at)}`), data.current_release?.length ? '' : '빌드 기록이 없어요.', data.reported_revision ? `앱 revision: ${data.reported_revision}` : '', data.reported_web_revision ? `웹 revision: ${data.reported_web_revision}` : '', data.deploy_available ? '' : '빌드 선언과 앱 설정을 확인하면 빌드 후 배포를 사용할 수 있어요.'].filter(Boolean).join('\n');
    log.hidden = !data.log && !data.log_tail;
    log.querySelector('p').textContent = data.log || '';
    log.querySelector('pre').textContent = data.log_tail || '로그 내용이 없어요.';
    state.textContent = [data.target_app ? `PM2 앱 ${data.target_app}` : '', data.phase, data.error, data.setup_error].filter(Boolean).join('\n');
    if(active()) restartGraceUntil = Date.now() + 90000;
    else if(['complete','failed'].includes(data.status)) restartGraceUntil = 0;
    diagnostic = ['Dev 앱 재시작 진단', `진단 번호: DEV-RESTART-${project.key}-${data.id || '미생성'}`,
      `프로젝트: ${project.key}`, `앱: ${data.app || data.target_app || '미확인'}`, `상태: ${data.status || '미확인'}`,
      `단계: ${data.phase || '미확인'}`, `오류: ${data.error || data.setup_error || '없음'}`,
      `로컬 확인 주소: ${data.health_url || data.target_health_url || '미확인'}`, `요청: ${data.created_at || '미기록'}`,
      `갱신: ${data.updated_at || '미기록'}`, `모드: ${data.mode || 'restart'}`, release.textContent, `로그: ${data.log || '없음'}`, data.log_tail || ''].join('\n');
    manual.value = diagnostic; copy.disabled = !data.status;
  }
  async function request(method, mode = 'restart'){
    const response = await fetch(mode === 'deploy' ? url.replace(/restart$/, 'deploy') : url, {method, headers:{'X-Requested-With':'dev'}, cache:'no-store'});
    const body = await response.json().catch(() => null);
    if(!response.ok){ const error = new Error(body?.detail || `서버 응답 ${response.status}`); error.status = response.status; throw error; }
    return body;
  }
  async function load(){
    if(!panel.isConnected || loading) return;
    clearTimeout(timer); loading = true;
    try {
      const result = await request('GET');
      if(!panel.isConnected) return;
      const reloadAfterRestart = active() && data.self_restart && result.self_restart && result.id === data.id && result.status === 'complete';
      data = result; retries = 0; paint();
      if(reloadAfterRestart){ location.reload(); return; }
    } catch(error){
      if(!panel.isConnected) return;
      retries++;
      state.textContent = `서버 응답을 기다리고 있어요. ${error.message}`;
      if(active()) restartGraceUntil = Date.now() + 15000;
    } finally {
      loading = false;
      if(panel.isConnected){
        start.disabled = busy || !data.available || active();
        deploy.disabled = busy || !data.deploy_available || active();
        if((active() || retries) && retries < 60) timer = setTimeout(load, 2000);
      }
    }
  }
  const submit = async (mode) => {
    if(busy || loading || active() || !(mode === 'deploy' ? data.deploy_available : data.available)) return;
    pendingMode = mode;
    busy = true; restartGraceUntil = Date.now() + 90000; paint();
    try {
      const result = await request('POST', mode);
      if(panel.isConnected){ data = result; manual.hidden = true; feedback.textContent = mode === 'deploy' ? '빌드 후 배포를 요청했어요.' : '재시작을 요청했어요.'; paint(); }
    } catch(error){ if(panel.isConnected) feedback.textContent = `요청 상태를 확인하고 있어요. ${error.message}`; }
    finally { busy = false; if(panel.isConnected) await load(); }
  };
  start.onclick = () => submit('restart');
  deploy.onclick = () => submit('deploy');
  refresh.onclick = () => { retries = 0; load(); };
  copy.onclick = async () => {
    const text = diagnostic;
    try {
      if(!navigator.clipboard?.writeText) throw new Error();
      await navigator.clipboard.writeText(text);
      if(panel.isConnected) feedback.textContent = '진단 내용을 복사했어요.';
    } catch {
      if(panel.isConnected){ feedback.textContent = '아래 내용을 선택해 직접 복사해 주세요.'; manual.hidden = false; manual.value = text; manual.focus(); manual.select(); }
    }
  };
  load();
  return () => { retries = 0; load(); };
}

// 비밀값(DEV-239) — `.env.example` 키마다 값을 넣는다. 저장된 값은 응답에도 DOM에도 다시 넣지 않는다.
function bindProjectSecrets(project, reloadRestart){
  const anchor = view.querySelector('.project-restart');
  if(!anchor) return;
  anchor.insertAdjacentHTML('beforebegin', `<section class="panel project-secrets" aria-labelledby="secrets-title">
    <div class="audit-toolbar"><h3 id="secrets-title">비밀값</h3></div>
    <p class="dim">값은 이 프로젝트의 .env에만 저장돼요. 빈 칸은 바꾸지 않아요.</p>
    <div id="secrets-body" class="dim">불러오는 중…</div>
    <div class="line"><button type="button" id="secrets-save" class="primary" hidden>저장</button></div>
    <p id="secrets-result" role="status"></p></section>`);
  const panel = view.querySelector('.project-secrets'), body = panel.querySelector('#secrets-body');
  const save = panel.querySelector('#secrets-save'), result = panel.querySelector('#secrets-result');
  const url = `/api/projects/${encodeURIComponent(project.key)}/secrets`;
  let keys = [];
  function paint(){
    body.className = keys.length ? 'secrets-list' : 'dim';
    save.hidden = !keys.length;
    if(!keys.length){ body.textContent = '.env.example에 키가 없어요.'; return; }
    body.innerHTML = keys.map((k, i) => `<div class="secret-row">
      <label class="secret-name" for="secret-${i}">${esc(k.key)}</label>
      <span class="secret-state ${k.set ? 'is-set' : ''}">${k.set ? `설정됨${k.last4 ? ' ··' + esc(k.last4) : ''}` : '비어 있어요'}</span>
      <input id="secret-${i}" type="password" autocomplete="off" spellcheck="false" data-env-key="${esc(k.key)}" placeholder="${k.set ? '새 값으로 바꾸기' : '값 붙여 넣기'}">
      <span class="secret-actions">${k.set ? `<label class="secret-clear"><input type="checkbox" data-secret-clear="${esc(k.key)}"> 지우기</label>` : ''}${k.dev_available ? `<button type="button" data-secret-dev="${esc(k.key)}">dev 값 그대로 쓰기</button>` : ''}</span></div>`).join('');
    body.querySelectorAll('[data-secret-dev]').forEach(b => b.onclick = () => submit({[b.dataset.secretDev]: {from_dev: true}}, b));
  }
  async function submit(changes, button){
    if(!Object.keys(changes).length){ result.textContent = '바꿀 값을 입력해 주세요.'; return; }
    await whileBusy(button, async () => {
      result.textContent = '저장하고 있어요…';
      let data;
      try { data = await api('PUT', url, changes, () => panel.isConnected, {notifyErrors: false}); }
      catch(error){ if(panel.isConnected) result.textContent = `저장하지 못했어요. ${error.message}`; return; }
      if(!panel.isConnected) return;
      keys = data.keys || []; paint();
      const restart = data.restart || {};
      result.textContent = restart.status === 'complete' ? '반영됨 — 앱을 다시 띄웠어요.'
        : restart.status === 'failed' ? `저장됐어요. 재시작 실패: ${restart.error || '원인을 알 수 없어요.'}`
        : restart.status === 'skipped' || !restart.status ? '저장됐어요 — 재시작할 앱이 없어요.'
        : '저장됐어요. 앱 재시작을 진행하고 있어요 — 아래 앱 재시작 칸에서 이어 봐요.';
      if(restart.status && restart.status !== 'skipped' && reloadRestart) reloadRestart();
    });
  }
  save.onclick = () => {
    const changes = {};
    body.querySelectorAll('[data-env-key]').forEach(input => { if(input.value.trim()) changes[input.dataset.envKey] = {value: input.value}; });
    body.querySelectorAll('[data-secret-clear]:checked').forEach(cb => { changes[cb.dataset.secretClear] = {clear: true}; });
    submit(changes, save);
  };
  api('GET', url, undefined, () => panel.isConnected, {notifyErrors: false}).then(data => {
    if(!panel.isConnected) return;
    keys = data.keys || []; paint();
  }).catch(error => {
    if(!panel.isConnected) return;
    // dev 자신의 .env는 이 화면에서 바꾸지 않는다 — 패널을 숨긴다.
    if(error.status === 409 && error.message.includes('dev 자신')){ panel.remove(); return; }
    body.textContent = error.message;
  });
}

function bindProjectUsage(project){
  const panel = view.querySelector('.project-usage'), body = panel.querySelector('#usage-body');
  const refresh = panel.querySelector('#usage-refresh');
  let generation = 0, offset = 0;
  const number = n => Number(n).toLocaleString();
  const duration = formatDuration;
  const time = iso => iso ? `<time datetime="${esc(iso)}" title="${esc(new Date(iso).toLocaleString())}">${esc(fmtTime(iso))}</time>` : '<span class="dim">미계측</span>';
  const metric = (label, value, note) => `<div><dt>${label}</dt><dd>${value}</dd><p class="dim">${note}</p></div>`;
  const missing = row => [row.unmeasured_run_count ? `토큰 미계측 ${number(row.unmeasured_run_count)}회` : '',
    row.running_run_count ? `실행 중 ${number(row.running_run_count)}회` : '',
    row.unmeasured_duration_run_count ? `실행 시간 미계측 ${number(row.unmeasured_duration_run_count)}회` : ''].filter(Boolean).join(' · ');
  async function load(nextOffset = 0, focusId){
    const token = ++generation;
    const current = () => panel.isConnected && generation === token;
    refresh.disabled = true;
    body.setAttribute('aria-busy', 'true');
    body.innerHTML = '<div class="usage-loading" role="status">사용량을 불러오는 중이에요…<div class="usage-skeleton" aria-hidden="true"></div></div>';
    try {
      const data = await api('GET', `/api/projects/${encodeURIComponent(project.key)}/usage?limit=50&offset=${nextOffset}`, undefined, current);
      if(!current()) return;
      offset = data.offset;
      const s = data.summary;
      body.innerHTML = `<dl class="usage-summary">
        ${metric('에이전트 실행 시간', duration(s.agent_seconds), `${number(s.run_count)}회 실행 · ${s.unmeasured_duration_run_count ? `미계측 ${number(s.unmeasured_duration_run_count)}회` : '보존된 실행 기록 기준이에요'}`)}
         ${metric('측정된 토큰', tokenHtml(s.total_tokens), `입력 ${tokenHtml(s.input_tokens)} · 출력 ${tokenHtml(s.output_tokens)}${s.partial ? ' · 부분 집계예요' : ''}`)}
        ${metric('완료 경과 시간 합계', duration(s.completed_elapsed_seconds), `Done ${number(s.completed_issue_count)}개 · 경과 시간 미계측 ${number(s.unmeasured_completed_issue_count)}개`)}
      </dl><p class="usage-measurement">${esc(missing(s) || '모든 보존된 실행의 토큰과 실행 시간을 측정했어요.')}</p>
      <p class="dim usage-notes">${data.notes.map(esc).join(' ')} 부모 Issue와 Task는 자신의 직접 실행분만 표시해요. 현재 보존된 ${number(s.issue_count)}개 Issue 기준 · ${time(data.as_of)} 조회</p>
      ${data.issues.length ? `<table class="usage-table"><caption class="usage-caption">${esc(project.name)} Issue별 사용량</caption><thead><tr><th>Issue / 상태</th><th>최초 시작 / 마지막 Done</th><th>경과 시간</th><th>실행 시간</th><th>측정된 토큰</th></tr></thead><tbody>${data.issues.map(row => {
        const ongoing = !['done', 'closed'].includes(row.status);
        return `<tr><td data-label="Issue / 상태"><a class="usage-issue" href="#/issue/${encodeURIComponent(row.ref)}"><span class="ref">${esc(row.ref)}</span><span>${esc(row.title)}</span></a>${statusHtml(row.status)}</td>
        <td data-label="최초 시작 / 마지막 Done"><div>시작 ${time(row.first_started_at)}</div><div>마지막 Done ${time(row.last_done_at)}</div></td>
        <td data-label="경과 시간"><div>${ongoing ? '진행 경과' : row.status === 'done' ? '완료 경과' : '과거 Done 경과'} ${duration(ongoing ? row.ongoing_elapsed_seconds : row.elapsed_seconds)}</div>${ongoing && row.last_done_at ? `<small class="dim">과거 Done 경과 ${duration(row.elapsed_seconds)}</small>` : ''}</td>
        <td data-label="실행 시간">${duration(row.agent_seconds)}<small class="dim">${number(row.run_count)}회${row.unmeasured_duration_run_count ? ` · 미계측 ${number(row.unmeasured_duration_run_count)}회` : ''}</small></td>
        <td data-label="측정된 토큰"><div>${tokenHtml(row.total_tokens)}${row.partial ? ' · 부분 집계' : ''}</div><small class="dim">입력 ${tokenHtml(row.input_tokens)} · 출력 ${tokenHtml(row.output_tokens)}</small><small class="dim">${esc(missing(row))}</small></td></tr>`;
      }).join('')}</tbody></table><div class="usage-pagination"><button type="button" id="usage-prev" ${offset === 0 ? 'disabled' : ''}>이전</button><span>${number(offset + 1)}–${number(offset + data.issues.length)} / ${number(data.total)}개</span><button type="button" id="usage-next" ${data.has_more ? '' : 'disabled'}>다음</button></div>` : '<div class="empty">아직 Issue가 없어요. Issue를 만들면 사용량을 여기서 확인할 수 있어요.</div>'}`;
      body.querySelector('#usage-prev')?.addEventListener('click', () => load(Math.max(0, offset - data.limit), 'usage-prev'));
      body.querySelector('#usage-next')?.addEventListener('click', () => load(offset + data.limit, 'usage-next'));
    } catch(e){
      if(current()) body.innerHTML = '<div class="empty" role="alert">사용량을 불러오지 못했어요. 새로고침으로 다시 시도해 주세요.</div>';
    } finally {
      if(current()){
        body.setAttribute('aria-busy', 'false'); refresh.disabled = false;
        if(focusId) (body.querySelector(`#${focusId}:not(:disabled)`) || refresh).focus();
      }
    }
  }
  refresh.onclick = () => load(offset);
  load();
}

// ---- 시작 ----
async function boot(){
  const res = await fetch('/api/auth/me');
  const data = await res.json();
  if(!data.actor || data.actor.kind !== 'human'){ showLogin(data.reason); return; }
  me = data.actor;
  document.getElementById('open-jupyter').hidden = false;
  document.getElementById('user-chip-name').textContent = me.name;
  document.getElementById('open-nightshift').href = data.nightshift_url;
  await Promise.all([loadProjects(), loadAgents(), loadTokenSettings()]);
  document.getElementById('login').hidden = true;
  document.getElementById('shell').hidden = false;
  startRestartPolling();
  route();
}
boot();


// ---- 프로젝트별 자동화 설정 — 이전 화면의 응답은 새 화면에 반영하지 않는다. ----
let settingsSession = null;
async function loadTokenSettings(){
  try{ tokenUnit = (await api('GET', '/api/settings/tokens', undefined, () => !!me, {notifyErrors:false})).unit; }
  catch(e){ tokenUnit = 'auto'; }
}
async function renderTokenSettings(){
  const session = {};
  settingsSession = session;
  const current = () => settingsSession === session && location.hash.split('?')[0] === '#/settings/tokens';
  view.innerHTML = `<section class="panel management-card token-settings">${settingsHeader('tokens')}<h3>토큰 표시 단위</h3><p class="dim">모든 화면의 토큰 수치에 적용해요. 원본 수치는 숫자 위에 커서를 올리면 볼 수 있어요.</p><div class="setting-row"><label for="token-unit">표시 단위</label><select id="token-unit"><option value="auto">자동</option><option value="k">k</option><option value="M">M</option><option value="full">전체 숫자</option></select></div><p id="token-status" role="status" aria-live="polite"></p></section>`;
  const select = view.querySelector('#token-unit'), status = view.querySelector('#token-status');
  select.value = tokenUnit;
  select.onchange = async () => {
    const previous = tokenUnit, next = select.value;
    select.disabled = true;
    try{
      const saved = await api('PUT', '/api/settings/tokens', {unit:next}, current, {notifyErrors:false});
      if(!current()) return;
      tokenUnit = saved.unit;
      status.textContent = '저장했어요. 다른 화면에도 바로 적용돼요.';
    }catch(e){ if(current()){ select.value = previous; status.textContent = `${e.message} 이전 설정을 유지했어요.`; } }
    finally{ if(current()) select.disabled = false; }
  };
}
// [설정 필드, 목록 id, 제목, 설명, 실행에 쓰이는지] — 오케스트레이터는 저장만 하고 아직 실행에 쓰지 않는다.
const ROLE_CARDS = [
  ['orchestrator_provider_order','orchestrator-order','오케스트레이터','준비 중인 역할이에요. 순서는 저장되지만 아직 실행되지 않아요.',false],
  ['provider_order','provider-order','작업 에이전트','Auto 검토·실행을 이 순서로 맡겨요.',true],
];
// 공통 제목과 서브탭을 설정 카드 안에 함께 그린다.
function settingsHeader(tab){
  const header = document.getElementById('settings-header').content.cloneNode(true);
  header.querySelectorAll('[data-settings]').forEach(a => {
    if(a.dataset.settings === tab){ a.classList.add('active'); a.setAttribute('aria-current', 'page'); }
  });
  const container = document.createElement('header');
  container.className = 'management-header';
  container.append(header);
  return container.outerHTML;
}
function renderFontSettings(){
  const fonts = window.DevFonts;
  const selected = fonts.get();
  const options = language => fonts.catalog[language].map(o => `<option value="${o.id}"${selected[language] === o.id ? ' selected' : ''}>${esc(o.label)}</option>`).join('');
  view.innerHTML = `<section class="panel management-card font-settings">${settingsHeader('fonts')}
    <h3>글꼴</h3><p class="dim" id="font-help">이 브라우저에 저장해요. 프로젝트를 바꿔도 유지돼요. 선택하면 바로 적용돼요.</p>
    <div class="font-controls"><label for="font-english">영문<select id="font-english" aria-describedby="font-help">${options('english')}</select></label>
    <label for="font-korean">한글<select id="font-korean" aria-describedby="font-help font-apple-help">${options('korean')}</select></label></div>
    <p class="dim" id="font-apple-help">Apple 글꼴은 설치된 기기에서 사용해요. 없으면 기존 글꼴로 표시돼요. 기기와 브라우저에 따라 모양이 달라질 수 있어요.</p>
    <section class="font-preview" aria-labelledby="font-preview-title"><h3 id="font-preview-title">미리보기</h3>
    <p class="font-preview-mono">ISSUE DEV-107 · PLAN V1 · 2026</p>
    <p class="font-preview-sans">Hello, 안녕하세요. Issue와 Task를 읽고 작업해요. 0123456789</p>
    <p class="font-preview-mono">TASK 작업 상태 · ㄱㄴㄷ · 가 · ꥠힰ</p>
    <label for="font-sample">입력 미리보기<input id="font-sample" value="Dev 프로젝트 · Apple 글꼴 123" autocomplete="off"></label></section>
    <button type="button" id="font-reset">기본값으로 되돌리기</button><p id="font-status" role="status" aria-live="polite"></p></section>`;
  const english = view.querySelector('#font-english'), korean = view.querySelector('#font-korean');
  const save = () => {
    const saved = fonts.save({english: english.value, korean: korean.value});
    view.querySelector('#font-status').textContent = saved ? '이 브라우저에 저장했어요.' : '지금 화면에 적용했지만 저장하지 못했어요. 브라우저 저장 설정을 확인하고 다시 선택해 주세요.';
  };
  english.addEventListener('change', save);
  korean.addEventListener('change', save);
  view.querySelector('#font-reset').addEventListener('click', () => {
    english.value = korean.value = 'default'; save();
  });
}
// 대기열 동시 실행 상한 — 모든 프로젝트 공통. 줄은 프로젝트마다 검토·실행 하나씩이고, 동시에 도는 줄 수를 이 값까지로 막는다.
async function renderQueueSettings(box, current){
  let s;
  try { s = await api('GET', '/api/settings/queue', undefined, current); }
  catch(e){ if(current()) box.innerHTML = `<p class="error" role="alert">${esc(e.message)}</p>`; return; }
  if(!current()) return;
  const field = (kind, label, value) => `<label class="queue-field">${label}<input type="number" class="queue-${kind}" min="1" max="${s.max}" step="1" value="${value ?? ''}" ${kind === 'total' ? 'id="queue-limit" required' : (value == null ? 'disabled' : '')} aria-describedby="queue-limit-help"></label>`
    + (kind === 'total' ? '' : `<label class="queue-unlimited"><input type="checkbox" class="queue-${kind}-free" ${value == null ? 'checked' : ''}>무제한</label>`);
  box.innerHTML = `<div class="setting-row"><div><b id="queue-limit-label">동시 실행 상한</b><p class="dim" id="queue-limit-help">대기열은 프로젝트마다 검토·실행 줄이 하나씩이에요. 전체·실행·검토 상한을 함께 지켜요(1~${s.max}). 낮춰도 이미 도는 작업은 끊지 않아요. 늘리면 홈서버 부하와 사용량 소모가 빨라져요.</p></div>
    <form class="queue-limit" aria-labelledby="queue-limit-label">${field('total', '전체', s.queue_concurrency)}${field('execute', '실행', s.execute)}${field('review', '검토', s.review)}<button type="submit">저장</button></form></div>
    <p id="queue-limit-busy" class="dim"></p>
    <p id="queue-limit-hint" class="dim"></p>
    <p id="queue-limit-status" class="dim" role="status" aria-live="polite"></p>`;
  const form = box.querySelector('form'), status = box.querySelector('#queue-limit-status');
  const q = sel => form.querySelector(sel);
  const readLimit = kind => q(`.queue-${kind}-free`).checked ? null : Number(q(`.queue-${kind}`).value);
  const hint = () => {
    const total = Number(q('.queue-total').value), ex = readLimit('execute'), rv = readLimit('review'), notes = [];
    if(Number.isInteger(total)){
      if((ex != null && ex > total) || (rv != null && rv > total)) notes.push(`전체 상한 ${total}개를 넘을 수 없어요.`);
      if(ex != null && rv != null && ex + rv < total) notes.push('전체 상한이 실제로는 검토+실행 합계로 묶여요.');
    }
    box.querySelector('#queue-limit-hint').textContent = notes.join(' ');
  };
  const paint = () => {
    for(const kind of ['execute', 'review']){ const free = q(`.queue-${kind}-free`), inp = q(`.queue-${kind}`); inp.disabled = free.checked; if(!free.checked && !inp.value) inp.value = s.queue_concurrency; }
    box.querySelector('#queue-limit-busy').textContent = `지금 바쁨: 실행 ${s.busy.execute}·검토 ${s.busy.review} / 전체 ${s.queue_concurrency}`;
    hint();
  };
  form.addEventListener('input', hint); form.addEventListener('change', paint);
  paint();
  const restore = () => { q('.queue-total').value = s.queue_concurrency; for(const kind of ['execute', 'review']){ q(`.queue-${kind}`).value = s[kind] ?? ''; q(`.queue-${kind}-free`).checked = s[kind] == null; } paint(); };
  form.onsubmit = async e => {
    e.preventDefault();
    const total = Number(q('.queue-total').value), execute = readLimit('execute'), review = readLimit('review'), button = form.querySelector('button');
    const bad = v => !Number.isInteger(v) || v < 1 || v > s.max;
    if(bad(total) || (execute != null && bad(execute)) || (review != null && bad(review))){ status.textContent = `1~${s.max} 사이의 정수로 입력해 주세요.`; return; }
    button.disabled = true; button.textContent = '저장 중…'; status.textContent = '';
    try{ s = await api('PUT', '/api/settings/queue', { queue_concurrency: total, execute, review }, current); if(current()){ paint(); status.textContent = `저장했어요 — 전체 ${s.queue_concurrency}개, 실행 ${s.execute ?? '무제한'}, 검토 ${s.review ?? '무제한'}.`; } }
    catch(err){ if(current()){ restore(); status.textContent = `${err.message} 이전 값으로 되돌렸어요.`; } }
    finally{ if(current()){ button.disabled = false; button.textContent = '저장'; } }
  };
}

async function renderSettings(){
  const session = {};
  settingsSession = session;
  const current = () => settingsSession === session && !document.getElementById('shell').hidden;
  view.innerHTML = `<section class="panel management-card auto-settings">${settingsHeader('auto')}<h3>대기열</h3><div id="queue-settings"></div><h3>Auto 위임 설정</h3><p class="dim">모든 프로젝트에 같은 위임 설정을 적용해요. 프로젝트를 바꿔도 설정은 같아요.</p><div id="settings-body"></div></section>`;
  void renderQueueSettings(view.querySelector('#queue-settings'), current);
  const body = view.querySelector('#settings-body');
  body.innerHTML = '<div class="settings-skeleton" aria-label="설정을 불러오는 중" aria-busy="true"></div>';
  let settings;
  const url = '/api/settings/auto';
  try { settings = await api('GET', url, undefined, current); }
  catch(e){ if(current()) body.innerHTML = `<p class="error" role="alert">${esc(e.message)} 다시 불러와 주세요.</p><button id="settings-retry" type="button">다시 불러오기</button>`;
    body.querySelector('#settings-retry')?.addEventListener('click', renderSettings); return; }
  if(!current()) return;
  const delegationFields = ['auto_review','auto_plan_approve','auto_execute','auto_approve'];
  body.innerHTML = `<div class="setting-row auto-master-row"><div><h3 id="auto-all-label">전체 · Auto</h3><p class="dim" id="auto-all-help">검토·계획 승인·실행·태스크 승인 네 위임을 함께 켜고 꺼요. 유료 호출과 자동 승인이 발생할 수 있어요.</p><p class="dim" id="auto-all-hint"></p></div><button type="button" class="auto-switch" id="auto-all" role="switch" aria-labelledby="auto-all-label" aria-describedby="auto-all-help auto-all-hint" aria-checked="false"><span class="switch-track" aria-hidden="true"></span><span class="switch-state">꺼짐</span></button></div>
    ${[['auto_review','Auto 검토 위임','사람이 등록한 Plan 없는 최상위 Backlog Issue를 검토해요. Goal과 기존 작업이 있는 Issue는 제외해요.'],
       ['auto_plan_approve','Auto 계획 승인','Manual 중 완료된 Plan도 포함해 성공한 최신 검토의 유효한 Plan을 구조 검사 후 승인해요. 의미적·기술적 오류 검출은 보장하지 않으며, 사람의 기존 결정과 Goal은 유지해요.'],
       ['auto_execute','Auto 실행 위임','최신 부모 Plan이 승인되고 선행 조건을 충족한 Backlog Task를 실행해요. Auto 태스크 승인만 ON이면 결과 승인만 해요.'],
       ['auto_approve','Auto 태스크 승인','In Review Task의 결과를 자동 승인하여 완료해요. 최신 부모 Plan의 사람 승인이 필요하며, 자동 병합이 켜져 있으면 병합·배포 성공을 기다려요. Goal과 상위 Issue는 완료하지 않아요.']].map(([field,label,help]) =>
      `<div class="setting-row"><div><b id="${field}-label">${label}</b><p class="dim" id="${field}-help">${esc(help)}</p></div><button type="button" class="auto-switch" id="${field}" role="switch" aria-labelledby="${field}-label" aria-describedby="${field}-help" aria-checked="false"><span class="switch-track" aria-hidden="true"></span><span class="switch-state">꺼짐</span></button></div>`).join('')}
    <h3>Auto 에이전트 우선순위</h3>
    <div class="role-cards">${ROLE_CARDS.map(([field,id,title,help,ready]) =>
      `<section class="role-card" aria-labelledby="${id}-title"><h4 id="${id}-title">${title}${ready ? '' : ' <span class="role-pending">준비 중</span>'}</h4><p class="dim">${esc(help)}</p><ol class="provider-order" id="${id}" data-field="${field}" aria-label="${title} 우선순위"></ol></section>`).join('')}</div>
    <p id="settings-status" role="status" aria-live="polite"></p>
    <aside class="settings-policy dim"><p>Auto를 켜면 클릭 없이 유료 검토·실행이 발생해요. Claude의 기존 비용 상한과 Codex의 시간 제한을 유지해요. Codex에는 금액 상한이 없어요.</p><p>Auto 계획 승인을 OFF로 바꾸면 이후 계획 승인만 중단해요. Auto 태스크 승인을 OFF로 바꾸면 이후 결과 승인만 중단해요. 완료된 Task·실행은 되돌리지 않아요. Auto 검토·실행을 OFF로 바꾸면 해당 자동 등록된 미착수 대기만 취소해요. 수동 대기와 실행 중 작업은 유지해요.</p><p>기존 auto_merge 정책은 별도로 적용돼요(기본 켜짐). 켜져 있는 프로젝트는 실행 후 병합·서버 재시작까지 이어질 수 있어요. 이 설정은 병합·배포 권한을 확대하지 않아요.</p></aside>`;
  let busy = false;
  const status = body.querySelector('#settings-status');
  function paint(){
    const allOn = delegationFields.every(field => settings[field]);
    const someOn = delegationFields.some(field => settings[field]);
    const master = body.querySelector('#auto-all');
    master.setAttribute('aria-checked', String(allOn));
    master.disabled = busy || settings.auto_approve_available === false;
    master.querySelector('.switch-state').textContent = busy ? '저장 중…' : allOn ? '켜짐' : someOn ? '일부 켜짐' : '꺼짐';
    body.querySelector('#auto-all-hint').textContent = allOn ? '누르면 모두 꺼요.' : '누르면 모두 켜요.';
    for(const field of delegationFields){
      const b = body.querySelector(`#${field}`), on = settings[field];
      b.setAttribute('aria-checked', String(!!on)); b.disabled = busy || (field === 'auto_approve' && settings.auto_approve_available === false);
      b.querySelector('.switch-state').textContent = on ? '켜짐' : '꺼짐';
    }
    for(const [field,id,title] of ROLE_CARDS){
      // 켜진 카탈로그 Agent가 있으면 "이름 · Vendor · Model" 줄로, 없으면 예전처럼 provider 두 줄로 그린다.
      const agents = settings.agent_orders?.[field] || [], list = body.querySelector(`#${id}`);
      list.dataset.agents = agents.length ? '1' : '';
      if(agents.length){
        list.innerHTML = agents.map((a,i) => `<li><span>${esc(a.name)} · ${esc(a.vendor)} · ${esc(a.model)}</span><div><button type="button" data-move="${i}" data-direction="-1" aria-label="${title} ${esc(a.name)} 우선순위 올리기" ${busy || i === 0 ? 'disabled' : ''}>위로</button><button type="button" data-move="${i}" data-direction="1" aria-label="${title} ${esc(a.name)} 우선순위 내리기" ${busy || i === agents.length - 1 ? 'disabled' : ''}>아래로</button></div></li>`).join('');
        continue;
      }
      const order = settings[field] || ['claude','codex'];
      list.innerHTML = order.map((provider,i) => `<li><span>${provider === 'claude' ? 'Claude' : 'Codex'}</span><div><button type="button" data-move="${i}" data-direction="-1" aria-label="${title} ${esc(provider)} 우선순위 올리기" ${busy || i === 0 ? 'disabled' : ''}>위로</button><button type="button" data-move="${i}" data-direction="1" aria-label="${title} ${esc(provider)} 우선순위 내리기" ${busy || i === order.length - 1 ? 'disabled' : ''}>아래로</button></div></li>`).join('');
    }
    body.setAttribute('aria-busy', String(busy));
  }
  async function save(changes, focus, optimistic = changes){
    if(busy || !current()) return;
    const before = settings;
    settings = {...settings, ...optimistic}; busy = true; paint(); status.textContent = '저장 중…';
    try{ const result = await api('PATCH', url, changes, current); if(!current()) return; settings = result; status.textContent = '저장했어요.'; }
    catch(e){ if(!current()) return; settings = before; status.textContent = `${e.message} 이전 설정으로 복원했어요. 다시 시도해 주세요.`; }
    finally{ if(current()){ busy = false; paint(); body.querySelector(focus)?.focus(); } }
  }
  body.querySelector('#auto-all').onclick = () => {
    const on = !delegationFields.every(field => settings[field]);
    save(Object.fromEntries(delegationFields.map(field => [field, on])), '#auto-all');
  };
  body.querySelectorAll('.auto-switch:not(#auto-all):not(:disabled)').forEach(b => b.onclick = () => save({[b.id]: !settings[b.id]}, `#${b.id}`));
  body.querySelector('.role-cards').onclick = e => {
    const b = e.target.closest('[data-move]'); if(!b || b.disabled) return;
    const list = b.closest('ol'), field = list.dataset.field;
    const i = Number(b.dataset.move), j = i + Number(b.dataset.direction), focus = `#${list.id} [data-move="${j}"][data-direction="${-Number(b.dataset.direction)}"]`;
    if(list.dataset.agents){
      const agents = [...settings.agent_orders[field]];
      [agents[i], agents[j]] = [agents[j], agents[i]];
      save({agent_orders: {[field]: agents.map(a => a.id)}}, focus, {agent_orders: {...settings.agent_orders, [field]: agents}});
      return;
    }
    const order = [...settings[field]];
    [order[i], order[j]] = [order[j], order[i]];
    save({[field]: order}, focus);
  };
  paint();
}

// 종류는 Labels와 독립적으로 안정 ID를 사용한다.
function typesHtml(it, showUnclassified = true){
  if(!(it.types || []).length) return showUnclassified ? '<span class="dim">미분류</span>' : '';
  return `<span class="issue-types">${it.types.map(t => `<span class="type-badge" title="${t.source === 'agent' ? '자동 분류' : t.source === 'human' ? '사람 선택' : '문서 생성 요청'}${t.active ? '' : ' · 비활성'}">#${esc(t.name).replace(/\s+/g, '')}</span>`).join('')}</span>`;
}
function typePicker(types, selected = [], opts = {}){
  return `<fieldset class="type-picker"><legend>${esc(opts.legend || '종류 (복수 선택)')}${opts.automatic ? ' <span class="chip dim">자동</span>' : ''}</legend><div>${types.filter(t => t.active || selected.includes(t.id)).map(t => `<button type="button" data-type="${t.id}" aria-pressed="${selected.includes(t.id)}">${esc(t.name)}${t.active ? '' : ' · 비활성'}</button>`).join('')}</div>${opts.hint === false ? '' : '<p class="dim">비워 두면 검토를 맡길 때 Agent가 자동 분류해요.</p>'}</fieldset>`;
}
function bindTypePicker(root){
  root.querySelectorAll('[data-type]').forEach(b => b.onclick = () => b.setAttribute('aria-pressed', String(b.getAttribute('aria-pressed') !== 'true')));
}
function selectedTypes(root){ return [...root.querySelectorAll('[data-type][aria-pressed="true"]')].map(b => Number(b.dataset.type)); }
async function renderTypes(){
  if(me?.kind !== 'human'){ view.innerHTML = '<div class="empty">관리자만 종류를 관리할 수 있어요.</div>'; return; }
  const session = {};
  settingsSession = session;
  const current = () => settingsSession === session && !document.getElementById('shell').hidden;
  view.innerHTML = `<section class="panel management-card type-management">${settingsHeader('types')}<h3>이슈 종류 관리</h3><p class="dim">모든 프로젝트에서 함께 사용해요.</p><div class="settings-skeleton" aria-label="종류를 불러오는 중" aria-busy="true"></div></section>`;
  let types;
  try{ ({types} = await api('GET', '/api/issue-types?include_inactive=true', undefined, current)); }
  catch(e){
    if(current()){
      view.innerHTML = `<section class="panel management-card type-management">${settingsHeader('types')}<h3>이슈 종류 관리</h3><p class="error" role="alert">${esc(e.message)} 다시 불러와 주세요.</p><button id="types-retry" type="button">다시 불러오기</button></section>`;
      view.querySelector('#types-retry').onclick = renderTypes;
    }
    return;
  }
  if(!current()) return;
  view.innerHTML = `<section class="panel management-card type-management">${settingsHeader('types')}<h3>이슈 종류 관리</h3><p class="dim">모든 프로젝트에서 함께 사용해요. 비활성화해도 기존 Issue의 연결은 보존돼요.</p><form id="type-create"><label>새 종류 이름<input name="name" maxlength="100" required></label><button type="submit">추가</button></form><div>${types.map(t => `<form class="type-row" data-id="${t.id}"><label>종류 이름<input name="name" aria-label="${esc(t.name)} 이름" maxlength="100" required value="${esc(t.name)}"></label><button type="submit">이름 저장</button><button type="button" class="type-active auto-switch" role="switch" aria-label="${esc(t.name)} 활성" aria-checked="${t.active}"><span class="switch-track" aria-hidden="true"></span><span>${t.active ? '활성' : '비활성'}</span></button></form>`).join('')}</div><p id="type-status" role="status"></p></section>`;
  const root = view.querySelector('.type-management');
  async function save(form, method, url, data){
    const controls = [...form.querySelectorAll('input,button')];
    const focus = form.id === 'type-create' ? '#type-create input' : `.type-row[data-id="${form.dataset.id}"] ${document.activeElement?.classList.contains('type-active') ? '.type-active' : '[type=submit]'}`;
    if(form.getAttribute('aria-busy') === 'true') return;
    form.setAttribute('aria-busy', 'true'); controls.forEach(c => c.disabled = true);
    root.querySelector('#type-status').textContent = '저장 중…';
    try{
      await api(method, url, data, current);
      if(current() && root.isConnected){
        const refreshed = await renderTypes();
        if(refreshed && settingsSession === refreshed && !document.getElementById('shell').hidden){
          view.querySelector('#type-status').textContent = '저장했어요.';
          view.querySelector(focus)?.focus();
        }
      }
    }
    catch(e){ if(current() && root.isConnected) root.querySelector('#type-status').textContent = `${e.message} 다시 시도해 주세요.`; }
    finally{ form.setAttribute('aria-busy','false'); controls.forEach(c => c.disabled = false); }
  }
  root.querySelector('#type-create').onsubmit = e => { e.preventDefault(); save(e.target, 'POST', '/api/issue-types', {name:e.target.elements.name.value}); };
  root.querySelectorAll('.type-row').forEach(form => {
    form.onsubmit = e => { e.preventDefault(); save(form, 'PATCH', `/api/issue-types/${form.dataset.id}`, {name:form.elements.name.value}); };
    const b = form.querySelector('.type-active');
    b.onclick = () => save(form, 'PATCH', `/api/issue-types/${form.dataset.id}`, {active:b.getAttribute('aria-checked') !== 'true'});
  });
  return session;
}
