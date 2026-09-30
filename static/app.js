// dev 화면 — 해시 라우팅 한 파일. #/ 목록 · #/board 칸반 · #/issue/NS-1 상세 · #/new 새 이슈 · #/agents · #/projects
'use strict';

const STATUSES = ['backlog', 'triage', 'in_progress', 'in_review', 'changes_requested', 'on_hold', 'done', 'closed'];
const STATUS_LABEL = { backlog: 'Backlog', triage: 'Triage', in_progress: 'In Progress', in_review: 'In Review',
  changes_requested: 'Changes Requested', on_hold: 'On Hold', done: 'Done', closed: 'Closed' };
const PRIORITIES = ['urgent', 'high', 'medium', 'low', 'none'];
const OPEN_STATUSES = STATUSES.filter(s => s !== 'done' && s !== 'closed');

let me = null;
let projects = [];
let agentsById = new Map();
const view = document.getElementById('view');

// ---- 공용 ----
function esc(s){
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function toast(msg){
  const el = document.getElementById('toast');
  el.textContent = msg; el.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => { el.hidden = true; }, 3000);
}
async function api(method, url, body){
  const opt = { method, headers: { 'X-Requested-With': 'dev' } };
  if(body !== undefined){ opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const res = await fetch(url, opt);
  if(res.status === 401){ showLogin(); throw new Error('로그인이 필요해요.'); }
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if(!res.ok){ const msg = (data && data.detail) || `요청이 실패했어요(${res.status}).`; toast(msg); throw new Error(msg); }
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
function statusHtml(s){ return `<span class="status" style="--sc:var(--s-${esc(s)})">${esc(STATUS_LABEL[s] || s)}</span>`; }
function prioHtml(p){ return p && p !== 'none' ? `<span class="prio ${esc(p)}">${esc(p)}</span>` : ''; }
function approvalHtml(a){   // 계획서 결정 배지 — 새 판이 올라와 무효가 된 것은 흐리게
  if(!a) return '';
  if(a.stale) return `<span class="status dim" style="--sc:var(--s-in_progress)" title="새 계획서가 올라와 결정이 무효예요">결정 무효</span>`;
  return `<span class="status" style="--sc:var(--s-${VERDICT_COLOR[a.verdict]})">${VERDICT_LABEL[a.verdict]}</span>`;
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
    .replace(/\b([A-Z][A-Z0-9]{0,9}-\d+)\b/g, '<a href="#/issue/$1">$1</a>');
}

// ---- 로그인 ----
function showLogin(reason){
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

// ---- 사용자 칩 → 메뉴(nightshift 열기·로그아웃) ----
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

// ---- 라우팅 ----
async function route(){
  const h = location.hash.replace(/^#\/?/, '').split('?')[0];
  const [name, arg] = h.split('/');
  document.querySelectorAll('[data-nav]').forEach(a => a.classList.toggle('active', a.dataset.nav === (name || 'issues')));
  try{
    if(!name) await renderList();
    else if(name === 'board') await renderBoard();
    else if(name === 'issue' && arg) await renderIssue(decodeURIComponent(arg));
    else if(name === 'new') renderNew(new URLSearchParams(location.hash.split('?')[1] || ''));
    else if(name === 'agents') await renderAgents();
    else if(name === 'projects') renderProjects();
    else view.innerHTML = '<div class="empty">없는 화면이에요.</div>';
  }catch(e){ if(!view.innerHTML) view.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}
window.addEventListener('hashchange', route);

// ---- 목록 ----
function listState(){ return JSON.parse(localStorage.getItem('dev.list') || '{"statuses":[],"closed":false,"q":""}'); }
// 목록은 LIST_PAGE개씩 — 끝(#list-more)이 보이면 다음 묶음을 붙인다(DEV-7). 도구줄은 한 번만 그리고
// 필터·검색이 바뀌면 결과 칸만 처음부터 다시 받는다(검색 글칸의 포커스가 유지되게).
const LIST_PAGE = 50;
let listLoad = null;   // { seq, qs, offset, done, busy, seen }
function issueRowHtml(i){
  return `<tr class="row" data-ref="${esc(i.ref)}"><td class="ref">${esc(i.ref)}</td>
    <td class="title-cell">${titleHtml(i)} ${labelsHtml(i.labels)}${i.parent_id ? '<div class="sub">Task</div>' : ''}</td>
    <td>${statusHtml(i.status)} ${approvalHtml(i.approval)}</td><td class="hide-m">${prioHtml(i.priority)}</td>
    <td class="hide-m">${i.claimed_by ? esc(actorName(i.claimed_by)) : ''}</td><td class="hide-m dim">${fmtTime(i.updated_at)}</td></tr>`;
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
    <div id="list-body"></div><div id="list-more" class="list-more"></div>`;
  const save = (patch) => { localStorage.setItem('dev.list', JSON.stringify({ ...listState(), ...patch })); loadList(); };
  let t;
  view.querySelector('#q').addEventListener('input', (e) => { clearTimeout(t); t = setTimeout(() => save({ q: e.target.value }), 300); });
  view.querySelectorAll('[data-st]').forEach(ch => ch.addEventListener('click', () => {
    const s = ch.dataset.st, cur = listState().statuses;
    ch.classList.toggle('on', !cur.includes(s));
    save({ statuses: cur.includes(s) ? cur.filter(x => x !== s) : [...cur, s] });
  }));
  view.querySelector('#show-closed').addEventListener('change', (e) => save({ closed: e.target.checked }));
  view.querySelector('#only-approved').addEventListener('change', (e) => save({ approved: e.target.checked }));
  // 프로젝트 필터(DEV-11) — 탭 줄의 전역 필터와 같은 값(dev.project)을 쓴다. 여기서 바꾸면 그쪽도 따라간다.
  view.querySelector('#list-project').addEventListener('change', (e) => {
    localStorage.setItem('dev.project', e.target.value); projectSel.value = e.target.value; loadList();
  });
  view.querySelector('#list-body').addEventListener('click', (e) => {
    const r = e.target.closest('tr.row');
    if(r) location.hash = `#/issue/${r.dataset.ref}`;
  });
  new IntersectionObserver((entries) => { if(entries.some(en => en.isIntersecting)) loadMoreIssues(); }, { rootMargin: '400px' })
    .observe(view.querySelector('#list-more'));
  await loadList();
}
async function loadList(){
  const st = listState();
  const statuses = st.statuses.length ? st.statuses : (st.closed ? [] : OPEN_STATUSES);
  listLoad = { qs: { project: currentProject(), status: statuses.join(','), q: st.q, ...(st.approved ? { approved: 'true' } : {}) }, offset: 0, done: false, busy: false, seen: new Set() };
  view.querySelector('#list-body').innerHTML = '';
  await loadMoreIssues();
}
async function loadMoreIssues(){
  const L = listLoad, body = view.querySelector('#list-body'), more = view.querySelector('#list-more');
  if(!L || L.done || L.busy || !body) return;
  L.busy = true;
  more.textContent = L.offset ? '더 불러오는 중…' : '';
  let data;
  try{ data = await api('GET', '/api/issues?' + new URLSearchParams({ ...L.qs, limit: LIST_PAGE, offset: L.offset })); }
  catch(e){ L.busy = false; more.textContent = ''; return; }
  if(L !== listLoad) return;   // 그 사이 필터가 바뀌었다
  // 고친 순 정렬이라 사이에 바뀐 이슈가 두 번 올 수 있다 — 이미 그린 것은 건너뛴다.
  const items = data.issues.filter(i => !L.seen.has(i.ref));
  items.forEach(i => L.seen.add(i.ref));
  L.offset += data.issues.length;
  L.done = !data.has_more;   // 예전 서버(has_more 없음)면 한 번으로 끝
  L.busy = false;
  if(!body.querySelector('table, .empty')){
    body.innerHTML = items.length ? `<table class="issues"><thead><tr><th>ID</th><th>Title</th><th>Status</th><th class="hide-m">Priority</th>
      <th class="hide-m">Claimed</th><th class="hide-m">Updated</th></tr></thead><tbody></tbody></table>` : '<div class="empty">이슈가 없어요.</div>';
  }
  const tbody = body.querySelector('tbody');
  if(tbody) tbody.insertAdjacentHTML('beforeend', items.map(issueRowHtml).join(''));
  more.textContent = '';
  // 한 화면이 다 안 찼으면 바로 다음 묶음
  if(!L.done && more.getBoundingClientRect().top < window.innerHeight + 400) loadMoreIssues();
}

// ---- 칸반 ----
async function renderBoard(){
  const cols = STATUSES.filter(s => s !== 'closed');
  const items = (await api('GET', '/api/issues?' + new URLSearchParams({ project: currentProject(), status: cols.join(',') }))).issues;
  view.innerHTML = `<div class="kanban">${cols.map(s => {
    const mine = items.filter(i => i.status === s);
    return `<div class="col" data-col="${s}"><h3>${statusHtml(s)}<span class="ref">${mine.length}</span></h3><div class="cards">
      ${mine.map(i => `<div class="card" draggable="true" data-ref="${esc(i.ref)}"><div class="ref">${esc(i.ref)}${i.parent_id ? ' · Task' : ''}</div>
        <div class="t">${titleHtml(i)}</div><div class="meta">${prioHtml(i.priority)}${approvalHtml(i.approval)}${labelsHtml(i.labels)}
        ${i.claimed_by ? `<span>● ${esc(actorName(i.claimed_by))}</span>` : ''}</div></div>`).join('')}
    </div></div>`;
  }).join('')}</div>`;
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
function eventHtml(e){
  const who = actorHtml(e.actor, e.data && e.data.model);
  const when = `<span class="when" title="${esc(e.created_at)}">${fmtTime(e.created_at)}</span>`;
  const d = e.data || {};
  let what;
  if(e.kind === 'comment') return `<li class="comment">${who}${when}${md(e.body)}</li>`;
  if(e.kind === 'status') what = `상태 ${statusHtml(d.from)} → ${statusHtml(d.to)}`;
  else if(e.kind === 'plan') what = `Plan v${esc(d.version)}을(를) 올렸어요`;
  else if(e.kind === 'commit') what = `커밋 <code>${esc(String(d.sha).slice(0, 7))}</code>${d.repo ? ` (${esc(d.repo)})` : ''} ${esc(e.body)}`;
  else if(e.kind === 'claim') what = d.action === 'release' ? '작업을 놓았어요' : `작업을 잡았어요(${esc(d.minutes)}분)`;
  else if(e.kind === 'assign') what = d.agent_id ? `담당: ${esc(actorName('agent:' + d.agent_id))}` : '담당을 비웠어요';
  else if(e.kind === 'edit') what = `고침: ${esc((d.fields || []).join(', '))}`;
  else what = esc(e.kind);
  return `<li class="sys">${who}${when} · ${what}${e.kind === 'status' && e.body ? md(e.body) : ''}</li>`;
}
// ---- 계획서 결정(DEV-14): 승인 · 메모 붙여 승인 · 거절 ----
const VERDICT_LABEL = { approve: '승인됨', approve_notes: '조건부 승인됨', reject: '거절됨' };
const VERDICT_COLOR = { approve: 'done', approve_notes: 'in_review', reject: 'changes_requested' };
function decisionQuestions(body){   // 계획서의 "정해야 할 것" 절의 목록 항목만
  const m = /^#{1,6}[ \t]*(?:사람이 )?정해야 할 것.*$/m.exec(body);
  if(!m) return '';
  const rest = body.slice(m.index + m[0].length), next = rest.search(/^#{1,6}[ \t]/m);
  return (next < 0 ? rest : rest.slice(0, next)).split('\n').filter(l => /^\s*(\d+[.)]|[-*])\s/.test(l)).map(l => `> ${l.trim()}\n→ `).join('\n\n');
}
function decisionHtml(it){
  const a = it.approval;
  const state = !a ? '<span class="dim">아직 결정하지 않았어요.</span>'
    : a.stale ? `<span class="status" style="--sc:var(--s-in_progress)">v${a.plan_version} 결정은 무효</span> <span class="dim">— 새 계획서(v${it.plan.version})가 올라왔어요. 다시 결정해 주세요.</span>`
    : `<span class="status" style="--sc:var(--s-${VERDICT_COLOR[a.verdict]})">${VERDICT_LABEL[a.verdict]} · v${a.plan_version}</span> <span class="dim">${esc(actorName(a.actor))} · ${fmtTime(a.created_at)}</span>${a.note ? md(a.note) : ''}`;
  const busy = it.review_running;
  return `<div class="decision" id="decision"><div class="decision-state">${state}</div>
    ${busy ? '<div class="dim hint">검토가 도는 중이라 끝나면 결정할 수 있어요.</div>' : `
    <div id="decision-form" hidden><textarea id="decision-note"></textarea>
      <div class="row-end"><button id="decision-cancel">Cancel</button><button id="decision-send" class="primary"></button></div></div>
    <div class="review-actions" id="decision-actions">
      <button data-verdict="approve"${it.status === 'in_review' ? '' : ' class="primary"'}>승인</button>
      <button data-verdict="approve_notes">메모 붙여 승인</button>
      <button data-verdict="reject" class="danger">거절</button></div>`}</div>`;
}
// ---- 결과 결정(DEV-16): in_review에서 승인 → Done · 수정 요청 → Changes Requested · 거절 → Closed ----
const RESULT_FORM = {
  changes_requested: { placeholder: '무엇을 더 해야 하나요? (에이전트가 읽는 사람의 지시예요)', send: '수정 요청', cls: 'primary' },
  closed: { placeholder: '거절 이유 (예: 방향이 달라서 접기로 함)', send: '거절하고 닫기', cls: 'danger' },
};
function resultHtml(it){
  if(it.status !== 'in_review') return '';
  return `<div class="panel"><h2>Result <span class="meta">결과를 확인해 주세요</span></h2>
    <div class="decision" id="result"><div id="result-form" hidden><textarea id="result-note"></textarea>
      <div class="row-end"><button id="result-cancel">Cancel</button><button id="result-send"></button></div></div>
    <div class="review-actions" id="result-actions"><button id="approve" class="primary">승인 → Done</button>
      <button data-to="changes_requested" id="request-changes">수정 요청</button>
      <button data-to="closed" class="danger">거절</button></div></div></div>`;
}
async function renderIssue(ref){
  const [it] = await Promise.all([api('GET', `/api/issues/${encodeURIComponent(ref)}`), agentsById.size ? null : loadAgents()]);
  const agentOpts = ['<option value="">(없음)</option>'].concat([...agentsById.values()].map(a =>
    `<option value="${a.id}"${a.id === it.assignee_agent_id ? ' selected' : ''}>${esc(a.name)}</option>`)).join('');
  view.innerHTML = `
    <div class="detail">
      <div>
        <div class="ref">${esc(it.ref)}${it.parent_ref ? ` · Task of <a href="#/issue/${esc(it.parent_ref)}">${esc(it.parent_ref)}</a>` : ''}</div>
        <h1 id="title">${titleHtml(it)}</h1>
        <div class="byline">${actorHtml(it.reporter)}<span>·</span><span>${fmtTime(it.created_at)}</span>${labelsHtml(it.labels)}</div>
        <div class="panel"><h2>Description<span class="right"><button id="edit-body">Edit</button></span></h2>
          <div id="body">${it.body ? md(it.body) : '<p class="dim">본문이 없어요.</p>'}</div></div>
        <div class="panel"><h2>Plan${it.plan ? ` <span class="meta">v${it.plan.version} · ${esc(actorName(it.plan.author))} · ${fmtTime(it.plan.created_at)}</span>` : ''}
          <span class="right">${it.plan && it.plan.version > 1 ? '<button id="plan-history">History</button>' : ''}<button id="edit-plan">${it.plan ? 'Revise' : 'Write'}</button></span></h2>
          <div id="plan">${it.plan ? md(it.plan.body) : '<p class="dim">아직 계획서가 없어요.</p>'}</div>
          ${it.plan && !['done', 'closed'].includes(it.status) ? decisionHtml(it) : ''}</div>
        ${resultHtml(it)}
        <div class="panel"><h2>Tasks <span class="meta">${it.children.length}</span><span class="right"><a class="button" href="#/new?parent=${esc(it.ref)}">Add task</a></span></h2>
          ${it.children.length ? `<table class="issues">${it.children.map(ch => `<tr class="row" data-ref="${esc(ch.ref)}"><td class="ref">${esc(ch.ref)}</td>
            <td>${titleHtml(ch)}</td><td>${statusHtml(ch.status)}</td></tr>`).join('')}</table>` : '<p class="dim">하위 Task가 없어요.</p>'}</div>
        <div class="panel"><h2>Activity</h2><ul class="timeline">${it.events.map(eventHtml).join('') || '<li class="dim empty-line">아직 활동이 없어요.</li>'}</ul>
          <div class="comment-box"><textarea id="comment" placeholder="댓글(마크다운)"></textarea>
            <div class="row-end"><button id="send-comment" class="primary">Comment</button></div></div></div>
      </div>
      <aside class="side panel">
        <div class="field"><span>Status</span><select id="status">${STATUSES.map(s => `<option value="${s}"${s === it.status ? ' selected' : ''}>${STATUS_LABEL[s]}</option>`).join('')}</select></div>
        <div class="field"><span>Priority</span><select id="priority">${PRIORITIES.map(p => `<option${p === it.priority ? ' selected' : ''}>${p}</option>`).join('')}</select></div>
        <div class="field"><span>Assignee</span><select id="assignee">${agentOpts}</select></div>
        <div class="field"><span>Labels (쉼표로)</span><input id="labels" value="${esc(it.labels.join(', '))}"></div>
        <div class="field"><span>Claimed</span>${it.claimed_by ? `${esc(actorName(it.claimed_by))} <span class="dim">~${new Date(it.lease_until).toLocaleTimeString()}</span>
          <button id="release" class="ghost">놓기</button>` : '<span class="dim">없음</span>'}</div>
        <div class="field"><span>Commits</span>${it.events.filter(e => e.kind === 'commit').map(e => `<div><code>${esc(e.data.sha.slice(0, 7))}</code> <span class="dim">${esc(e.data.repo)}</span></div>`).join('') || '<span class="dim">없음</span>'}</div>
        <div class="field"><span>Claude</span>
          <button id="ask-review"${it.review_busy ? ' disabled' : ''}><svg class="ico"><use href="#i-bot"/></svg>${it.review_running ? '검토 중…' : 'Claude에게 검토 맡기기'}</button>
          <div class="dim hint">${it.review_busy && !it.review_running ? '다른 이슈를 검토하는 중이에요.' : '홈서버에서 검토만 해요 — 계획서·질문을 남겨요(코드 수정 없음).'}</div></div>
        <div class="field"><button id="delete" class="danger">Delete issue</button></div>
      </aside>
    </div>`;
  const R = encodeURIComponent(it.ref);
  const reload = () => renderIssue(it.ref);
  const $ = (id) => view.querySelector('#' + id);
  view.querySelectorAll('tr.row').forEach(r => r.addEventListener('click', () => { location.hash = `#/issue/${r.dataset.ref}`; }));
  // done/closed로 끝내면 목록으로 돌아간다(DEV-5) — 끝난 이슈 화면에 머물 일은 없다.
  const setStatus = async (status, note) => {
    try{ await api('POST', `/api/issues/${R}/status`, note === undefined ? { status } : { status, note }); }
    catch(e){ reload(); return; }
    if(status === 'done' || status === 'closed'){ toast(`${it.ref}을(를) 끝냈어요`); location.hash = '#/'; }
    else reload();
  };
  $('status').addEventListener('change', (e) => {
    if(e.target.value !== 'closed'){ setStatus(e.target.value); return; }
    // Closed는 "안 하기로 함" — 왜 닫는지 남긴다(DEV-6). 취소하거나 비우면 닫지 않는다.
    const reason = (prompt('닫는 이유를 적어 주세요 (예: NS-3과 중복, 필요 없어짐)') || '').trim();
    if(!reason){ e.target.value = it.status; toast('사유가 없어서 닫지 않았어요'); return; }
    setStatus('closed', reason);
  });
  $('priority').addEventListener('change', async (e) => { await api('PATCH', `/api/issues/${R}`, { priority: e.target.value }).catch(() => {}); reload(); });
  $('assignee').addEventListener('change', async (e) => { await api('PATCH', `/api/issues/${R}`, { assignee_agent_id: e.target.value ? Number(e.target.value) : null }).catch(() => {}); reload(); });
  $('labels').addEventListener('change', async (e) => {
    await api('PATCH', `/api/issues/${R}`, { labels: e.target.value.split(',').map(s => s.trim()).filter(Boolean) }).catch(() => {}); reload();
  });
  if($('approve')){
    $('approve').addEventListener('click', () => setStatus('done'));
    let to = null;
    $('result-actions').querySelectorAll('[data-to]').forEach(b => b.addEventListener('click', () => {
      to = b.dataset.to; const f = RESULT_FORM[to];
      $('result-form').hidden = false; $('result-actions').hidden = true;
      $('result-note').value = ''; $('result-note').placeholder = f.placeholder;
      $('result-send').textContent = f.send; $('result-send').className = f.cls; $('result-note').focus();
    }));
    $('result-cancel').addEventListener('click', () => { $('result-form').hidden = true; $('result-actions').hidden = false; });
    $('result-send').addEventListener('click', (e) => {
      const note = $('result-note').value.trim();
      if(!note){ toast('메모를 적어 주세요'); return; }
      whileBusy(e.currentTarget, () => setStatus(to, note));
    });
  }
  if($('decision-actions')){
    let verdict = null;
    const send = async (v, note) => { await api('POST', `/api/issues/${R}/decision`, { verdict: v, note, plan_version: it.plan.version });
      toast(v === 'reject' ? `${it.ref}을(를) 거절해서 닫았어요` : '결정을 남겼어요'); if(v === 'reject') location.hash = '#/'; else reload(); };
    $('decision-actions').querySelectorAll('button').forEach(b => b.addEventListener('click', (e) => {
      verdict = b.dataset.verdict;
      if(verdict === 'approve'){ whileBusy(e.currentTarget, () => send('approve', '')); return; }
      $('decision-form').hidden = false; $('decision-actions').hidden = true;
      $('decision-note').value = verdict === 'approve_notes' ? decisionQuestions(it.plan.body) : '';
      $('decision-note').placeholder = verdict === 'approve_notes' ? '계획서가 물은 것에 대한 답, 고칠 내용 (예: 1번은 끄기로)' : '거절 이유 (예: NS-3과 중복, 필요 없어짐)';
      $('decision-send').textContent = verdict === 'approve_notes' ? '메모 붙여 승인' : '거절하고 닫기';
      $('decision-send').classList.toggle('danger', verdict === 'reject'); $('decision-send').classList.toggle('primary', verdict !== 'reject');
      $('decision-note').focus();
    }));
    $('decision-cancel').addEventListener('click', () => { $('decision-form').hidden = true; $('decision-actions').hidden = false; });
    $('decision-send').addEventListener('click', (e) => whileBusy(e.currentTarget, () => send(verdict, $('decision-note').value)));
  }
  if($('release')) $('release').addEventListener('click', async () => { await api('POST', `/api/issues/${R}/release`).catch(() => {}); reload(); });
  // Claude에게 검토 맡기기(DEV-13) — 도는 동안은 15초마다 이 화면을 다시 불러 계획서가 올라오면 보이게
  $('ask-review').addEventListener('click', (e) => whileBusy(e.currentTarget, async () => {
    if(!confirm(`${it.ref}을(를) Claude에게 검토 맡길까요?
홈서버에서 Claude Code가 이슈와 코드를 읽고 계획서·질문을 남겨요(코드는 고치지 않아요).
사용량은 이 서버에 로그인된 Claude 계정에서 나가요.`)) return;
    await api('POST', `/api/issues/${R}/review`); toast('검토를 맡겼어요 — 몇 분 뒤 계획서가 올라와요'); reload();
  }));
  if(it.review_running) setTimeout(() => { if(location.hash === `#/issue/${it.ref}`) reload(); }, 15000);
  $('delete').addEventListener('click', async () => {
    if(!confirm(`${it.ref}을(를) 지울까요? 계획서·활동 기록도 같이 지워져요.`)) return;
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
    <textarea style="min-height:240px">${esc(value)}</textarea><div class="row-end" style="margin-top:6px"><button class="cancel">Cancel</button><button class="primary save">Save</button></div>`;
  box.querySelector('.cancel').addEventListener('click', route);
  box.querySelector('.save').addEventListener('click', () => {
    const t = box.querySelector('.edit-title');
    save(box.querySelector('textarea').value, t ? t.value : undefined).catch(() => {});
  });
  box.querySelector('textarea').focus();
}

// ---- 새 이슈 ----
function renderNew(params){
  const parent = params.get('parent') || '';
  const proj = parent ? parent.split('-')[0] : currentProject();
  if(!projects.length){ view.innerHTML = '<div class="empty">먼저 <a href="#/projects">Projects</a>에서 프로젝트를 만들어 주세요.</div>'; return; }
  view.innerHTML = `<form class="form" id="new-form">
    <h2 style="margin:0">${parent ? `New task · <span class="ref">${esc(parent)}</span>` : 'New issue'}</h2>
    <div class="line"><label>Project<select id="n-project"${parent ? ' disabled' : ''}>${projects.filter(p => !p.archived).map(p =>
      `<option value="${esc(p.key)}"${p.key === proj ? ' selected' : ''}>${esc(p.key)} · ${esc(p.name)}</option>`).join('')}</select></label>
      <label>Priority<select id="n-priority">${PRIORITIES.map(p => `<option${p === 'none' ? ' selected' : ''}>${p}</option>`).join('')}</select></label>
      <label>Status<select id="n-status"><option>backlog</option><option>triage</option></select></label></div>
    <label>Title<input id="n-title" maxlength="300" placeholder="비워 두면 이슈를 맡은 에이전트가 본문을 보고 지어요"></label>
    <label>Description (마크다운)<textarea id="n-body" style="min-height:260px"></textarea></label>
    <label>Labels (쉼표로)<input id="n-labels"></label>
    <div class="row-end"><a class="button" href="#/">Cancel</a><button class="primary" type="submit">Create</button></div>
  </form>`;
  view.querySelector('#n-title').focus();
  view.querySelector('#new-form').addEventListener('submit', (e) => { e.preventDefault(); whileBusy(submitBtn(e), async () => {
    const it = await api('POST', '/api/issues', {
      project: view.querySelector('#n-project').value, title: view.querySelector('#n-title').value, body: view.querySelector('#n-body').value,
      priority: view.querySelector('#n-priority').value, status: view.querySelector('#n-status').value,
      labels: view.querySelector('#n-labels').value.split(',').map(s => s.trim()).filter(Boolean), parent: parent || undefined,
    });
    location.hash = `#/issue/${it.ref}`;
  }); });
}

// ---- 에이전트 ----
async function renderAgents(newKey){
  const list = await loadAgents();
  view.innerHTML = `
    ${newKey ? `<div class="keybox"><b>${esc(newKey.name)}</b>의 API 키예요. 지금 한 번만 보여 드려요 — 에이전트 설정에 넣어 주세요.<br>
      <code id="key">${esc(newKey.key)}</code> <button id="copy-key">복사</button></div>` : ''}
    <form class="form panel" id="agent-form"><div class="line">
      <label>Name<input id="a-name" required placeholder="claude-main"></label>
      <label>Vendor<input id="a-vendor" placeholder="anthropic / openai"></label>
      <label>Model<input id="a-model" placeholder="claude-opus-5-5"></label>
      <label class="actions"><button class="primary" type="submit">Add agent</button></label></div></form>
    ${list.length ? `<table class="issues"><thead><tr><th>Name</th><th>Model</th><th class="hide-m">Key</th><th class="hide-m">Last seen</th><th>Enabled</th><th></th></tr></thead><tbody>
      ${list.map(a => `<tr><td>${esc(a.name)} <span class="dim">${esc(a.vendor)}</span></td><td>${esc(a.model)}</td>
        <td class="hide-m ref">${esc(a.key_prefix)}…</td><td class="hide-m dim">${a.last_seen_at ? fmtTime(a.last_seen_at) : '—'}</td>
        <td><input type="checkbox" data-toggle="${a.id}" ${a.enabled ? 'checked' : ''}></td>
        <td><button data-model="${a.id}" class="ghost">모델 바꾸기</button><button data-rotate="${a.id}" class="ghost">키 재발급</button></td></tr>`).join('')}
      </tbody></table>` : '<div class="empty">등록된 에이전트가 없어요.</div>'}`;
  const $ = (s) => view.querySelector(s);
  if(newKey) $('#copy-key').addEventListener('click', () => navigator.clipboard.writeText(newKey.key).then(() => toast('복사했어요')));
  $('#agent-form').addEventListener('submit', (e) => { e.preventDefault(); whileBusy(submitBtn(e), async () => {
    const r = await api('POST', '/api/agents', { name: $('#a-name').value, vendor: $('#a-vendor').value, model: $('#a-model').value });
    renderAgents({ name: r.agent.name, key: r.key });
  }); });
  view.querySelectorAll('[data-toggle]').forEach(cb => cb.addEventListener('change', async () => {
    await api('PATCH', `/api/agents/${cb.dataset.toggle}`, { enabled: cb.checked }).catch(() => {}); renderAgents();
  }));
  view.querySelectorAll('[data-model]').forEach(b => b.addEventListener('click', async () => {
    const a = agentsById.get(Number(b.dataset.model));
    const model = prompt('모델 이름', a.model);
    if(model === null) return;
    await api('PATCH', `/api/agents/${a.id}`, { model }).catch(() => {}); renderAgents();
  }));
  view.querySelectorAll('[data-rotate]').forEach(b => b.addEventListener('click', async () => {
    const a = agentsById.get(Number(b.dataset.rotate));
    if(!confirm(`${a.name}의 키를 새로 만들까요? 지금 키는 바로 못 쓰게 돼요.`)) return;
    const r = await api('POST', `/api/agents/${a.id}/rotate`);
    renderAgents({ name: a.name, key: r.key });
  }));
}

// ---- 프로젝트 ----
// 위 폼 하나로 추가와 고치기를 같이 한다 — 줄의 Edit을 누르면 그 프로젝트 값으로 채우고 키는 잠근다(이슈 번호의 앞머리라 못 바꾼다).
function renderProjects(editKey){
  const ed = projects.find(p => p.key === editKey);
  view.innerHTML = `
    <form class="form panel" id="project-form">${ed ? `<b>${esc(ed.key)} 고치기</b>` : ''}<div class="line">
      <label style="flex:0 0 90px">Key<input id="p-key" required placeholder="NS" maxlength="10" value="${esc(ed ? ed.key : '')}"${ed ? ' disabled' : ''}></label>
      <label>Name<input id="p-name" required placeholder="nightshift" value="${esc(ed ? ed.name : '')}"></label>
      <label>Repository<input id="p-repo" placeholder="https://github.com/…" value="${esc(ed ? ed.repo_url : '')}"></label>
      <label>Local path<input id="p-path" placeholder="C:\\Users\\…\\Projects\\…" value="${esc(ed ? ed.local_path : '')}"></label>
      <label class="actions">${ed ? '<button type="button" id="p-cancel">Cancel</button><button class="primary" type="submit">Save</button>'
        : '<button class="primary" type="submit">Add project</button>'}</label></div></form>
    ${projects.length ? `<table class="issues"><thead><tr><th>Key</th><th>Name</th><th class="hide-m">Repository</th><th class="hide-m">Local path</th><th>Archived</th><th></th></tr></thead><tbody>
      ${projects.map(p => `<tr><td class="ref">${esc(p.key)}</td><td>${esc(p.name)}</td><td class="hide-m">${esc(p.repo_url)}</td>
        <td class="hide-m dim">${esc(p.local_path)}</td><td><input type="checkbox" data-archive="${esc(p.key)}" ${p.archived ? 'checked' : ''}></td>
        <td><button class="ghost" data-edit="${esc(p.key)}">Edit</button></td></tr>`).join('')}
      </tbody></table>` : '<div class="empty">프로젝트가 없어요.</div>'}`;
  const val = (s) => view.querySelector(s).value;
  view.querySelector('#project-form').addEventListener('submit', (e) => { e.preventDefault(); whileBusy(submitBtn(e), async () => {
    const fields = { name: val('#p-name'), repo_url: val('#p-repo'), local_path: val('#p-path') };
    if(ed) await api('PATCH', `/api/projects/${ed.key}`, fields);
    else await api('POST', '/api/projects', { key: val('#p-key'), ...fields });
    await loadProjects(); renderProjects();
    toast(ed ? '고쳤어요' : '만들었어요');
  }); });
  if(ed){ view.querySelector('#p-cancel').addEventListener('click', () => renderProjects()); view.querySelector('#p-name').focus(); }
  view.querySelectorAll('[data-edit]').forEach(b => b.addEventListener('click', () => { renderProjects(b.dataset.edit); window.scrollTo(0, 0); }));
  view.querySelectorAll('[data-archive]').forEach(cb => cb.addEventListener('change', async () => {
    await api('PATCH', `/api/projects/${cb.dataset.archive}`, { archived: cb.checked }).catch(() => {});
    await loadProjects(); renderProjects();
  }));
}

// ---- 시작 ----
async function boot(){
  const res = await fetch('/api/auth/me');
  const data = await res.json();
  if(!data.actor || data.actor.kind !== 'human'){ showLogin(data.reason); return; }
  me = data.actor;
  document.getElementById('user-chip-name').textContent = me.name;
  document.getElementById('open-nightshift').href = data.nightshift_url;
  document.getElementById('login').hidden = true;
  document.getElementById('shell').hidden = false;
  await Promise.all([loadProjects(), loadAgents()]);
  route();
}
boot();
