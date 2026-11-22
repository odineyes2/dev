// 원격 /docs 읽기 전용 뷰어 — 문서 주소·안전한 Markdown·상대 링크·모바일 문서 목록을 담당한다.
'use strict';
window.ProjectDocs = (() => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const url = (project, path = '', anchor = '') => '#/projects?' + new URLSearchParams({project, tab:'docs', ...(path ? {doc:path} : {}), ...(anchor ? {anchor} : {})});
  function resolve(target, path) {
    if (/^(https?:)\/\//i.test(target)) return {external:target};
    if (/^[a-z][a-z0-9+.-]*:/i.test(target) || target.startsWith('//') || target.includes('\\')) return null;
    try {
      const parsed = new URL(target, 'https://docs.invalid/' + path);
      const file = decodeURIComponent(parsed.pathname).slice(1);
      if (!file.startsWith('docs/') || file.split('/').some(p => p.startsWith('.'))) return null;
      return {path:file, anchor:decodeURIComponent(parsed.hash.slice(1))};
    } catch (_) { return null; }
  }
  function markdown(source, {project, path, commit}) {
    const slugCounts = new Map();
    function inline(text, depth = 0) {
      if (depth > 4) return esc(text);
      const pattern = /`([^`]+)`|(!?)\[([^\]]*)\]\((<[^>]+>|[^\s)]+)(?:\s+"[^"]*")?\)|\*\*([^*]+)\*\*|\*([^*]+)\*|~~([^~]+)~~/g;
      let html = '', end = 0;
      for (const m of text.matchAll(pattern)) {
        html += esc(text.slice(end, m.index)); end = m.index + m[0].length;
        if (m[1] !== undefined) html += `<code>${esc(m[1])}</code>`;
        else if (m[3] !== undefined) {
          const target = resolve(m[4].replace(/^<|>$/g, ''), path), label = inline(m[3], depth + 1);
          if (m[2]) {
            html += target?.path && /\.(png|jpe?g|gif|webp)$/i.test(target.path)
              ? `<img loading="lazy" alt="${esc(m[3])}" src="/api/projects/${encodeURIComponent(project)}/documents/asset?${esc(new URLSearchParams({path:target.path, commit}))}">`
              : `<span class="dim">[이미지: ${esc(m[3])}]</span>`;
          } else if (target?.external) html += `<a href="${esc(target.external)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
          else if (target?.path && /\.(md|markdown|txt)$/i.test(target.path)) html += `<a href="${esc(url(project, target.path, target.anchor))}">${label}</a>`;
          else html += `<span title="/docs 안의 Markdown·텍스트 링크만 열 수 있어요">${label}</span>`;
        } else if (m[5] !== undefined) html += `<strong>${inline(m[5], depth+1)}</strong>`;
        else if (m[6] !== undefined) html += `<em>${inline(m[6], depth+1)}</em>`;
        else html += `<del>${inline(m[7], depth+1)}</del>`;
      }
      return html + esc(text.slice(end));
    }
    const cells = line => line.trim().replace(/^\|/, '').replace(/\|$/, '').split(/(?<!\\)\|/).map(s => s.trim().replace(/\\\|/g, '|'));
    const lines = String(source).replace(/\r\n?/g, '\n').split('\n');
    let html = '', paragraph = [], list = null;
    const flush = () => {
      if (paragraph.length) { html += `<p>${inline(paragraph.join('\n')).replace(/\n/g, '<br>')}</p>`; paragraph = []; }
      if (list) { html += `</${list}>`; list = null; }
    };
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i]; let m;
      if ((m = line.match(/^\s*(`{3,}|~{3,})(.*)$/))) {
        flush(); const body = [], fence = m[1][0], length = m[1].length;
        while (++i < lines.length && !new RegExp('^\\s*' + fence + '{' + length + ',}\\s*$').test(lines[i])) body.push(lines[i]);
        html += `<pre><code>${esc(body.join('\n'))}</code></pre>`;
      } else if ((m = line.match(/^(#{1,6})\s+(.+?)\s*#*$/))) {
        flush();
        const base = m[2].replace(/[*`~]/g, '').toLowerCase().replace(/[^\p{L}\p{N}_\-\s]/gu, '').replace(/\s/g, '-');
        const n = slugCounts.get(base) || 0; slugCounts.set(base, n + 1);
        html += `<h${m[1].length} id="doc-${esc(base + (n ? '-' + n : ''))}">${inline(m[2])}</h${m[1].length}>`;
      } else if (line.includes('|') && i + 1 < lines.length && cells(lines[i+1]).every(c => /^:?-{3,}:?$/.test(c)) && cells(lines[i+1]).length > 1) {
        flush(); html += '<div class="doc-table"><table><thead><tr>' + cells(line).map(c => `<th>${inline(c)}</th>`).join('') + '</tr></thead><tbody>'; i++;
        while (i + 1 < lines.length && lines[i+1].trim() && lines[i+1].includes('|')) html += '<tr>' + cells(lines[++i]).map(c => `<td>${inline(c)}</td>`).join('') + '</tr>';
        html += '</tbody></table></div>';
      } else if ((m = line.match(/^\s*(?:([-*+])|\d+[.)])\s+(.+)$/))) {
        const type = m[1] ? 'ul' : 'ol';
        if (paragraph.length || (list && list !== type)) flush();
        if (!list) { html += `<${type}>`; list = type; }
        const checkbox = m[2].match(/^\[([ xX])\]\s+(.*)$/);
        html += `<li>${checkbox ? `<input type="checkbox" disabled ${checkbox[1] !== ' ' ? 'checked' : ''} aria-label="작업 상태"> ${inline(checkbox[2])}` : inline(m[2])}</li>`;
      } else if ((m = line.match(/^>\s?(.*)$/))) { flush(); html += `<blockquote>${inline(m[1])}</blockquote>`; }
      else if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) { flush(); html += '<hr>'; }
      else if (!line.trim()) flush();
      else { if (list) flush(); paragraph.push(line); }
    }
    flush(); return html;
  }

  async function connect(view, project, helpers) {
    const host = view.querySelector('#project-documents');
    if (!host) return;
    const endpoint = `/api/projects/${encodeURIComponent(project.key)}/documents`;
    let sequence = 0;
    async function load(refresh = false) {
      const seq = ++sequence, current = () => host.isConnected && seq === sequence;
      host.innerHTML = '<p class="dim" role="status" aria-busy="true">원격 /docs를 불러와요…</p>';
      try {
        const listing = await helpers.api('GET', endpoint + (refresh ? '?refresh=true' : ''));
        if (!current()) return;
        const params = new URLSearchParams(location.hash.split('?')[1] || '');
        const path = params.get('doc') || listing.default_path;
        const meta = `<p class="dim doc-provenance">원격 기본 브랜치 <b>${esc(listing.branch)}</b> · <code>${esc(listing.commit.slice(0, 12))}</code> · 확인 ${esc(new Date(listing.checked_at).toLocaleString())}</p>`;
        host.innerHTML = `<div class="doc-toolbar"><div><h3>프로젝트 문서</h3>${meta}</div><button type="button" data-doc-refresh>새로고침</button></div><p class="dim">저장소 /docs를 읽기 전용으로 보여줘요. 로컬 변경은 커밋·푸시 후 반영되며, 조회 결과는 최대 60초간 재사용해요.</p>
          ${listing.documents.length ? `<div class="doc-layout"><details class="doc-tree" open><summary>문서 목록 · ${listing.documents.length}</summary><nav aria-label="프로젝트 문서">${listing.documents.map(d => `<a href="${esc(url(project.key, d.path))}" ${d.path === path ? 'aria-current="page"' : ''}>${esc(d.path.slice(5))}</a>`).join('')}</nav></details><section class="doc-reader" aria-label="문서 내용"><div class="doc-reading-toolbar"><code>${esc(path)}</code><button type="button" data-doc-copy>주소 복사</button></div><div data-doc-body aria-live="polite">문서를 불러와요…</div><p class="dim" data-doc-message role="status"></p></section></div>` : '<p class="empty">기본 브랜치 /docs에 Markdown·텍스트 문서가 없어요. docs/HANDOFF.md나 docs/README.md를 작성하고 푸시해 주세요.</p>'}`;
        host.querySelector('[data-doc-refresh]').onclick = () => load(true);
        if (!path || !listing.documents.length) return;
        const canonical = url(project.key, path, params.get('anchor') || '');
        history.replaceState(null, '', canonical);
        host.querySelector('[data-doc-copy]').onclick = async () => {
          const full = new URL(canonical, location.href).href, message = host.querySelector('[data-doc-message]');
          try { await navigator.clipboard.writeText(full); message.textContent = '문서 주소를 복사했어요. 문서를 읽으려면 Dev 로그인이 필요해요.'; }
          catch (_) { message.textContent = '주소를 선택해 복사해 주세요.'; const input = document.createElement('input'); input.value = full; input.readOnly = true; input.setAttribute('aria-label', '문서 공유 주소'); message.append(input); input.focus(); input.select(); }
        };
        const body = host.querySelector('[data-doc-body]');
        try {
          const doc = await helpers.api('GET', endpoint + '/read?' + new URLSearchParams({path}));
          if (!current()) return;
          if (doc.commit !== listing.commit) { body.textContent = '조회 중 기본 브랜치가 바뀌었어요. 새로고침해 주세요.'; return; }
          body.innerHTML = doc.format === 'text' ? `<pre class="document-source">${esc(doc.content)}</pre>` : `<article class="md doc-markdown">${markdown(doc.content, {project:project.key, path, commit:doc.commit})}</article>`;
          const anchor = params.get('anchor');
          if (anchor) [...body.querySelectorAll('[id]')].find(e => e.id === 'doc-' + anchor)?.scrollIntoView({block:'start'});
        } catch (e) { if (current()) { body.textContent = e.message; body.setAttribute('role', 'alert'); } }
      } catch (e) {
        if (!current()) return;
        host.innerHTML = `<p role="alert">${esc(e.message)}</p><p class="dim">프로젝트 Repository 설정과 서버의 Git 접근 권한을 확인해 주세요.</p><button type="button" data-doc-refresh>다시 불러오기</button>`;
        host.querySelector('[data-doc-refresh]').onclick = () => load(true);
      }
    }
    await load();
  }
  return {connect, markdown, resolve, url};
})();
