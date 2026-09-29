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
function fmtTime(iso){
  if(!iso) return '';
  const d = new Date(iso), diff = (Date.now() - d) / 1000;
  if(diff < 60) return '방금';
  if(diff < 3600) return `${Math.floor(diff / 60)}분 전`;
  if(diff < 86400) return `${Math.floor(diff / 3600)}시간 전`;
  if(diff < 86400 * 7) return `${Math.floor(diff / 86400)}일 전`;
  return d.toLocaleDateString();
}
function statusHtml(s){ return `<span class="status" style="--sc:var(--s-${esc(s)})">${esc(STATUS_LABEL[s] || s)}</span>`; }
function prioHtml(p){ return p && p !== 'none' ? `<span class="prio ${esc(p)}">${esc(p)}</span>` : ''; }
function labelsHtml(ls){ return (ls || []).map(l => `<span class="label">${esc(l)}</span>`).join(''); }
function actorName(a){
  if(!a) return '';
  if(a.startsWith('human:')) return a.slice(6);
  const ag = agentsById.get(Number(a.slice(6)));
  return ag ? ag.name : a;
}
function actorHtml(a, model){
  const m = model ? ` <span class="dim">(${esc(model)})</span>` : '';
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
  await fetch('/api/auth/logout', { method: 'POST', headers: { 'X-Requested-With': 'dev' } });
  showLogin();
});

// ---- 테마 — 자동(운영체제 설정) → 낮 → 밤 순서로 돈다. 처음 적용은 index.html의 인라인 스크립트 ----
const THEMES = [['auto', '◐', 'Auto', '자동 — 운영체제 설정을 따라요'], ['light', '☀', 'Light', '낮 모드'], ['dark', '☾', 'Dark', '밤 모드']];
function paintTheme(){
  const cur = localStorage.getItem('dev.theme') || 'auto';
  const [, icon, label, title] = THEMES.find(t => t[0] === cur) || THEMES[0];
  const btn = document.getElementById('theme');
  btn.innerHTML = `${icon}<span class="hide-m">${label}</span>`;   // 좁은 화면에서는 기호만
  btn.title = `${title} (누르면 바뀌어요)`;
  if(cur === 'auto') delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = cur;
}
document.getElementById('theme').addEventListener('click', () => {
  const cur = localStorage.getItem('dev.theme') || 'auto';
  const next = THEMES[(THEMES.findIndex(t => t[0] === cur) + 1) % THEMES.length][0];
  if(next === 'auto') localStorage.removeItem('dev.theme'); else localStorage.setItem('dev.theme', next);
  paintTheme();
});
paintTheme();

// ---- 프로젝트 필터(모든 화면 공통, 브라우저에 기억) ----
const projectSel = document.getElementById('project-filter');
function currentProject(){ return localStorage.getItem('dev.project') || ''; }
function paintProjectFilter(){
  projectSel.innerHTML = '<option value="">All projects</option>'
    + projects.filter(p => !p.archived).map(p => `<option value="${esc(p.key)}">${esc(p.key)} · ${esc(p.name)}</option>`).join('');
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
async function renderList(){
  const st = listState();
  const statuses = st.statuses.length ? st.statuses : (st.closed ? [] : OPEN_STATUSES);
  const qs = new URLSearchParams({ project: currentProject(), status: statuses.join(','), q: st.q });
  const items = (await api('GET', '/api/issues?' + qs)).issues;
  view.innerHTML = `
    <div class="toolbar">
      <input class="grow" id="q" placeholder="제목·본문 검색" value="${esc(st.q)}">
      <div class="chips">${STATUSES.map(s => `<span class="chip${st.statuses.includes(s) ? ' on' : ''}" data-st="${s}">${STATUS_LABEL[s]}</span>`).join('')}</div>
      <label class="dim"><input type="checkbox" id="show-closed" ${st.closed ? 'checked' : ''}> 끝난 것도</label>
    </div>
    ${items.length ? `<table class="issues"><thead><tr><th>ID</th><th>Title</th><th>Status</th><th class="hide-m">Priority</th>
      <th class="hide-m">Claimed</th><th class="hide-m">Updated</th></tr></thead><tbody>
      ${items.map(i => `<tr class="row" data-ref="${esc(i.ref)}"><td class="ref">${esc(i.ref)}</td>
        <td class="title-cell">${esc(i.title)} ${labelsHtml(i.labels)}${i.parent_id ? '<div class="sub">Task</div>' : ''}</td>
        <td>${statusHtml(i.status)}</td><td class="hide-m">${prioHtml(i.priority)}</td>
        <td class="hide-m">${i.claimed_by ? esc(actorName(i.claimed_by)) : ''}</td><td class="hide-m dim">${fmtTime(i.updated_at)}</td></tr>`).join('')}
      </tbody></table>` : '<div class="empty">이슈가 없어요.</div>'}`;
  const save = (patch) => { localStorage.setItem('dev.list', JSON.stringify({ ...listState(), ...patch })); renderList(); };
  let t;
  view.querySelector('#q').addEventListener('input', (e) => { clearTimeout(t); t = setTimeout(() => save({ q: e.target.value }), 300); });
  view.querySelectorAll('[data-st]').forEach(ch => ch.addEventListener('click', () => {
    const s = ch.dataset.st, cur = listState().statuses;
    save({ statuses: cur.includes(s) ? cur.filter(x => x !== s) : [...cur, s] });
  }));
  view.querySelector('#show-closed').addEventListener('change', (e) => save({ closed: e.target.checked }));
  view.querySelectorAll('tr.row').forEach(r => r.addEventListener('click', () => { location.hash = `#/issue/${r.dataset.ref}`; }));
}

// ---- 칸반 ----
async function renderBoard(){
  const cols = STATUSES.filter(s => s !== 'closed');
  const items = (await api('GET', '/api/issues?' + new URLSearchParams({ project: currentProject(), status: cols.join(',') }))).issues;
  view.innerHTML = `<div class="kanban">${cols.map(s => {
    const mine = items.filter(i => i.status === s);
    return `<div class="col" data-col="${s}"><h3>${statusHtml(s)}<span class="dim">${mine.length}</span></h3><div class="cards">
      ${mine.map(i => `<div class="card" draggable="true" data-ref="${esc(i.ref)}"><div class="ref">${esc(i.ref)}${i.parent_id ? ' · Task' : ''}</div>
        <div class="t">${esc(i.title)}</div><div class="meta">${prioHtml(i.priority)}${labelsHtml(i.labels)}
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
  if(e.kind === 'comment') return `<li>${who}${when}${md(e.body)}</li>`;
  if(e.kind === 'status') what = `상태 ${statusHtml(d.from)} → ${statusHtml(d.to)}`;
  else if(e.kind === 'plan') what = `Plan v${esc(d.version)}을(를) 올렸어요`;
  else if(e.kind === 'commit') what = `커밋 <code>${esc(String(d.sha).slice(0, 7))}</code>${d.repo ? ` (${esc(d.repo)})` : ''} ${esc(e.body)}`;
  else if(e.kind === 'claim') what = d.action === 'release' ? '작업을 놓았어요' : `작업을 잡았어요(${esc(d.minutes)}분)`;
  else if(e.kind === 'assign') what = d.agent_id ? `담당: ${esc(actorName('agent:' + d.agent_id))}` : '담당을 비웠어요';
  else if(e.kind === 'edit') what = `고침: ${esc((d.fields || []).join(', '))}`;
  else what = esc(e.kind);
  return `<li class="sys">${who}${when} · ${what}${e.kind === 'status' && e.body ? md(e.body) : ''}</li>`;
}
async function renderIssue(ref){
  const [it] = await Promise.all([api('GET', `/api/issues/${encodeURIComponent(ref)}`), agentsById.size ? null : loadAgents()]);
  const agentOpts = ['<option value="">(없음)</option>'].concat([...agentsById.values()].map(a =>
    `<option value="${a.id}"${a.id === it.assignee_agent_id ? ' selected' : ''}>${esc(a.name)}</option>`)).join('');
  view.innerHTML = `
    <div class="detail">
      <div>
        <div class="ref">${esc(it.ref)}${it.parent_ref ? ` · Task of <a href="#/issue/${esc(it.parent_ref)}">${esc(it.parent_ref)}</a>` : ''}</div>
        <h1 id="title">${esc(it.title)}</h1>
        <div class="dim" style="margin-bottom:12px">${actorHtml(it.reporter)} · ${fmtTime(it.created_at)} ${labelsHtml(it.labels)}</div>
        <div class="panel"><h2>Description<span class="right"><button id="edit-body">Edit</button></span></h2>
          <div id="body">${it.body ? md(it.body) : '<p class="dim">본문이 없어요.</p>'}</div></div>
        <div class="panel"><h2>Plan${it.plan ? ` <span class="dim">v${it.plan.version} · ${actorHtml(it.plan.author)} · ${fmtTime(it.plan.created_at)}</span>` : ''}
          <span class="right">${it.plan && it.plan.version > 1 ? '<button id="plan-history">History</button>' : ''}<button id="edit-plan">${it.plan ? 'Revise' : 'Write'}</button></span></h2>
          <div id="plan">${it.plan ? md(it.plan.body) : '<p class="dim">아직 계획서가 없어요.</p>'}</div></div>
        <div class="panel"><h2>Tasks <span class="dim">${it.children.length}</span><span class="right"><a class="button" href="#/new?parent=${esc(it.ref)}">Add task</a></span></h2>
          ${it.children.length ? `<table class="issues">${it.children.map(ch => `<tr class="row" data-ref="${esc(ch.ref)}"><td class="ref">${esc(ch.ref)}</td>
            <td>${esc(ch.title)}</td><td>${statusHtml(ch.status)}</td></tr>`).join('')}</table>` : '<p class="dim">하위 Task가 없어요.</p>'}</div>
        <div class="panel"><h2>Activity</h2><ul class="timeline">${it.events.map(eventHtml).join('') || '<li class="dim">아직 활동이 없어요.</li>'}</ul>
          <div class="comment-box"><textarea id="comment" placeholder="댓글(마크다운)"></textarea>
            <div class="row-end"><button id="send-comment" class="primary">Comment</button></div></div></div>
      </div>
      <aside class="side panel">
        <div class="field"><span>Status</span><select id="status">${STATUSES.map(s => `<option value="${s}"${s === it.status ? ' selected' : ''}>${STATUS_LABEL[s]}</option>`).join('')}</select></div>
        ${it.status === 'in_review' ? `<div class="field"><span>Review</span><div class="review-actions">
          <button id="approve" class="primary">Approve → Done</button><button id="request-changes">Request changes</button></div></div>` : ''}
        <div class="field"><span>Priority</span><select id="priority">${PRIORITIES.map(p => `<option${p === it.priority ? ' selected' : ''}>${p}</option>`).join('')}</select></div>
        <div class="field"><span>Assignee</span><select id="assignee">${agentOpts}</select></div>
        <div class="field"><span>Labels (쉼표로)</span><input id="labels" value="${esc(it.labels.join(', '))}"></div>
        <div class="field"><span>Claimed</span>${it.claimed_by ? `${esc(actorName(it.claimed_by))} <span class="dim">~${new Date(it.lease_until).toLocaleTimeString()}</span>
          <button id="release" class="ghost">놓기</button>` : '<span class="dim">없음</span>'}</div>
        <div class="field"><span>Commits</span>${it.events.filter(e => e.kind === 'commit').map(e => `<div><code>${esc(e.data.sha.slice(0, 7))}</code> <span class="dim">${esc(e.data.repo)}</span></div>`).join('') || '<span class="dim">없음</span>'}</div>
        <div class="field"><button id="delete" class="danger">Delete issue</button></div>
      </aside>
    </div>`;
  const R = encodeURIComponent(it.ref);
  const reload = () => renderIssue(it.ref);
  const $ = (id) => view.querySelector('#' + id);
  view.querySelectorAll('tr.row').forEach(r => r.addEventListener('click', () => { location.hash = `#/issue/${r.dataset.ref}`; }));
  $('status').addEventListener('change', async (e) => { await api('POST', `/api/issues/${R}/status`, { status: e.target.value }).catch(() => {}); reload(); });
  $('priority').addEventListener('change', async (e) => { await api('PATCH', `/api/issues/${R}`, { priority: e.target.value }).catch(() => {}); reload(); });
  $('assignee').addEventListener('change', async (e) => { await api('PATCH', `/api/issues/${R}`, { assignee_agent_id: e.target.value ? Number(e.target.value) : null }).catch(() => {}); reload(); });
  $('labels').addEventListener('change', async (e) => {
    await api('PATCH', `/api/issues/${R}`, { labels: e.target.value.split(',').map(s => s.trim()).filter(Boolean) }).catch(() => {}); reload();
  });
  if($('approve')){
    $('approve').addEventListener('click', async () => { await api('POST', `/api/issues/${R}/status`, { status: 'done' }).catch(() => {}); reload(); });
    $('request-changes').addEventListener('click', async () => {
      const note = prompt('무엇을 더 해야 하나요?');
      if(note === null) return;
      await api('POST', `/api/issues/${R}/status`, { status: 'changes_requested', note }).catch(() => {}); reload();
    });
  }
  if($('release')) $('release').addEventListener('click', async () => { await api('POST', `/api/issues/${R}/release`).catch(() => {}); reload(); });
  $('delete').addEventListener('click', async () => {
    if(!confirm(`${it.ref}을(를) 지울까요? 계획서·활동 기록도 같이 지워져요.`)) return;
    await api('DELETE', `/api/issues/${R}`); location.hash = '#/';
  });
  $('send-comment').addEventListener('click', async () => {
    const body = $('comment').value;
    if(!body.trim()) return;
    await api('POST', `/api/issues/${R}/comments`, { body }); reload();
  });
  $('edit-body').addEventListener('click', () => editInPlace($('body'), it.body, async (v, title) => {
    await api('PATCH', `/api/issues/${R}`, { body: v, title }); reload();
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
    <label>Title<input id="n-title" required maxlength="300"></label>
    <label>Description (마크다운)<textarea id="n-body" style="min-height:260px"></textarea></label>
    <label>Labels (쉼표로)<input id="n-labels"></label>
    <div class="row-end"><a class="button" href="#/">Cancel</a><button class="primary" type="submit">Create</button></div>
  </form>`;
  view.querySelector('#n-title').focus();
  view.querySelector('#new-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const it = await api('POST', '/api/issues', {
      project: view.querySelector('#n-project').value, title: view.querySelector('#n-title').value, body: view.querySelector('#n-body').value,
      priority: view.querySelector('#n-priority').value, status: view.querySelector('#n-status').value,
      labels: view.querySelector('#n-labels').value.split(',').map(s => s.trim()).filter(Boolean), parent: parent || undefined,
    });
    location.hash = `#/issue/${it.ref}`;
  });
}

// ---- 에이전트 ----
async function renderAgents(newKey){
  const list = await loadAgents();
  view.innerHTML = `
    ${newKey ? `<div class="keybox"><b>${esc(newKey.name)}</b>의 API 키예요. 지금 한 번만 보여 드려요 — 에이전트 설정에 넣어 주세요.<br>
      <code id="key">${esc(newKey.key)}</code> <button id="copy-key">복사</button></div>` : ''}
    <form class="form panel" id="agent-form" style="max-width:none"><div class="line">
      <label>Name<input id="a-name" required placeholder="claude-main"></label>
      <label>Vendor<input id="a-vendor" placeholder="anthropic / openai"></label>
      <label>Model<input id="a-model" placeholder="claude-opus-5-5"></label>
      <label style="flex:0;justify-content:flex-end"><button class="primary" type="submit">Add agent</button></label></div></form>
    ${list.length ? `<table class="issues"><thead><tr><th>Name</th><th>Model</th><th class="hide-m">Key</th><th class="hide-m">Last seen</th><th>Enabled</th><th></th></tr></thead><tbody>
      ${list.map(a => `<tr><td>${esc(a.name)} <span class="dim">${esc(a.vendor)}</span></td><td>${esc(a.model)}</td>
        <td class="hide-m ref">${esc(a.key_prefix)}…</td><td class="hide-m dim">${a.last_seen_at ? fmtTime(a.last_seen_at) : '—'}</td>
        <td><input type="checkbox" data-toggle="${a.id}" ${a.enabled ? 'checked' : ''}></td>
        <td><button data-model="${a.id}" class="ghost">모델 바꾸기</button><button data-rotate="${a.id}" class="ghost">키 재발급</button></td></tr>`).join('')}
      </tbody></table>` : '<div class="empty">등록된 에이전트가 없어요.</div>'}`;
  const $ = (s) => view.querySelector(s);
  if(newKey) $('#copy-key').addEventListener('click', () => navigator.clipboard.writeText(newKey.key).then(() => toast('복사했어요')));
  $('#agent-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const r = await api('POST', '/api/agents', { name: $('#a-name').value, vendor: $('#a-vendor').value, model: $('#a-model').value });
    renderAgents({ name: r.agent.name, key: r.key });
  });
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
function renderProjects(){
  view.innerHTML = `
    <form class="form panel" id="project-form" style="max-width:none"><div class="line">
      <label style="flex:0 0 90px">Key<input id="p-key" required placeholder="NS" maxlength="10"></label>
      <label>Name<input id="p-name" required placeholder="nightshift"></label>
      <label>Repository<input id="p-repo" placeholder="https://github.com/…"></label>
      <label>Local path<input id="p-path" placeholder="C:\\Users\\…\\Projects\\…"></label>
      <label style="flex:0;justify-content:flex-end"><button class="primary" type="submit">Add project</button></label></div></form>
    ${projects.length ? `<table class="issues"><thead><tr><th>Key</th><th>Name</th><th class="hide-m">Repository</th><th class="hide-m">Local path</th><th>Archived</th></tr></thead><tbody>
      ${projects.map(p => `<tr><td class="ref">${esc(p.key)}</td><td>${esc(p.name)}</td><td class="hide-m">${esc(p.repo_url)}</td>
        <td class="hide-m dim">${esc(p.local_path)}</td><td><input type="checkbox" data-archive="${esc(p.key)}" ${p.archived ? 'checked' : ''}></td></tr>`).join('')}
      </tbody></table>` : '<div class="empty">프로젝트가 없어요.</div>'}`;
  view.querySelector('#project-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const $ = (s) => view.querySelector(s).value;
    await api('POST', '/api/projects', { key: $('#p-key'), name: $('#p-name'), repo_url: $('#p-repo'), local_path: $('#p-path') });
    await loadProjects(); renderProjects();
  });
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
  document.getElementById('login').hidden = true;
  document.getElementById('shell').hidden = false;
  await Promise.all([loadProjects(), loadAgents()]);
  route();
}
boot();
