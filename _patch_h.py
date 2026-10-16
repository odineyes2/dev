import sys
root = '.'
p = root + '/static/app.js'
s = open(p, encoding='utf8').read()
a = s.index("// ---- Harness (DEV-132)")
b = s.index("async function renderAgents(")
new = r'''// ---- Harness (DEV-132·136) — 에이전트가 받는 글을 5개 층 아코디언으로 보여 주고 1·2·4층 파일을 고친다 ----
async function renderHarness(){
  const params = new URLSearchParams(location.hash.split('?')[1] || '');
  if(!projects.length) await loadProjects();
  const filter = document.getElementById('project-filter')?.value;
  const key = params.get('project') || filter || projects[0]?.key || '';
  const seq = ++issueRenderSeq;
  view.innerHTML = `<section class="panel management-card harness-page">
    ${managementHeader('Harness', '')}
    <form class="form harness-pick" onsubmit="return false"><div class="line">
      <label>Project<select id="h-project">${projects.map(p => `<option value="${esc(p.key)}" ${p.key === key ? 'selected' : ''}>${esc(`${p.key} · ${p.name}`)}</option>`).join('')}</select></label>
    </div></form>
    <div id="harness-body" aria-live="polite" aria-busy="true"><div class="usage-loading" role="status">불러와요…</div></div></section>`;
  view.querySelector('#h-project').addEventListener('change', e => { location.hash = `#/harness?project=${encodeURIComponent(e.target.value)}`; });
  const body = view.querySelector('#harness-body');
  if(!key){ body.removeAttribute('aria-busy'); body.innerHTML = '<div class="empty">프로젝트가 아직 없어요.</div>'; return; }
  const current = () => seq === issueRenderSeq;
  let data;
  try{ data = await api('GET', `/api/harness?project=${encodeURIComponent(key)}`, undefined, current); }
  catch(e){ if(current()){ body.removeAttribute('aria-busy'); body.innerHTML = `<div class="empty">${esc(e.message)}</div>`; } return; }
  if(!current() || !data) return;
  const L = data.layers;
  const pre = text => `<pre class="harness-text">${esc(text)}</pre>`;
  const fileCard = (f, scope, path, extra = '') => `<article class="harness-file" data-path="${esc(path)}" data-scope="${esc(scope)}">
    <header><b>${esc(f.label)}</b><span class="dim">${esc(f.source)}</span>
    ${f.editable ? `<button type="button" class="harness-edit">편집</button>` : ''}</header>${extra}
    <div class="harness-content">${f.note ? `<p class="dim">${esc(f.note)}</p>` : f.exists ? pre(f.content) : '<p class="dim">아직 없는 파일이에요. 편집해서 저장하면 새로 만들어요.</p>'}</div>
    <div class="harness-result" role="status"></div></article>`;
  const list = a => a && a.length ? `<ul>${a.map(x => `<li><code>${esc(x)}</code></li>`).join('')}</ul>` : '<p class="dim">없음</p>';
  const layer = (n, title, source, inner, editable) => `<details class="harness-layer" data-layer="${n}" open><summary><h3>${n}층 · ${esc(title)}</h3> <span class="chip-static">${editable ? '편집 가능' : '읽기 전용'}</span></summary><p class="dim">출처 ${esc(source)}</p>${inner}</details>`;
  const chip = x => `<code class="chip-static">{${esc(x)}}</code>`;
  const slotChips = f => `<p class="harness-slots dim">쓸 수 있는 자리표시: ${f.slots.length ? f.slots.map(chip).join(' ') : '없음'}${f.required.length ? ` · 꼭 있어야 하는 것: ${f.required.map(chip).join(' ')}` : ''}</p>`;
  const P = L.prompts, S = L.settings;
  const stageName = {review: '검토', execute: '실행'}, providerName = {claude: 'Claude', codex: 'Codex'};
  const setting = E => `<details class="harness-setting" data-stage="${esc(E.stage)}" data-provider="${esc(E.provider)}"><summary>${stageName[E.stage]} · ${providerName[E.provider]}</summary><dl class="harness-enf">
      <dt>허용 도구</dt><dd>${list(E.allowed_tools)}</dd><dt>차단 도구</dt><dd>${list(E.blocked_tools)}</dd>
      <dt>기본 도구</dt><dd>${list(E.builtin_tools)}</dd><dt>웹 도메인</dt><dd>${list(E.web_domains)}</dd>
      <dt>한도</dt><dd>${list(Object.entries(E.limits || {}).map(([k, v]) => `${k}: ${v}`))}</dd>
      ${E.secret_env_removed ? `<dt>제거되는 환경변수</dt><dd>${list(E.secret_env_removed)}</dd>` : ''}
      ${E.merge_rules ? `<dt>병합 규칙</dt><dd>${list(E.merge_rules)}</dd>` : ''}
      <dt>명령 인자</dt><dd>${list(E.args)}</dd></dl></details>`;
  body.removeAttribute('aria-busy');
  body.innerHTML = `<p class="dim">${esc(data.note)}</p>
    ${layer(1, '공통 하네스', L.common.source, fileCard(L.common, 'common', 'harness/COMMON.md'), true)}
    ${layer(2, '프로젝트 하네스', `${key} 저장소의 규칙 파일`, L.project.map(f => {
      const design = f.label.startsWith('docs/DESIGN.md');
      return fileCard(f, design ? 'common' : key, design ? 'docs/DESIGN.md' : f.label, f.readers ? `<p class="dim harness-readers">읽는 에이전트: ${esc(f.readers)}</p>` : '');
    }).join(''), true)}
    ${layer(3, '파일 지도', L.file_map.source, L.file_map.content ? pre(L.file_map.content) : '<p class="dim">지도를 만들 수 없어요.</p>', false)}
    ${layer(4, '코드 속 프롬프트', 'dev 저장소 harness/prompts/*.md', P.files.map(f => fileCard(f, 'prompts', f.path, slotChips(f))).join('') +
      `<div class="harness-filled"><b>코드가 채우는 부분</b><p class="dim">아래는 파일이 아니라 코드가 상황에 따라 덧붙여요.</p>${list(P.code_filled)}</div>`, true)}
    ${layer(5, '코드 속 설정값', 'server/review.py · server/execute.py', S.items.map(setting).join('') + '<p class="dim">바꾸려면 이슈로 요청해 주세요.</p>', false)}`;
  body.querySelectorAll('.harness-edit').forEach(btn => btn.addEventListener('click', async () => {
    const card = btn.closest('.harness-file'), { scope, path } = card.dataset;
    const content = card.querySelector('.harness-content'), result = card.querySelector('.harness-result');
    btn.disabled = true;
    try{
      const q = `scope=${encodeURIComponent(scope)}&path=${encodeURIComponent(path)}`;
      let base = (await api('GET', `/api/harness/history?${q}`, undefined, current)).base_sha;
      const f = scope === 'prompts' ? P.files.find(x => x.path === path)
        : scope === 'common' ? (path === 'docs/DESIGN.md' ? L.project.find(x => x.label.startsWith('docs/DESIGN.md')) : L.common) : L.project.find(x => x.label === path);
      content.innerHTML = `<textarea class="harness-area" rows="14" aria-label="${esc(path)} 내용">${esc(f?.content || '')}</textarea>
        <div class="harness-actions"><button type="button" class="primary harness-save">저장</button><button type="button" class="harness-cancel">취소</button></div>`;
      content.querySelector('.harness-cancel').addEventListener('click', () => renderHarness());
      content.querySelector('.harness-save').addEventListener('click', async ev => {
        ev.target.disabled = true; result.textContent = '';
        try{
          const r = await api('PUT', '/api/harness/file', {scope, path, content: content.querySelector('textarea').value, base_sha: base}, current, {notifyErrors: false});
          if(!r) return;
          base = r.base_sha;
          result.innerHTML = `<p>저장했어요 · 커밋 <code>${esc(r.commit.slice(0, 8))}</code></p>${pre(r.diff)}`;
          toast('저장했어요');
        }catch(e){ result.innerHTML = `<p class="error harness-error" role="alert">${esc(e.message)}</p>`; }
        ev.target.disabled = false;
      });
    }catch(e){ result.innerHTML = `<p class="error harness-error" role="alert">${esc(e.message)}</p>`; btn.disabled = false; }
  }));
}
'''
s = s[:a] + new + s[b:]
open(p, 'w', encoding='utf8').write(s)

c = root + '/static/app.css'
t = open(c, encoding='utf8').read()
old = ".harness-layer h3, .harness-seg h4{ margin:0 0 var(--sp-2); font-size:15px; }"
assert old in t
t = t.replace(old, old + """
.harness-layer > summary, .harness-setting > summary{ cursor:pointer; display:flex; flex-wrap:wrap; align-items:center; gap:var(--sp-2); }
.harness-layer > summary h3{ display:inline; margin:0; }
.harness-setting{ border:1px solid var(--border); border-radius:var(--r-md); padding:var(--sp-2) var(--sp-3); margin-bottom:var(--sp-2); }
.harness-slots, .harness-error{ overflow-wrap:anywhere; white-space:pre-wrap; }""")
open(c, 'w', encoding='utf8').write(t)
