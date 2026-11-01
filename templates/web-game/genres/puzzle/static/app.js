// 퍼즐의 이벤트 입력·격자 렌더와 공통 점수 기록을 연결한다.
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
let state=Game.createGame();
const board=document.getElementById('game'), pause=document.getElementById('pause');
function render() {
  board.replaceChildren();
  state.board.forEach((value,index)=>{
    const tile=document.createElement('button');
    tile.type='button'; tile.dataset.index=String(index);
    tile.textContent=value?String(value):'';
    tile.setAttribute('aria-label',value?`${value}번 타일`:'빈칸');
    tile.disabled=!value || state.paused || state.won || !Game.canMove(state,index);
    tile.className=value?'tile':'tile empty';
    tile.onclick=()=>{Game.move(state,index);render();};
    board.append(tile);
  });
  board.dataset.board=state.board.join(',');
  document.getElementById('status').textContent=`이동 ${state.moves} · ${state.paused?'멈췄어요.':state.won?`완성했어요! 점수 ${state.score}`:'플레이 중이에요.'}`;
  document.getElementById('undo').disabled=state.paused || !state.history.length;
  pause.textContent=state.paused?'계속하기':'일시정지';
}
pause.onclick=()=>{state.paused=!state.paused;render();};
document.getElementById('undo').onclick=()=>{Game.undo(state);render();};
document.getElementById('restart').onclick=()=>{state=Game.createGame();render();};
addEventListener('keydown',event=>{
  if(event.target.closest('input, button, textarea, select, [contenteditable]') || event.repeat) return;
  const offsets={ArrowLeft:-1,ArrowRight:1,ArrowUp:-4,ArrowDown:4};
  if(!(event.key in offsets)) return;
  event.preventDefault();
  Game.move(state,state.board.indexOf(0)+offsets[event.key]); render();
});
render();
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
