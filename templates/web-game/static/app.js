// 게임 입력·고정 간격 루프·그리기·점수 기록을 처리한다.
'use strict';
// [option:dark_mode]
const savedTheme = localStorage.getItem('theme');
document.documentElement.dataset.theme = savedTheme === 'dark' || savedTheme === 'light' ? savedTheme : (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
document.getElementById('theme').addEventListener('click', () => {
  const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = theme;
  localStorage.setItem('theme', theme);
});
// [/option:dark_mode]
const canvas = document.getElementById('game'), ctx = canvas.getContext('2d');
let state = Game.createGame(), pointer = null, accumulator = 0, previous = 0;
const keys = new Set(), pause = document.getElementById('pause');
function setPaused(value) { state.paused = value; keys.clear(); pointer = null; pause.textContent = value ? '계속하기' : '일시정지'; }
function restart() { state = Game.createGame(); accumulator = 0; setPaused(false); }
pause.onclick = () => setPaused(!state.paused);
document.getElementById('restart').onclick = restart;
addEventListener('keydown', event => {
  if (event.target.matches('input, button, textarea') || event.repeat) return;
  const key = event.key.toLowerCase();
  if (['arrowup','arrowdown','arrowleft','arrowright','w','a','s','d','p','escape','r'].includes(key)) event.preventDefault();
  keys.add(key);
  if (key === 'p' || key === 'escape') setPaused(!state.paused);
  if (key === 'r') restart();
});
addEventListener('keyup', event => keys.delete(event.key.toLowerCase()));
addEventListener('blur', () => setPaused(true));
document.addEventListener('visibilitychange', () => { if (document.hidden) setPaused(true); });
function point(event) { const r = canvas.getBoundingClientRect(); pointer = {x:(event.clientX-r.left)*800/r.width, y:(event.clientY-r.top)*450/r.height}; }
canvas.addEventListener('pointerdown', event => { canvas.setPointerCapture(event.pointerId); point(event); });
canvas.addEventListener('pointermove', event => { if (canvas.hasPointerCapture(event.pointerId)) point(event); });
for (const type of ['pointerup','pointercancel','lostpointercapture']) canvas.addEventListener(type, () => { pointer = null; });
function frame(time) {
  accumulator += Math.min((time - (previous || time)) / 1000, 0.1); previous = time;
  let input = {x:Number(keys.has('d') || keys.has('arrowright'))-Number(keys.has('a') || keys.has('arrowleft')), y:Number(keys.has('s') || keys.has('arrowdown'))-Number(keys.has('w') || keys.has('arrowup'))};
  if (pointer) { const x=pointer.x-state.x, y=pointer.y-state.y; input = Math.hypot(x,y)>4 ? {x,y} : {x:0,y:0}; }
  while (accumulator >= 1/60) { Game.step(state, input, 1/60); accumulator -= 1/60; }
  const r = canvas.getBoundingClientRect(), ratio = devicePixelRatio || 1;
  if (canvas.width !== Math.round(r.width*ratio) || canvas.height !== Math.round(r.height*ratio)) { canvas.width=Math.round(r.width*ratio); canvas.height=Math.round(r.height*ratio); }
  ctx.setTransform(canvas.width/800,0,0,canvas.height/450,0,0);
  const css = getComputedStyle(document.documentElement);
  ctx.fillStyle=css.getPropertyValue('--panel'); ctx.fillRect(0,0,800,450);
  ctx.fillStyle=css.getPropertyValue('--text'); ctx.beginPath(); ctx.arc(state.coin.x,state.coin.y,8,0,Math.PI*2); ctx.fill();
  ctx.fillStyle=css.getPropertyValue('--accent'); ctx.beginPath(); ctx.arc(state.x,state.y,12,0,Math.PI*2); ctx.fill();
  document.getElementById('status').textContent=`점수 ${state.score} · ${state.paused ? '멈췄어요.' : '플레이 중이에요.'}`;
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
// [option:scores]
const message = document.getElementById('score-message');
async function loadScores() {
  const response = await fetch('/api/scores'); if (!response.ok) throw Error();
  const list = document.getElementById('leaders'); list.replaceChildren();
  for (const score of await response.json()) { const li=document.createElement('li'); li.textContent=`${score.name} · ${score.score}`; list.append(li); }
}
document.getElementById('scores').onsubmit = async event => {
  event.preventDefault(); const button=event.submitter; button.disabled=true;
  try {
    const response=await fetch('/api/scores',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:document.getElementById('name').value.trim(),score:state.score})});
    if (!response.ok) throw Error(); await loadScores(); message.textContent='점수를 기록했어요.';
  } catch { message.textContent='기록에 실패했어요. 연결을 확인하고 다시 시도해 주세요.'; }
  finally { button.disabled=false; }
};
loadScores().catch(() => { message.textContent='기록을 불러오지 못했어요. 연결을 확인해 주세요.'; });
// [/option:scores]
