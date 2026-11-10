// 프로젝트 브랜드 탭의 원본·조정·서버 생성 미리보기·적용 기록을 연결한다.
window.BrandTab = (() => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const image = (f, cls='') => f ? `<img class="${cls}" src="${esc(f.url)}" alt="${esc(f.name || f.path || '브랜드 미리보기')}">` : '<span class="brand-empty">없음</span>';
  async function request(url, body) {
    const r = await fetch(url, body ? {method:'POST', ...(body instanceof FormData ? {body} : {headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})} : {});
    const data = await r.json();
    if(!r.ok) throw Error(typeof data.detail === 'string' ? data.detail : '처리하지 못했어요 — 원본과 조정값을 확인해 주세요');
    return data;
  }
  function models(files, meta, domain, safe=false) {
    const icon=files('icon')[0], mask=files('maskable')[0] || icon;
    return `<div class="brand-models"><div class="brand-home"><div>${image(icon,'brand-ios')}<span>${esc(meta.short_name || meta.name)}</span></div><div class="${safe?'brand-safe':''}">${image(mask,'brand-android')}<span>${esc(meta.short_name || meta.name)}</span></div></div><div><div class="brand-browser">${image(files('favicon')[0])}<span>${esc(meta.name)}</span></div><div class="brand-phone">${image(files('splash')[0])}</div></div><div class="brand-share">${image(files('og')[0])}<div><b>${esc(meta.name)}</b><p>${esc(meta.description)}</p><small>${esc(domain)}</small></div></div></div>`;
  }
  async function connect(view, project, tab, helpers) {
    const button=view.querySelector('[data-project-tab="brand"]');
    const panel=document.createElement('section'); panel.className='brand-panel';
    if(tab==='brand'){ view.querySelector('.projects-page').append(panel); panel.innerHTML='<p role="status">브랜드 자원을 확인하고 있어요…</p>'; }
    const base=`/api/projects/${encodeURIComponent(project.key)}/brand`;
    let info;
    try { info=await request(base); } catch(e) { if(panel.isConnected) panel.textContent=e.message; return; }
    if(!button.isConnected) return;
    if(!info.available){ button.disabled=true; button.title=info.reason; const reason=document.createElement('p'); reason.className='dim'; reason.textContent=info.reason; button.closest('.management-header')?.append(reason); if(tab==='brand') panel.textContent=info.reason; return; }
    if(tab!=='brand') return;
    let draft=null, uploads={}, busy=false, dirty=false, timer, revision=0;
    const meta={...info.manifest.values};
    const options={background:meta.background_color || '#14161B',theme_same:true,theme_color:meta.theme_color || '#14161B',padding:0,corners:'none',slots:{},splash:{mode:'compose',icon_scale:0.25,text:false,text_value:meta.short_name || '',focal:{x:0.5,y:0.5}},og:{mode:'auto'},meta};
    const current=id => info.slots.find(s=>s.id===id)?.files || [];
    const field=(name,label,value,type='text',extra='') => `<label>${label}<input data-option="${name}" type="${type}" value="${esc(value)}" ${extra}></label>`;
    panel.innerHTML=`<section><h3>현재 상태</h3><div id="brand-current-models">${models(current,meta,project.service_domain)}</div><details><summary>감지한 파일 자세히</summary>${info.slots.map(s=>`<p>${esc(s.label)} ${esc(s.reason)}<br>${s.files.map(f=>esc(f.path)).join('<br>')}</p>`).join('')}</details></section>
      <section><h3>원본 하나로 한꺼번에 바꿔요</h3><button type="button" class="brand-drop" id="brand-drop"><svg class="ico" aria-hidden="true"><use href="#i-upload"/></svg><span class="brand-drop-desktop">아이콘 원본을 끌어다 놓거나 눌러서 고르기</span><span class="brand-drop-mobile">사진 고르기</span><small>정사각 1024px 이상 권장 · Ctrl+V로 붙여넣기</small></button><input type="file" id="brand-icon" accept="image/png,image/jpeg,image/webp,image/svg+xml" hidden><p id="brand-message" role="status" aria-live="polite"></p></section>
      <details id="brand-adjust"><summary>조정</summary><div class="brand-fields"><label>배경색<div class="brand-colors" id="brand-colors"></div><input data-option="background" aria-label="배경색 HEX" value="${esc(options.background)}" pattern="#[0-9a-fA-F]{6}"></label><label><button type="button" id="brand-theme-same" role="switch" aria-checked="true">테마색도 같게</button><input data-option="theme_color" aria-label="테마색 HEX" value="${esc(options.theme_color)}" hidden></label>${field('padding','아이콘 여백 (%)',0,'range','min="0" max="30"')}
      <label>앱 아이콘 모서리<select data-option="corners"><option value="none">없음</option><option value="round">둥글게</option></select></label><button type="button" id="brand-safe" role="switch" aria-checked="false">마스커블 안전 영역 보기</button>
      ${field('meta.name','앱 이름',meta.name)}${field('meta.short_name','짧은 이름',meta.short_name,'text','aria-describedby="brand-short-hint"')}<p id="brand-short-hint" class="dim"></p>${field('meta.description','설명',meta.description)}
      <label>iOS 시작 화면<select data-option="splash.mode"><option value="compose">조합형</option><option value="image">이미지형</option></select></label>${field('splash.icon_scale','시작 화면 아이콘 크기',0.25,'range','min="0.1" max="0.6" step="0.01"')}<button type="button" id="brand-text" role="switch" aria-checked="false">이름 글자 넣기</button>${field('splash.text_value','시작 화면 글자',options.splash.text_value)}<label>공유 이미지<select data-option="og.mode"><option value="auto">아이콘으로 자동 만들기</option><option value="upload">따로 올리기</option></select></label></div>
      <details><summary>슬롯별로 따로 올리기</summary><div class="brand-fields">${info.slots.filter(s=>s.id!=='meta').map(s=>`<label>${esc(s.label)}${!s.editable?`<span>${esc(s.reason)}</span>`:`<input type="file" data-upload="${s.id}" accept="image/png,image/jpeg,image/webp,image/svg+xml">${['maskable','favicon'].includes(s.id)?`<button type="button" data-auto="${s.id}">아이콘에서 자동</button>`:''}${s.id==='art'?`<select id="brand-art">${s.files.map(f=>`<option>${esc(f.path)}</option>`).join('')}</select>`:''}`}</label>`).join('')}${current('app_splash').some(f=>f.path.includes('splash-wide'))?'<label>가로 시작 그림<input type="file" data-upload="app_splash_wide" accept="image/*"></label>':''}</div></details></details>
      <section id="brand-preview" hidden><h3>미리보기</h3><div class="brand-compare-toggle"><button type="button" data-compare="before">지금</button><button type="button" data-compare="after" aria-pressed="true">바꾼 뒤</button></div><div class="brand-compare" data-show="after"><div class="brand-before"><h4>지금</h4>${models(current,info.manifest.values,project.service_domain)}</div><div class="brand-after"><h4>바꾼 뒤</h4><div id="brand-new-models"></div></div></div><details><summary>시작 화면 모두 보기</summary><div id="brand-splashes" class="brand-splashes"></div></details><div id="brand-focal" hidden tabindex="0" role="slider" aria-label="시작 화면 초점" aria-valuemin="0" aria-valuemax="100" aria-valuenow="50"><img alt="초점 지정용 원본"><span></span></div></section>
      <section><h3>바뀔 파일</h3><div id="brand-changes" class="brand-changes">원본을 올리거나 조정값을 바꿔 주세요.</div><div class="brand-actions"><button type="button" class="primary" id="brand-apply" disabled>0개 파일 적용</button><button type="button" id="brand-rebuild">다시 만들기</button><button type="button" id="brand-discard">버리기</button></div><div id="brand-progress" role="status" aria-live="polite"></div></section><section><h3>기록</h3><div id="brand-history"></div></section>`;
    const q=s=>panel.querySelector(s), message=text=>{q('#brand-message').textContent=text;};
    ['corners','splash.mode','og.mode'].forEach(name=>{
      const select=q(`[data-option="${name}"]`), group=document.createElement('div');
      group.className='brand-segments'; group.setAttribute('role','group'); group.setAttribute('aria-label',select.parentElement.firstChild.textContent);
      [...select.options].forEach(option=>{const button=document.createElement('button');button.type='button';button.textContent=option.textContent;button.dataset.value=option.value;button.setAttribute('aria-pressed',String(option.selected));button.onclick=()=>{select.value=option.value;select.dispatchEvent(new Event('input'));group.querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));};group.append(button);});
      select.hidden=true;select.after(group);
    });
    function colors(values=[]) { q('#brand-colors').innerHTML=[...new Set([...values,meta.theme_color].filter(Boolean))].map(c=>`<button type="button" class="brand-color" data-color="${esc(c)}" style="--chip:${/^#[a-f\d]{6}$/i.test(c)?c:'#14161B'}" aria-label="${esc(c)}"></button>`).join('')+'<button type="button" id="brand-custom">직접</button><input type="color" id="brand-picker" hidden>'; q('#brand-custom').onclick=()=>q('#brand-picker').click(); q('#brand-picker').oninput=e=>setColor(e.target.value); panel.querySelectorAll('[data-color]').forEach(b=>b.onclick=()=>setColor(b.dataset.color)); }
    function setColor(c){q('[data-option="background"]').value=c; options.background=c; schedule();}
    function updateApply(){q('#brand-apply').disabled=busy || dirty || !draft?.changes.length || ['waiting','committing','merging'].includes(info.apply?.phase);}
    function schedule(){dirty=true; revision++; updateApply(); clearTimeout(timer); timer=setTimeout(generate,300);}
    async function generate(){
      if(busy){timer=setTimeout(generate,300);return;} if(!panel.isConnected) return;
      busy=true; updateApply(); message('미리보기를 만들고 있어요…'); const version=revision;
      const sent={...uploads}; const form=new FormData();
      if(options.theme_same) options.theme_color=options.background;
      form.append('options',JSON.stringify(options)); if(draft) form.append('draft_id',draft.draft_id);
      Object.entries(sent).forEach(([k,f])=>form.append(k,f));
      try { const result=await request(base+'/draft',form); if(!panel.isConnected) return;
        draft=result; Object.keys(sent).forEach(k=>{if(uploads[k]===sent[k]) delete uploads[k];});
        if(version!==revision) {timer=setTimeout(generate,0);return;} dirty=false;
        if(result.source) q('#brand-adjust').open=true; colors(result.source?.suggested_colors); q('[data-option="background"]').classList.toggle('brand-attention',!!result.source?.transparent);
        message([...result.warnings,...Object.values(result.blocked || {}),result.source?.transparent?'투명 배경이 있어요 — 배경색을 골라 주세요':''].filter(Boolean).join(' · '));
        panel.querySelectorAll('.brand-segments').forEach(group=>group.querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.value===group.previousElementSibling.value))));
        q('#brand-preview').hidden=false; q('#brand-new-models').innerHTML=models(id=>result.previews[id] || current(id),options.meta,project.service_domain,q('#brand-safe').getAttribute('aria-checked')==='true');
        q('#brand-splashes').innerHTML=(result.previews.splash || []).map(f=>image(f)).join('');
        q('#brand-changes').innerHTML=result.changes.length?`<table><thead><tr><th>경로</th><th>상태</th><th>크기</th></tr></thead><tbody>${result.changes.map(f=>`<tr><td>${esc(f.path)}${f.diff?`<details><summary>텍스트 diff</summary><pre>${esc(f.diff)}</pre></details>`:''}</td><td>${({new:'새로 만듦',overwrite:'덮어씀',tags:'태그 수정'})[f.status] || esc(f.status)}</td><td>${f.width?`${f.width}×${f.height} · `:''}${Math.ceil(f.bytes/1024)} KB</td></tr>`).join('')}</tbody></table>`:'바뀐 파일이 없어요';
        q('#brand-apply').textContent=`${result.changes.length}개 파일 적용`;
      }catch(e){message(e.message);}finally{busy=false;if(panel.isConnected)updateApply();}
    }
    panel.querySelectorAll('[data-option]').forEach(el=>el.oninput=()=>{const keys=el.dataset.option.split('.');const target=keys.length===2?options[keys[0]]:options;target[keys.at(-1)]=el.type==='range'?Number(el.value):el.value;q('#brand-short-hint').textContent=options.meta.short_name.length>12?'홈 화면에서 잘릴 수 있어요':'';schedule();});
    function toggle(id,obj,key){q(id).onclick=e=>{obj[key]=!obj[key];e.currentTarget.setAttribute('aria-checked',String(obj[key]));if(id==='#brand-theme-same')q('[data-option="theme_color"]').hidden=obj[key];schedule();};}
    toggle('#brand-theme-same',options,'theme_same'); toggle('#brand-text',options.splash,'text');
    q('#brand-safe').onclick=e=>{const on=e.currentTarget.getAttribute('aria-checked')!=='true';e.currentTarget.setAttribute('aria-checked',String(on));q('#brand-new-models').querySelector('.brand-home > div:last-child')?.classList.toggle('brand-safe',on);};
    async function accept(file,slot='icon'){
      if(!file) return; if(file.size>25*1024*1024){message('원본은 25MB 이하로 골라 주세요');return;}
      if(slot==='icon'){
        try{const bitmap=await createImageBitmap(file); if(Math.min(bitmap.width,bitmap.height)<512){bitmap.close();message('원본이 너무 작아요 — 512px 이상으로 골라 주세요');return;} if(bitmap.width!==bitmap.height){const crop=await cropImage(file,bitmap.width,bitmap.height); bitmap.close(); if(!crop)return; options.crop=crop;}else{bitmap.close();delete options.crop;}}
        catch(e){if(!file.name.toLowerCase().endsWith('.svg')){message('이미지를 읽지 못했어요 — PNG·JPG·WebP·SVG로 골라 주세요');return;}}
      }
      uploads[slot]=file;
      if(['maskable','favicon'].includes(slot))options.slots[slot]='upload';
      if(slot==='og'){options.og.mode='upload';q('[data-option="og.mode"]').value='upload';}
      if(slot==='splash'){options.splash.mode='image';q('[data-option="splash.mode"]').value='image';focalSource(file);}
      if(slot==='art') options.art_path=q('#brand-art').value;
      schedule();
    }
    q('#brand-drop').onclick=()=>q('#brand-icon').click(); q('#brand-icon').onchange=e=>accept(e.target.files[0]);
    q('#brand-drop').ondragover=e=>{e.preventDefault();e.currentTarget.classList.add('brand-drag');}; q('#brand-drop').ondragleave=e=>e.currentTarget.classList.remove('brand-drag'); q('#brand-drop').ondrop=e=>{e.preventDefault();e.currentTarget.classList.remove('brand-drag');accept(e.dataTransfer.files[0]);};
    const paste=e=>{if(!panel.isConnected){document.removeEventListener('paste',paste);return;} const f=[...e.clipboardData.items].find(i=>i.type.startsWith('image/'))?.getAsFile();if(f){e.preventDefault();accept(f);}}; document.addEventListener('paste',paste);
    panel.querySelectorAll('[data-upload]').forEach(el=>el.onchange=e=>accept(e.target.files[0],el.dataset.upload));
    panel.querySelectorAll('[data-compare]').forEach(b=>b.onclick=()=>{q('.brand-compare').dataset.show=b.dataset.compare;panel.querySelectorAll('[data-compare]').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));});
    function focalSource(file){const el=q('#brand-focal'),url=URL.createObjectURL(file); el.hidden=false;el.querySelector('img').src=url;el.querySelector('img').onload=()=>URL.revokeObjectURL(url);const draw=()=>{el.querySelector('span').style.cssText=`left:${options.splash.focal.x*100}%;top:${options.splash.focal.y*100}%`;el.setAttribute('aria-valuenow',Math.round(options.splash.focal.x*100));}; const move=e=>{const r=el.getBoundingClientRect();options.splash.focal={x:Math.max(0,Math.min(1,(e.clientX-r.left)/r.width)),y:Math.max(0,Math.min(1,(e.clientY-r.top)/r.height))};draw();schedule();};el.onpointerdown=e=>{el.setPointerCapture(e.pointerId);move(e);};el.onpointermove=e=>{if(el.hasPointerCapture(e.pointerId))move(e);};el.onkeydown=e=>{if(!e.key.startsWith('Arrow'))return;e.preventDefault();const d=(e.shiftKey?10:1)/el.clientWidth;options.splash.focal.x=Math.max(0,Math.min(1,options.splash.focal.x+(e.key==='ArrowRight'?d:e.key==='ArrowLeft'?-d:0)));options.splash.focal.y=Math.max(0,Math.min(1,options.splash.focal.y+(e.key==='ArrowDown'?d:e.key==='ArrowUp'?-d:0)));draw();schedule();};draw();}
    panel.querySelectorAll('[data-auto]').forEach(b=>b.onclick=()=>{options.slots[b.dataset.auto]='auto';delete uploads[b.dataset.auto];schedule();});
    q('#brand-rebuild').onclick=schedule; q('#brand-discard').onclick=()=>{revision++;clearTimeout(timer);draft=null;uploads={};connect(view,project,tab,helpers);panel.remove();};
    async function refresh(){if(!panel.isConnected)return;try{info=await request(base);if(!panel.isConnected)return;progress();}catch(e){message(e.message);}}
    function progress(){const a=info.apply || {}; if(a.phase==='done') { draft=null; q('#brand-current-models').innerHTML=models(current,info.manifest.values,project.service_domain); } const labels={idle:'',waiting:'다른 작업 병합이 끝나면 적용돼요',committing:'커밋 중…',merging:'반영 중…',done:'완료',failed:'실패'};q('#brand-progress').innerHTML=`<p>${esc(labels[a.phase] ?? a.phase ?? '')} ${esc(a.error || '')}</p>${a.sha?`<a ${/^https?:\/\//.test(project.repo_url || '')?`href="${esc(project.repo_url.replace(/\.git$/,''))}/commit/${esc(a.sha)}" target="_blank" rel="noopener"`:''}>${esc(a.sha.slice(0,10))}</a>`:''}${a.phase==='done'?'<p class="dim">예전 아이콘이 보이면 캐시 때문이에요(최대 4시간). 홈 화면 아이콘은 앱을 지웠다가 다시 추가해 주세요.</p>':''}${(a.warnings || []).map(w=>`<p>${esc(w)}</p>`).join('')}${a.log?`<details><summary>자세히</summary><pre>${esc(typeof a.log==='string'?a.log:JSON.stringify(a.log))}</pre></details>`:''}`;updateApply();history();if(['waiting','committing','merging'].includes(a.phase))setTimeout(refresh,2000);}
    async function action(path,body){if(busy || ['waiting','committing','merging'].includes(info.apply?.phase))return;busy=true;updateApply();message('적용을 준비하고 있어요…');try{const data=await request(base+path,body);info.apply=data.apply || data;message('');progress();}catch(e){message(e.message);}finally{busy=false;if(panel.isConnected)updateApply();}}
    q('#brand-apply').onclick=()=>{if(!dirty&&draft?.changes.length)action('/apply',{draft_id:draft.draft_id});};
    function history(){q('#brand-history').innerHTML=(info.history || []).map(h=>`<div class="brand-history-row"><span>${esc(h.at)} · <code>${esc(h.sha.slice(0,10))}</code> · ${esc((h.slots || []).join(' · '))}</span><button type="button" data-revert="${esc(h.sha)}" ${h.reverted_by?'disabled':''}>${h.reverted_by?'되돌렸어요':'이 상태로 되돌리기'}</button></div>`).join('') || '<p class="dim">아직 적용 기록이 없어요.</p>';q('#brand-history').querySelectorAll('[data-revert]').forEach(b=>{b.disabled ||= busy || ['waiting','committing','merging'].includes(info.apply?.phase);b.onclick=async()=>{if(await helpers.confirm('이 적용을 되돌릴까요?',{description:'선택한 적용을 취소하는 커밋을 새로 만들어요.',okText:'되돌리기'}))action('/revert',{sha:b.dataset.revert});};});}
    colors();progress();
  }
  function cropImage(file,width,height){return new Promise(resolve=>{
    const dialog=document.createElement('dialog');dialog.className='brand-crop-dialog';const url=URL.createObjectURL(file);let size=Math.min(width,height),x=(width-size)/2,y=(height-size)/2,result=null,start;
    dialog.innerHTML=`<h3>정사각형으로 자르기</h3><p>그림을 끌거나 방향키로 옮겨 주세요. Shift는 10px씩 움직여요.</p><div class="brand-crop-stage" tabindex="0" aria-label="자르기 위치"><img src="${url}" alt="원본 자르기"><div></div></div><label>확대<input type="range" min="1" max="3" step="0.01" value="1" aria-label="자르기 확대"></label><div class="brand-actions"><button type="button" data-cancel>취소</button><button type="button" data-ok>이 영역 사용</button></div>`;
    document.body.append(dialog);const stage=dialog.querySelector('.brand-crop-stage'),box=stage.querySelector('div'); stage.style.width=`${Math.min(400,320*width/height)}px`; stage.style.maxWidth='100%';
    function draw(){x=Math.max(0,Math.min(width-size,x));y=Math.max(0,Math.min(height-size,y));box.style.cssText=`left:${x/width*100}%;top:${y/height*100}%;width:${size/width*100}%;height:${size/height*100}%`;}
    stage.onpointerdown=e=>{stage.setPointerCapture(e.pointerId);start={px:e.clientX,py:e.clientY,x,y};};stage.onpointermove=e=>{if(!stage.hasPointerCapture(e.pointerId))return;x=start.x+(e.clientX-start.px)/stage.clientWidth*width;y=start.y+(e.clientY-start.py)/stage.clientHeight*height;draw();};stage.onkeydown=e=>{if(!e.key.startsWith('Arrow'))return;e.preventDefault();const d=e.shiftKey?10:1;x+=e.key==='ArrowLeft'?-d:e.key==='ArrowRight'?d:0;y+=e.key==='ArrowUp'?-d:e.key==='ArrowDown'?d:0;draw();};dialog.querySelector('input').oninput=e=>{size=Math.min(width,height)/Number(e.target.value);draw();};dialog.querySelector('[data-cancel]').onclick=()=>dialog.close();dialog.querySelector('[data-ok]').onclick=()=>{result={x:Math.round(x),y:Math.round(y),size:Math.floor(size)};dialog.close();};dialog.onclose=()=>{URL.revokeObjectURL(url);dialog.remove();resolve(result);};draw();dialog.showModal();stage.focus();
  });}
  return {connect};
})();
