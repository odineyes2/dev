// dev 화면 — 해시 라우팅 한 파일. #/ 목록 · #/board 칸반 · #/issue/NS-1 상세 · #/new 새 이슈 · #/agents · #/projects
'use strict';

const STATUSES = ['backlog', 'waiting', 'triage', 'in_progress', 'in_review', 'changes_requested', 'on_hold', 'done', 'closed'];
const STATUS_LABEL = { backlog: 'Backlog', waiting: 'Waiting', triage: 'Triage', in_progress: 'In Progress', in_review: 'In Review',
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
async function api(method, url, body, isCurrent = () => true){
  const opt = { method, headers: { 'X-Requested-With': 'dev' } };
  if(body !== undefined){ opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const t0 = performance.now();
  const res = await fetch(url, opt);
  perf.fetchMs += performance.now() - t0;
  perf.server = res.headers.get('Server-Timing') || perf.server;
  if(res.status === 401){ if(isCurrent()) showLogin(); throw new Error('로그인이 필요해요.'); }
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if(!res.ok){ const msg = (data && data.detail) || `요청이 실패했어요(${res.status}).`; if(isCurrent()) toast(msg); throw new Error(msg); }
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
function blockedHtml(bs){   // 선행 Task — 끝난 것은 흐리게, 아직인 것이 있으면 "대기"
  if(!bs || !bs.length) return '';
  const open = bs.some(b => !['done', 'closed'].includes(b.status));
  return `<div class="sub">${open ? '대기 · ' : ''}선행: ${bs.map(b => `<a href="#/issue/${esc(b.ref)}" class="${['done', 'closed'].includes(b.status) ? 'dim' : ''}">${esc(b.ref)}</a>`).join(', ')}</div>`;
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

// ---- 로그인 ----
function showLogin(reason){
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
  disposeList();
  cleanupBoard();
  const h = location.hash.replace(/^#\/?/, '').split('?')[0];
  const [name, arg] = h.split('/');
  const t0 = performance.now();
  perf.fetchMs = 0; perf.server = '';
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
  perfReport(name || 'issues', t0);
}
window.addEventListener('hashchange', route);

// ---- 목록 ----
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
function issueRowHtml(i){
  return `<tr class="row${i.parent_ref ? ' child' : ''}" data-ref="${esc(i.ref)}"${i.parent_ref ? ` data-parent="${esc(i.parent_ref)}"` : ''}><td class="ref">${esc(i.ref)}</td>
    <td class="title-cell">${titleHtml(i)} ${labelsHtml(i.labels)}${i.parent_id ? '<div class="sub">Task</div>' : ''}</td>
    <td>${statusHtml(i.status)} ${approvalHtml(i.approval)}</td><td class="hide-m">${prioHtml(i.priority)}</td>
    <td class="hide-m">${i.claimed_by ? esc(actorName(i.claimed_by)) : ''}</td><td class="hide-m dim">${fmtTime(i.updated_at)}</td></tr>`;
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
function placeRow(tbody, i){
  const tr = document.createElement('tbody'); tr.innerHTML = issueRowHtml(i);
  const row = tr.firstElementChild;
  const parentTr = i.parent_ref && [...tbody.children].find(r => r.dataset.ref === i.parent_ref);
  if(parentTr){
    const sibs = kidRows(tbody, i.parent_ref);
    (sibs.length ? sibs[sibs.length - 1] : parentTr).after(row);
    refreshKids(tbody, parentTr);
    return;
  }
  tbody.append(row);
  const kids = kidRows(tbody, i.ref);
  kids.reverse().forEach(k => row.after(k));
  refreshKids(tbody, row);
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
    <div id="jobs-box"></div><div id="list-body"></div><div id="list-more" class="list-more"></div>`;
  loadJobs();
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
    localStorage.setItem('dev.project', e.target.value); projectSel.value = e.target.value; loadList();
  });
  view.querySelector('#list-body').addEventListener('click', (e) => {
    const tog = e.target.closest('.tog');
    if(tog){
      const tr = tog.closest('tr.row'), set = collapsedSet();
      set.has(tr.dataset.ref) ? set.delete(tr.dataset.ref) : set.add(tr.dataset.ref);
      localStorage.setItem('dev.collapsed', JSON.stringify([...set]));
      refreshKids(tr.parentElement, tr);
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
}
async function loadList(){
  const body = view.querySelector('#list-body'), more = view.querySelector('#list-more');
  if(!body || !more || location.hash.replace(/^#\/?/, '').split('?')[0]) return;
  if(listLoad){ clearTimeout(listLoad.timer); listLoad.loading?.remove(); }
  const st = listState();
  const statuses = st.statuses.length ? st.statuses : (st.closed ? [] : OPEN_STATUSES);
  listLoad = { body, more, qs: { project: currentProject(), status: statuses.join(','), q: st.q, ...(st.approved ? { approved: 'true' } : {}) }, offset: 0, done: false, busy: false, seen: new Set() };
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
  finish();
  if(!current()) return;   // 필터 변경과 탭 이동 후의 응답은 버린다.
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
  if(tbody) items.forEach(i => placeRow(tbody, i));
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
  const cols = STATUSES.filter(s => s !== 'closed');
  const items = (await api('GET', '/api/issues?' + new URLSearchParams({ project: currentProject(), status: cols.join(',') }))).issues;
  view.innerHTML = `<div class="kanban" tabindex="0" role="region" aria-label="Issue 보드 — 빈 영역을 끌거나 좌우 방향키로 이동해요">${cols.map(s => {
    const mine = items.filter(i => i.status === s);
    return `<div class="col" data-col="${s}"><h3>${statusHtml(s)}<span class="ref">${mine.length}</span></h3><div class="cards">
      ${mine.map(i => `<div class="card" draggable="true" data-ref="${esc(i.ref)}"><div class="ref">${esc(i.ref)}${i.parent_id ? ' · Task' : ''}</div>
        <div class="t">${titleHtml(i)}</div><div class="meta">${prioHtml(i.priority)}${approvalHtml(i.approval)}${labelsHtml(i.labels)}
        ${i.claimed_by ? `<span>● ${esc(actorName(i.claimed_by))}</span>` : ''}</div></div>`).join('')}
    </div></div>`;
  }).join('')}</div>`;
  enableBoardPan(view.querySelector('.kanban'));
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
// ---- 실행 기록(DEV-29) ----
const RUN_STATUS = { running: ['in_progress', '도는 중'], ok: ['done', '완료'], failed: ['changes_requested', '실패'], timeout: ['in_progress', '시간 초과'], orphaned: ['on_hold', '끊김'] };
const PROVIDER_NAME = { claude: 'Claude', codex: 'Codex' };
const RUN_MODE = { review: '검토', execute: '실행' };
function fmtTokens(n){ return n == null ? '' : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n); }
function runsHtml(runs){
  if(!runs.length) return '<div class="dim hint">아직 실행 기록이 없어요.</div>';
  return `<ul class="runs">${runs.map(r => {
    const [color, label] = RUN_STATUS[r.status] || ['closed', r.status];
    const sec = r.ended_at ? Math.round((new Date(r.ended_at) - new Date(r.started_at)) / 1000) : null;
    const dur = sec == null ? '' : sec >= 60 ? `${Math.floor(sec / 60)}분 ${sec % 60}초` : `${sec}초`;
    const tok = r.input_tokens == null && r.output_tokens == null ? '' : `토큰 ${fmtTokens(r.input_tokens)} → ${fmtTokens(r.output_tokens)}${r.cost_usd != null ? ` · $${r.cost_usd.toFixed(2)}` : ''}`;
    return `<li><div><span class="status" style="--sc:var(--s-${color})">${label}</span> ${PROVIDER_NAME[r.provider] || 'Claude'} ${RUN_MODE[r.mode] || r.mode} <span class="dim">${fmtTime(r.started_at)}</span></div>
      <div class="dim">${[dur, tok].filter(Boolean).join(' · ')}</div>
      ${r.note ? `<div class="dim">${esc(r.note)}</div>` : ''}${r.log_file ? `<div class="dim"><code>${esc(r.log_file)}</code></div>` : ''}</li>`;
  }).join('')}</ul>`;
}
// ---- 대기열(DEV-43): 바쁠 때 누른 것은 줄에 서고 하나씩 차례로 ----
const QUEUE_LINE = '다른 일이 돌고 있으면 끝난 뒤 차례로 시작해요.';
function jobHtml(j){   // 이 이슈가 줄에 있으면 버튼 자리에 "대기 n번째 · 취소"
  return `<div class="queued"><span class="status" style="--sc:var(--s-waiting)">Waiting · ${PROVIDER_NAME[j.provider] || 'Claude'} 대기 ${j.position}번째</span>
    <button class="ghost cancel-job" data-job="${j.id}">취소</button></div>${j.note ? `<div class="dim hint">${esc(j.note)}</div>` : ''}`;
}
function queuedMsg(r, started){ return r && r.queued ? `대기열에 넣었어요 — ${r.position}번째` : started; }
async function loadJobs(){   // 목록 화면 위의 "Claude 대기 n건" — 펼치면 대기 목록과 취소
  const box = view.querySelector('#jobs-box');
  if(!box) return;
  const { jobs } = await api('GET', '/api/jobs').catch(() => ({ jobs: [] }));
  box.innerHTML = jobs.length ? `<details class="jobs"><summary><span class="status" style="--sc:var(--s-waiting)">Agent 대기 ${jobs.length}건</span></summary>
    <ul>${jobs.map(j => `<li><span class="ref">${j.position}</span> <a href="#/issue/${esc(j.ref)}">${esc(j.ref)}</a> ${PROVIDER_NAME[j.provider] || 'Claude'} ${RUN_MODE[j.mode] || esc(j.mode)}
      ${j.note ? `<span class="dim">${esc(j.note)}</span>` : ''}<button class="ghost cancel-job" data-job="${j.id}">취소</button></li>`).join('')}</ul></details>` : '';
}
view.addEventListener('click', (e) => {
  const b = e.target.closest('.cancel-job');
  if(!b) return;
  e.stopPropagation();
  whileBusy(b, async () => { await api('DELETE', `/api/jobs/${b.dataset.job}`); toast('대기를 취소했어요'); route(); });
});
// ---- Task 실행(DEV-32): 격리된 worktree에서 구현, 결과는 브랜치로 ----
// ponytail: 선행 대기는 서버 문구로 가린다 — panel이 대기/불가를 따로 주면 그걸로
function waitOnly(blocked){ return !!blocked && blocked.startsWith('선행 Task'); }
const pendingExecutions = new Map();
function executeHtml(it, liveMode, liveProvider){
  const x = it.execute, running = it.review_running && liveMode === 'execute';
  const job = it.job && it.job.mode === 'execute' ? it.job : null;
  const provider = pendingExecutions.get(it.ref) || (running ? liveProvider || 'claude' : null);
  const off = !!provider || (x.blocked && !waitOnly(x.blocked));
  const hint = running ? '구현하는 중이에요 — 끝나면 in_review로 올라와요.'
    : x.blocked ? esc(x.blocked) + (waitOnly(x.blocked) ? ' 눌러 두면 선행이 끝난 뒤 차례로 시작해요.' : '')
    : it.review_busy ? `다른 이슈에서 Agent가 일하는 중이에요 — ${QUEUE_LINE}` : `격리된 worktree에서 구현해요 — Claude 비용 상한 $${x.budget_usd}.`;
  const b = x.branch;
  return `<div class="exec">${job ? jobHtml(job) : `<button id="ask-execute"${off ? ' disabled' : ''}><svg class="ico brand-icon" aria-hidden="true"><use href="#i-claude"/></svg>${provider === 'claude' ? '실행 중…' : 'Claude에게 실행 맡기기'}</button>
    <button id="ask-codex-execute"${off ? ' disabled' : ''}><svg class="ico brand-icon" aria-hidden="true"><use href="#i-openai"/></svg>${provider === 'codex' ? '실행 중…' : 'Codex에게 실행 맡기기'}</button>
    <div class="dim hint">${hint}</div><div class="dim hint">Codex: 시간 제한 ${Math.floor(x.timeout_sec / 60)}분 · 비용 상한 없음.</div>`}
    ${b ? `<div class="branch"><code>${esc(b.branch)}</code> <span class="dim">커밋 ${b.commits}개</span>
      ${b.diff_stat ? `<pre>${esc(b.diff_stat)}</pre>` : ''}<div class="dim hint">합치기는 터미널에서: <code>git merge ${esc(b.branch)}</code></div></div>` : ''}</div>`;
}
function decisionHtml(it){
  const a = it.approval;
  const state = !a ? '<span class="dim">아직 결정하지 않았어요.</span>'
    : a.stale ? `<span class="status" style="--sc:var(--s-in_progress)">v${a.plan_version} 결정은 무효</span> <span class="dim">— 새 계획서(v${it.plan.version})가 올라왔어요. 다시 결정해 주세요.</span>`
    : `<span class="status" style="--sc:var(--s-${VERDICT_COLOR[a.verdict]})">${VERDICT_LABEL[a.verdict]} · v${a.plan_version}</span> <span class="dim">${esc(actorName(a.actor))} · ${fmtTime(a.created_at)}</span>${a.note ? md(a.note) : ''}`;
  const busy = it.review_running;
  return `<div class="decision" id="decision"><div class="decision-state">${state}</div>
    ${busy ? '<div class="dim hint">검토가 도는 중이라 끝나면 결정할 수 있어요.</div>' : (a && !a.stale) ? '' : `
    <div id="decision-form" hidden><textarea id="decision-note"></textarea>
      <div class="row-end"><button id="decision-cancel">취소</button><button id="decision-send" class="primary"></button></div></div>
    <div class="review-actions" id="decision-actions">
      <button data-verdict="approve"${it.status === 'in_review' ? '' : ' class="primary"'}>계획 승인</button>
      <button data-verdict="approve_notes">메모 붙여 승인</button>
      <button data-verdict="reject" class="danger">계획 거절</button></div>`}</div>`;
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
      <div class="row-end"><button id="result-cancel">취소</button><button id="result-send"></button></div></div>
    <div class="review-actions" id="result-actions"><button id="approve" class="primary">결과 승인 → 완료</button>
      ${it.children.length || it.parent_ref ? '<button class="complete-tree">전체 완료</button>' : ''}
      <button data-to="changes_requested" id="request-changes">수정 요청</button>
      <button data-to="closed" class="danger">결과 거절</button></div></div></div>`;
}
// ---- 단계 안내: 지금 어디이고 다음에 뭘 하면 되는지 한 줄 ----
function stageHtml(it, liveMode, liveProvider){
  const a = it.approval, x = it.execute, ok = a && !a.stale && a.verdict !== 'reject';
  let msg = '';
  if(['done', 'closed'].includes(it.status)) return '';
  if(it.review_running) msg = liveMode === 'execute' ? `${PROVIDER_NAME[liveProvider] || 'Claude'}가 구현하는 중이에요 — 끝나면 결과 확인 단계로 올라와요.` : `${PROVIDER_NAME[liveProvider] || 'Claude'}가 검토하는 중이에요 — 끝나면 계획서가 올라와요.`;
  else if(it.job) msg = `${statusHtml('waiting')} ${PROVIDER_NAME[it.job.provider] || 'Claude'} ${RUN_MODE[it.job.mode]} 대기 ${it.job.position}번째예요 — 앞의 일이 끝나면 차례로 시작해요.`;
  else if(it.status === 'in_review') msg = '결과 확인 대기 → 아래 “결과”에서 승인·수정 요청·거절을 골라 주세요.';
  else if(x) msg = x.blocked ? `실행 대기 — ${esc(x.blocked)}` : '실행할 수 있어요 → Claude나 Codex에게 실행을 맡겨 주세요.';
  else if(!it.plan) msg = '계획서가 없어요 → Claude나 Codex에게 검토를 맡겨 계획서를 받아 보세요.';
  else if(!ok) msg = '계획서 승인 대기 → 계획서 아래에서 승인·메모 붙여 승인·거절을 골라 주세요.';
  else { const next = it.children.find(c => !['done', 'closed', 'in_review'].includes(c.status));
    msg = next ? `계획 승인됨 → <a href="#/issue/${esc(next.ref)}">${esc(next.ref)}</a>에서 실행 맡기기` : it.children.length ? '모든 Task가 실행됐어요 — 결과를 확인해 주세요.' : '계획 승인됨.'; }
  return `<div class="stage" id="stage">${msg}</div>`;
}
// 부모 화면에서 바로 실행: 계획 승인됨 · 아직 안 한 Task(서버가 다시 확인한다). 바쁘거나 선행이 안 끝났으면 줄에 선다(DEV-43)
// — Task 여럿을 한 번에 눌러 두면 선행 순서대로 돈다.
function canRun(it, ch){
  const a = it.approval;
  return a && !a.stale && a.verdict !== 'reject' && ['backlog', 'changes_requested'].includes(ch.status);
}
const pendingTasks = new Set();
function taskActionsHtml(it, ch){
  const icon = provider => `<svg class="ico brand-icon" aria-hidden="true"><use href="#i-${provider === 'codex' ? 'openai' : 'claude'}"/></svg>`;
  if(ch.job){
    const provider = ch.job.provider || 'claude', label = `${PROVIDER_NAME[provider]} 실행 대기 중`;
    return `<div class="task-actions"><button class="run-task task-queued" data-ref="${esc(ch.ref)}" data-provider="${provider}" disabled aria-label="${label}" title="${esc(`${label} — 대기 ${ch.job.position}번째${ch.job.note ? ' — ' + ch.job.note : ''}`)}">${icon(provider)}<span class="hide-m">${label}</span></button></div>`;
  }
  if(!canRun(it, ch)) return '';
  return `<div class="task-actions">${['claude', 'codex'].map(provider => {
    const label = `${PROVIDER_NAME[provider]}에게 실행 맡기기`;
    return `<button class="run-task" data-ref="${esc(ch.ref)}" data-provider="${provider}"${pendingTasks.has(ch.ref) ? ' disabled' : ''} title="${label}" aria-label="${label}">${icon(provider)}<span class="hide-m">${PROVIDER_NAME[provider]}</span></button>`;
  }).join('')}</div>`;
}
// 갱신 중에도 아직 응답하지 않은 검토 요청의 provider를 유지한다.
const pendingReviews = new Map();
async function renderIssue(ref){
  const [it, { runs }] = await Promise.all([api('GET', `/api/issues/${encodeURIComponent(ref)}`), api('GET', `/api/issues/${encodeURIComponent(ref)}/runs`), agentsById.size ? null : loadAgents()]);
  const agentOpts = ['<option value="">(없음)</option>'].concat([...agentsById.values()].map(a =>
    `<option value="${a.id}"${a.id === it.assignee_agent_id ? ' selected' : ''}>${esc(a.name)}</option>`)).join('');
  const liveRun = runs.find(r => r.status === 'running') || {};
  const liveMode = liveRun.mode;
  const reviewProvider = pendingReviews.get(ref) || (it.review_running && liveMode === 'review' ? liveRun.provider : null);
  const reviewBusy = it.review_running || pendingReviews.has(ref);
  const reviewButton = (provider, id) => `<button id="${id}"${reviewBusy ? ' disabled' : ''}${it.execute ? ' hidden' : ''}><svg class="ico brand-icon" aria-hidden="true"><use href="#i-${provider === 'codex' ? 'openai' : 'claude'}"/></svg>${reviewProvider === provider ? '검토 중…' : `${PROVIDER_NAME[provider]}에게 검토 맡기기`}</button>`;
  view.innerHTML = `
    <div class="detail">
      <div>
        <div class="ref">${esc(it.ref)}${it.parent_ref ? ` · Task of <a href="#/issue/${esc(it.parent_ref)}">${esc(it.parent_ref)}</a>` : ''}</div>
        <h1 id="title">${titleHtml(it)}</h1>
        <div class="byline">${actorHtml(it.reporter)}<span>·</span><span>${fmtTime(it.created_at)}</span>${labelsHtml(it.labels)}</div>
        ${stageHtml(it, liveMode, liveRun.provider)}
        <div class="panel"><h2>Description<span class="right"><button id="edit-body">고치기</button></span></h2>
          <div id="body">${it.body ? md(it.body) : '<p class="dim">본문이 없어요.</p>'}</div></div>
        <div class="panel"><h2>Plan${it.plan ? ` <span class="meta">v${it.plan.version} · ${esc(actorName(it.plan.author))} · ${fmtTime(it.plan.created_at)}</span>` : ''}
          <span class="right">${it.plan && it.plan.version > 1 ? '<button id="plan-history">이전 판</button>' : ''}<button id="edit-plan">${it.plan ? '고쳐 쓰기' : '쓰기'}</button></span></h2>
          <div id="plan">${it.plan ? md(it.plan.body) : '<p class="dim">아직 계획서가 없어요.</p>'}</div>
          ${it.plan && !['done', 'closed'].includes(it.status) ? decisionHtml(it) : ''}</div>
        ${it.parent_ref ? '' : resultHtml(it)}
        <div class="panel"><h2>Tasks <span class="meta">${it.children.length}</span><span class="right"><a class="button" href="#/new?parent=${esc(it.ref)}">Task 추가</a></span></h2>
          ${it.children.length ? `<table class="issues tasks">${it.children.map(ch => `<tr class="row" data-ref="${esc(ch.ref)}"><td class="ref">${esc(ch.ref)}</td>
            <td>${titleHtml(ch)}${blockedHtml(ch.blocked_by)}</td><td>${statusHtml(ch.status)}${ch.merge_state ? ` <span class="dim hide-m">${esc(ch.merge_state)}</span>` : ''}${ch.claimed_by ? ` <span class="dim hide-m">${esc(actorName(ch.claimed_by))}</span>` : ''}</td>
            <td>${taskActionsHtml(it, ch)}</td></tr>`).join('')}</table>` : '<p class="dim">하위 Task가 없어요.</p>'}</div>
        <div class="panel"><h2>Activity</h2><ul class="timeline">${it.events.map(eventHtml).join('') || '<li class="dim empty-line">아직 활동이 없어요.</li>'}</ul>
          <div class="comment-box"><textarea id="comment" placeholder="댓글(마크다운)"></textarea>
            <div class="row-end"><button id="send-comment" class="primary">댓글 달기</button></div></div></div>
        ${it.parent_ref ? resultHtml(it) : ''}
      </div>
      <aside class="side panel">
        <div class="field"><span>Status</span><select id="status">${STATUSES.map(s => `<option value="${s}"${s === it.status ? ' selected' : ''}>${STATUS_LABEL[s]}</option>`).join('')}</select>
          ${it.children.length || it.parent_ref ? '<button class="complete-tree" title="최상위 이슈와 모든 Task를 한 번에 Done으로">전체 완료</button>' : ''}</div>
        <div class="field"><span>Priority</span><select id="priority">${PRIORITIES.map(p => `<option${p === it.priority ? ' selected' : ''}>${p}</option>`).join('')}</select></div>
        <div class="field"><span>Assignee</span><select id="assignee">${agentOpts}</select></div>
        <div class="field"><span>Labels (쉼표로)</span><input id="labels" value="${esc(it.labels.join(', '))}"></div>
        <div class="field"><span>Claimed</span>${it.claimed_by ? `${esc(actorName(it.claimed_by))} <span class="dim">~${new Date(it.lease_until).toLocaleTimeString()}</span>
          <button id="release" class="ghost">놓기</button>` : '<span class="dim">없음</span>'}</div>
        <div class="field"><span>Commits</span>${it.events.filter(e => e.kind === 'commit').map(e => `<div><code>${esc(e.data.sha.slice(0, 7))}</code> <span class="dim">${esc(e.data.repo)}</span></div>`).join('') || '<span class="dim">없음</span>'}</div>
        <div class="field"><span>Agent</span>
          ${it.job && it.job.mode === 'review' ? jobHtml(it.job) : `${reviewButton('claude', 'ask-review')}${reviewButton('codex', 'ask-codex-review')}
          <div class="dim hint"${it.execute ? ' hidden' : ''}>${it.review_busy && !it.review_running ? `다른 이슈에서 Agent가 일하는 중이에요 — ${QUEUE_LINE}` : '홈서버에서 검토만 해요 — 계획서·질문을 남겨요(코드 수정 없음).'}</div>`}
          ${it.execute ? executeHtml(it, liveMode, liveRun.provider) : ''}</div>
        <div class="field"><span>실행 기록</span>${runsHtml(runs)}</div>
        <div class="field"><button id="delete" class="danger" title="이슈 지우기" aria-label="이슈 지우기"><svg class="ico"><use href="#i-trash"/></svg></button></div>
      </aside>
    </div>`;
  const R = encodeURIComponent(it.ref);
  const reload = () => renderIssue(it.ref);
  const $ = (id) => view.querySelector('#' + id);
  view.querySelectorAll('tr.row').forEach(r => r.addEventListener('click', (e) => { if(!e.target.closest('a')) location.hash = `#/issue/${r.dataset.ref}`; }));
  // done/closed로 끝내면 목록으로 돌아간다(DEV-5) — 끝난 이슈 화면에 머물 일은 없다.
  const setStatus = async (status, note) => {
    let updated;
    try{ updated = await api('POST', `/api/issues/${R}/status`, note === undefined ? { status } : { status, note }); }
    catch(e){ reload(); return; }
    if(status === 'closed' && updated.status !== 'closed'){
      toast('롤백 후 운영 반영을 확인 중이에요 — 보류 상태로 남겨요.'); await reload(); return;
    }
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
  // 전체 완료(DEV-44): 최상위 이슈와 모든 Task를 한 번에 done. 닫힐 목록과 주의할 Task를 보여 주고 확인받는다.
  view.querySelectorAll('.complete-tree').forEach(b => b.addEventListener('click', async () => {
    let root = it;
    while(root.parent_ref) root = await api('GET', `/api/issues/${encodeURIComponent(root.parent_ref)}`).catch(() => null) || {};
    if(!root.ref) return;
    // ponytail: 최상위의 바로 아래 Task까지만 보여 준다(서버는 더 깊은 자손도 닫는다)
    const open = [root, ...root.children].filter(x => !['done', 'closed'].includes(x.status));
    if(!open.length){ toast('이미 모두 끝났어요'); return; }
    const warn = root.children.filter(c => !['done', 'closed'].includes(c.status)
      && (c.status !== 'in_review' || ['병합 대기', '재시작 대기', '되돌림'].includes(c.merge_state)));
    if(!confirm(`${root.ref} 묶음 전체를 Done으로 바꿀까요? 되돌리기 기능은 없어요.

닫힐 이슈:
${open.map(x => `· ${x.ref} ${x.title}`).join('\n')}`
      + (warn.length ? `\n\n주의:\n${warn.map(c => `· ${c.ref} — ${c.status !== 'in_review' ? STATUS_LABEL[c.status] : ''}${c.status !== 'in_review' && c.merge_state ? ' · ' : ''}${c.merge_state || ''}`).join('\n')}` : ''))) return;
    b.disabled = true; b.textContent = '완료하는 중…';
    try{ await api('POST', `/api/issues/${R}/complete-tree`, {}); }
    catch(e){ reload(); return; }
    toast(`${root.ref} 묶음을 끝냈어요`); location.hash = '#/';
  }));
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
    const send = async (v, note) => { const updated = await api('POST', `/api/issues/${R}/decision`, { verdict: v, note, plan_version: it.plan.version });
      if(v === 'reject' && updated.status !== 'closed'){ toast('롤백 후 운영 반영을 확인 중이에요 — 보류 상태로 남겨요.'); await reload(); return; }
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
      if(!confirm(`${ref}을(를) ${name}에게 실행 맡길까요?
홈서버에서 relay/${ref} 브랜치의 worktree에 코드를 고치고 커밋해요(push·재시작은 하지 않아요).
${limit} 사용량은 이 서버에 로그인된 ${name} 계정에서 나가요.
${QUEUE_LINE}`)) return;
      const r = await api('POST', `/api/issues/${encodeURIComponent(ref)}/execute`, { provider });
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
  if($('release')) $('release').addEventListener('click', async () => { await api('POST', `/api/issues/${R}/release`).catch(() => {}); reload(); });
  // Claude에게 검토 맡기기(DEV-13) — 도는 동안은 15초마다 이 화면을 다시 불러 계획서가 올라오면 보이게
  for(const [id, provider] of [['ask-execute', 'claude'], ['ask-codex-execute', 'codex']]) {
    if($(id)) $(id).addEventListener('click', async (e) => {
    if(e.currentTarget.disabled || pendingExecutions.has(ref)) return;
    const name = PROVIDER_NAME[provider];
    const limit = provider === 'codex' ? `시간 제한은 ${Math.floor(it.execute.timeout_sec / 60)}분이고 비용 상한은 없어요.` : `비용 상한은 $${it.execute.budget_usd}이에요.`;
    if(!confirm(`${it.ref}을(를) ${name}에게 실행 맡길까요?
홈서버에서 ${it.execute.branch ? it.execute.branch.branch : 'relay/' + it.ref} 브랜치의 worktree에 코드를 고치고 커밋해요(push·재시작은 하지 않아요).
${limit} 사용량은 이 서버에 로그인된 ${name} 계정에서 나가요.
${QUEUE_LINE}`)) return;
    pendingExecutions.set(ref, provider);
    for(const buttonId of ['ask-execute', 'ask-codex-execute']) $(buttonId).disabled = true;
    e.currentTarget.lastChild.textContent = '실행 중…';
    try {
      const r = await api('POST', `/api/issues/${R}/execute`, { provider });
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
      if(!confirm(`${it.ref}을(를) ${name}에게 검토 맡길까요?
홈서버에서 이슈와 코드를 읽고 계획서·질문을 남겨요(코드는 고치지 않아요).
사용량은 이 서버에 로그인된 ${name} 계정에서 나가요.
${QUEUE_LINE}`)) return;
      pendingReviews.set(ref, provider);
      for(const buttonId of ['ask-review', 'ask-codex-review']) $(buttonId).disabled = true;
      const label = e.currentTarget.lastChild;
      label.textContent = '검토 중…';
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
  if(it.review_running || it.job) setTimeout(() => { if(location.hash === `#/issue/${it.ref}`) reload(); }, 15000);
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
    <div class="row-end"><a class="button" href="#/">취소</a><button class="primary" type="submit">만들기</button></div>
  </form>`;
  view.querySelector('#n-title').focus();
  view.querySelector('#new-form').addEventListener('submit', (e) => { e.preventDefault(); whileBusy(submitBtn(e), async () => {
    const it = await api('POST', '/api/issues', {
      project: view.querySelector('#n-project').value, title: view.querySelector('#n-title').value, body: view.querySelector('#n-body').value,
      priority: view.querySelector('#n-priority').value, status: view.querySelector('#n-status').value,
      labels: view.querySelector('#n-labels').value.split(',').map(s => s.trim()).filter(Boolean), parent: parent || undefined,
    });
    toast('이슈를 발행했어요 — Claude나 Codex에게 검토를 맡길 수 있어요.');
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
      <label class="actions"><button class="primary" type="submit">에이전트 추가</button></label></div></form>
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

// 생성은 라이트박스, 수정은 기존 인라인 폼에서 한다.
function renderProjects(editKey, projectTab = 'manage'){
  const ed = projects.find(p => p.key === editKey);
  view.innerHTML = `
    <div class="projects-page">
    ${ed ? `<div class="project-scope"><h2>${esc(ed.name)} · ${esc(ed.key)}</h2><nav aria-label="프로젝트 화면"><button type="button" data-project-tab="manage" aria-current="${projectTab === 'manage' ? 'page' : 'false'}">관리</button><button type="button" data-project-tab="documents" aria-current="${projectTab === 'documents' ? 'page' : 'false'}">문서</button></nav></div>` : ''}
    <button class="primary project-create" id="project-create" type="button" aria-haspopup="dialog"><svg class="ico" aria-hidden="true"><use href="#i-plus"/></svg>프로젝트 추가</button>
    ${ed ? '' : '<dialog class="project-dialog" id="project-dialog" aria-labelledby="project-dialog-title">'}
    <form class="form panel" id="project-form" ${ed && projectTab === 'documents' ? 'hidden' : ''}>${ed ? '' : '<h2 id="project-dialog-title">프로젝트 만들기</h2>'}${ed ? `<b>${esc(ed.key)} 고치기</b>` : ''}<div class="line">
      <label style="flex:0 0 90px">Key<input id="p-key" required placeholder="NS" maxlength="10" value="${esc(ed ? ed.key : '')}"${ed ? ' disabled' : ''}></label>
      <label>Name<input id="p-name" required placeholder="nightshift" value="${esc(ed ? ed.name : '')}"></label>
      <label>Repository<input id="p-repo" placeholder="https://github.com/…" value="${esc(ed ? ed.repo_url : '')}"></label>
      <label>Local path<input id="p-path" placeholder="C:\\Users\\…\\Projects\\…" value="${esc(ed ? ed.local_path : '')}"></label>
      <label class="project-description">프로젝트 설명<textarea id="p-description" maxlength="200000" placeholder="목적, 대상 사용자와 원하는 기능을 적어 주세요.">${esc(ed?.description || '')}</textarea></label>
      <label class="actions">${ed ? '<button type="button" id="p-cancel">취소</button><button class="primary" type="submit">저장</button>'
        : '<button type="button" id="p-cancel">취소</button><button class="primary" type="submit">프로젝트 추가</button>'}</label></div><p id="project-error" class="error" role="alert"></p></form>
    ${ed ? '' : '</dialog>'}
    ${ed ? `<section class="panel project-docs" ${projectTab !== 'documents' ? 'hidden' : ''}><h3>공식 문서</h3><p class="dim">저장한 설명으로 생성 Issue를 등록하고 선택 도구의 유료 검토 대기열에 연결해요. 문서 작성은 Plan 승인 후 별도 Task 실행으로 진행해요.</p><div class="line"><button type="button" data-doc-provider="codex"><svg class="ico brand-icon" aria-hidden="true"><use href="#i-openai"/></svg><span>Codex로 문서 생성</span></button><button type="button" data-doc-provider="claude"><svg class="ico brand-icon" aria-hidden="true"><use href="#i-claude"/></svg><span>Claude로 문서 생성</span></button><button type="button" id="docs-refresh">새로고침</button></div><p id="docs-message" role="status" aria-live="polite"></p><div id="docs-requests"></div><div id="docs-body" aria-live="polite"></div></section>` : ''}
    ${projects.length ? `<table class="issues"><thead><tr><th>Key</th><th>Name</th><th class="hide-m">Repository</th><th class="hide-m">Local path</th><th>Archived</th><th></th></tr></thead><tbody>
      ${projects.map(p => `<tr><td class="ref">${esc(p.key)}</td><td>${esc(p.name)}</td><td class="hide-m">${esc(p.repo_url)}</td>
        <td class="hide-m dim">${esc(p.local_path)}</td><td><input type="checkbox" data-archive="${esc(p.key)}" ${p.archived ? 'checked' : ''}></td>
        <td><button class="ghost" data-edit="${esc(p.key)}">고치기</button><button class="danger" data-delete-project="${esc(p.key)}" title="프로젝트 삭제" aria-label="${esc(p.name)} 프로젝트 삭제"><svg class="ico" aria-hidden="true"><use href="#i-trash"/></svg></button></td></tr>`).join('')}
      </tbody></table>` : '<div class="empty">프로젝트가 없어요.</div>'}</div>`;
  const val = (s) => view.querySelector(s).value;
  const opener = view.querySelector('#project-create');
  const dialog = view.querySelector('#project-dialog');
  const form = view.querySelector('#project-form');
  let saving = false;
  let suggestion = null;
  const clearSuggestion = () => { suggestion = null; form.querySelector('#project-suggestion')?.remove(); };
  form.addEventListener('input', clearSuggestion);
  opener.addEventListener('click', () => {
    if(ed){ renderProjects(); view.querySelector('#project-create').click(); return; }
    form.reset(); clearSuggestion(); view.querySelector('#project-error').textContent = '';
    dialog.showModal(); view.querySelector('#p-key').focus();
  });
  if(dialog){
    dialog.addEventListener('close', () => opener.focus());
    dialog.addEventListener('cancel', e => { if(saving) e.preventDefault(); });
  }
  form.addEventListener('submit', async e => {
    e.preventDefault();
    if(saving) return;
    saving = true;
    const button = submitBtn(e), cancel = view.querySelector('#p-cancel');
    const original = button.textContent;
    button.disabled = cancel.disabled = true;
    button.textContent = '저장 중…'; form.setAttribute('aria-busy', 'true');
    view.querySelector('#project-error').textContent = '';
    try{
      let fields = { name: val('#p-name'), repo_url: val('#p-repo'), local_path: val('#p-path'), description: val('#p-description') };
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
      if(dialog) dialog.close();
      renderProjects(ed?.key); view.querySelector(ed ? '#p-name' : '#project-create').focus();
      toast(ed ? '고쳤어요' : '만들었어요');
    }catch(error){
      if(form.isConnected) view.querySelector('#project-error').textContent = `${error.message} 입력을 확인하고 다시 시도해 주세요.`;
    }finally{
      saving = false;
      button.disabled = cancel.disabled = false; button.textContent = original;
      form.removeAttribute('aria-busy');
    }
  });
  view.querySelector('#p-cancel').addEventListener('click', () => {
    if(ed){ renderProjects(); view.querySelector('#project-create').focus(); }
    else dialog.close();
  });
  if(ed && projectTab === 'manage') view.querySelector('#p-name').focus();
  view.querySelectorAll('[data-project-tab]').forEach(b => b.onclick = () => {
    renderProjects(ed.key, b.dataset.projectTab);
    view.querySelector(`[data-project-tab="${b.dataset.projectTab}"]`).focus();
  });
  if(ed && projectTab === 'documents') bindProjectDocuments(ed);
  view.querySelectorAll('[data-edit]').forEach(b => b.addEventListener('click', () => { renderProjects(b.dataset.edit); window.scrollTo(0, 0); }));
  view.querySelectorAll('[data-delete-project]').forEach(b => b.addEventListener('click', () => whileBusy(b, async () => {
    const url = `/api/projects/${encodeURIComponent(b.dataset.deleteProject)}`;
    const state = await api('GET', `${url}/delete-check`);
    if(!state.can_delete){ toast(state.blockers.join(' ')); return; }
    if(state.confirmation_required && !confirm(`${state.project.name} (${state.project.key})를 삭제할까요?\nIssue·Task ${state.issue_count}개\n${state.warning}\n다른 프로젝트의 의존 연결 ${state.external_dependency_count}개도 제거돼요. 복구할 수 없어요.`)) return;
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

// 응답이 늦게 도착해도 떠난 화면이나 다른 문서에 반영하지 않는다.
function bindProjectDocuments(project){
  const panel = view.querySelector('.project-docs');
  const body = panel.querySelector('#docs-body'), requests = panel.querySelector('#docs-requests');
  const message = panel.querySelector('#docs-message');
  const url = `/api/projects/${encodeURIComponent(project.key)}/documents`;
  const current = () => panel.isConnected;
  const states = {failed:'실패 — Issue를 확인하고 다시 시도해 주세요', merged:'병합 완료', merge_pending:'병합 대기', in_progress:'진행 중'};
  let selection = 0, requesting = false, loading = 0;
  async function load(){
    const generation = ++loading; ++selection;
    body.innerHTML = '<div class="skeleton" role="status">문서 목록을 불러오는 중이에요…</div>';
    try {
      const data = await api('GET', url, undefined, current);
      if(!current() || generation !== loading) return;
      requests.innerHTML = data.requests.map(r => `<p><a href="#/issue/${encodeURIComponent(r.ref)}">${esc(r.ref)}</a> · ${esc(states[r.state] || r.state)}</p>`).join('');
      body.innerHTML = data.documents.length ? `<label>문서 선택<select id="doc-select">${data.documents.map(d => `<option value="${esc(d.path)}">${esc(d.path)} · ${d.status === 'available' ? '조회 가능' : d.status === 'missing' ? '아직 없어요' : '조회 불가'}</option>`).join('')}</select></label><p class="dim" id="doc-purpose"></p><div id="doc-content"></div>` : '<div class="empty">아직 문서가 없어요. 설명을 저장하고 생성 Issue를 등록해 주세요.</div>';
      const select = body.querySelector('#doc-select');
      if(!select) return;
      async function read(){
        const token = ++selection, path = select.value;
        const content = body.querySelector('#doc-content');
        body.querySelector('#doc-purpose').textContent = data.documents.find(d => d.path === path)?.purpose || '';
        content.innerHTML = '<div class="skeleton" role="status">문서를 불러오는 중이에요…</div>';
        try {
          const doc = await api('GET', `${url}/content?path=${encodeURIComponent(path)}`, undefined, current);
          if(!current() || token !== selection) return;
          content.innerHTML = doc.status === 'available' ? `<pre class="document-source" tabindex="0" aria-label="문서 원문">${esc(doc.content)}</pre>` : '<div class="empty">기준 브랜치에 아직 문서가 없어요. 생성 Issue의 진행 상태와 병합 여부를 확인해 주세요.</div>';
        } catch(e){ if(current() && token === selection) content.innerHTML = `<div class="empty">${esc(e.message)} 새로고침으로 다시 시도해 주세요.</div>`; }
      }
      select.onchange = read; await read();
    } catch(e){ if(current() && generation === loading) body.innerHTML = `<div class="empty">${esc(e.message)} 새로고침으로 다시 시도해 주세요.</div>`; }
  }
  panel.querySelector('#docs-refresh').onclick = load;
  const buttons = [...panel.querySelectorAll('[data-doc-provider]')];
  buttons.forEach(button => button.onclick = async () => {
    if(requesting) return;
    if(!project.description?.trim()) { message.textContent = '관리 탭에서 프로젝트 설명을 입력하고 저장해 주세요.'; return; }
    if(project.archived) { message.textContent = '보관을 해제한 뒤 다시 시도해 주세요.'; return; }
    if(!confirm('생성 Issue를 등록하고 유료 검토 대기열에 연결할까요? 문서 작성은 Plan 승인 후 별도 실행해요.')) return;
    requesting = true; buttons.forEach(b => b.disabled = true);
    const label = button.querySelector('span');
    const original = label.textContent; label.textContent = '등록 중…';
    try {
      const result = await api('POST', `${url}/request`, {provider:button.dataset.docProvider}, current);
      if(!current()) return;
      message.innerHTML = `<a href="#/issue/${encodeURIComponent(result.ref)}">${esc(result.ref)}</a> · ${result.queue_error ? `Issue는 등록됐지만 검토 연결에 실패했어요. 같은 버튼으로 다시 시도해 주세요. ${esc(result.queue_error)}` : result.reviewed ? '검토된 Issue예요. Plan과 승인 상태를 확인해 주세요.' : result.reused ? '기존 생성 요청에 연결했어요.' : '생성 요청을 등록했어요.'}`;
      await load();
    } catch(e){ if(current()) message.textContent = `${e.message} 같은 버튼으로 다시 시도해 주세요.`; }
    finally { requesting = false; buttons.forEach(b => b.disabled = false); label.textContent = original; }
  });
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
  document.getElementById('login').hidden = true;
  document.getElementById('shell').hidden = false;
  await Promise.all([loadProjects(), loadAgents()]);
  route();
}
boot();
