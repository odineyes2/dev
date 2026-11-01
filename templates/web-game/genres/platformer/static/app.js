// 플랫포머 입력·고정 간격 루프·그리기와 점수 기록을 연결한다.
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
const canvas=document.getElementById('game'), ctx=canvas.getContext('2d');
let state=Game.createGame(), accumulator=0, previous=0, jump=false;
const keys=new Set(), touches=new Map(), pause=document.getElementById('pause');
function setPaused(value) { state.paused=value; keys.clear(); touches.clear(); jump=false; pause.textContent=value?'계속하기':'일시정지'; }
function restart() { state=Game.createGame(); accumulator=0; setPaused(false); }
pause.onclick=()=>setPaused(!state.paused);
document.getElementById('restart').onclick=restart;
addEventListener('keydown',event=>{
  if(event.target.closest('input, button, textarea, select, [contenteditable]') || event.repeat) return;
  const key=event.key.toLowerCase();
  if(['arrowleft','arrowright','arrowup','a','d','w',' ','p','escape','r'].includes(key)) event.preventDefault();
  keys.add(key);
  if([' ','w','arrowup'].includes(key)) jump=true;
  if(key==='p'||key==='escape') setPaused(!state.paused);
  if(key==='r') restart();
});
addEventListener('keyup',event=>keys.delete(event.key.toLowerCase()));
addEventListener('blur',()=>setPaused(true));
document.addEventListener('visibilitychange',()=>{if(document.hidden) setPaused(true);});
for(const button of document.querySelectorAll('[data-control]')) {
  button.addEventListener('pointerdown',event=>{ event.preventDefault(); button.setPointerCapture(event.pointerId); touches.set(event.pointerId,button.dataset.control); if(button.dataset.control==='jump') jump=true; });
  button.addEventListener('click',event=>{if(event.detail===0 && button.dataset.control==='jump') jump=true;});
  for(const type of ['pointerup','pointercancel','lostpointercapture']) button.addEventListener(type,event=>touches.delete(event.pointerId));
}
function frame(time) {
  accumulator+=Math.min((time-(previous||time))/1000,0.1); previous=time;
  const held=new Set(touches.values());
  while(accumulator>=1/60) {
    Game.step(state,{x:Number(keys.has('d')||keys.has('arrowright')||held.has('right'))-Number(keys.has('a')||keys.has('arrowleft')||held.has('left')),jump},1/60);
    jump=false; accumulator-=1/60;
  }
  const r=canvas.getBoundingClientRect(), ratio=devicePixelRatio||1;
  if(canvas.width!==Math.round(r.width*ratio)||canvas.height!==Math.round(r.height*ratio)) {canvas.width=Math.round(r.width*ratio);canvas.height=Math.round(r.height*ratio);}
  ctx.setTransform(canvas.width/800,0,0,canvas.height/450,0,0);
  const css=getComputedStyle(document.documentElement);
  ctx.fillStyle=css.getPropertyValue('--panel');ctx.fillRect(0,0,800,450);
  ctx.fillStyle=css.getPropertyValue('--border');
  state.map.forEach((row,y)=>[...row].forEach((tile,x)=>{if(tile==='#')ctx.fillRect(x*40,y*40,40,40);}));
  ctx.fillStyle=css.getPropertyValue('--text');
  for(const coin of state.coins) {ctx.beginPath();ctx.arc(coin.x+20,coin.y+20,6,0,Math.PI*2);ctx.fill();}
  ctx.fillRect(state.goal.x+8,state.goal.y,3,40);ctx.fillRect(state.goal.x+11,state.goal.y,20,14);
  ctx.fillStyle=css.getPropertyValue('--accent');ctx.fillRect(state.x,state.y,state.w,state.h);
  canvas.dataset.playerY=String(state.y);
  document.getElementById('status').textContent=`점수 ${state.score} · ${state.paused?'멈췄어요.':state.won?'도착했어요!':'플레이 중이에요.'}`;
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
