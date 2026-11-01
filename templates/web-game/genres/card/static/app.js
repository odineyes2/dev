// 블랙잭의 손패 표시·턴 입력과 공통 점수 기록을 연결한다.
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
  for(const who of ['dealer','player']) {
    const hand=document.getElementById(who); hand.replaceChildren();
    state[who].forEach((card,index)=>{
      const item=document.createElement('span'); item.className='card';
      item.textContent=who==='dealer' && index===1 && state.phase==='player'?'뒷면':`${card.suit} ${card.rank}`;
      hand.append(item);
    });
    document.getElementById(`${who}-title`).textContent=`${who==='dealer'?'딜러':'플레이어'} · ${who==='dealer' && state.phase==='player'?'한 장 비공개':Game.total(state[who])}`;
  }
  const results={win:'이겼어요!',lose:'딜러가 이겼어요.',draw:'비겼어요.'};
  board.dataset.phase=state.phase;
  document.getElementById('status').textContent=`승리 ${state.score} · ${state.paused?'멈췄어요.':state.phase==='result'?results[state.result]:'플레이 중이에요.'}`;
  for(const id of ['hit','stand']) document.getElementById(id).disabled=state.paused || state.phase!=='player';
  document.getElementById('next').disabled=state.paused || state.phase!=='result';
  pause.textContent=state.paused?'계속하기':'일시정지';
}
pause.onclick=()=>{state.paused=!state.paused;render();};
document.getElementById('restart').onclick=()=>{state=Game.createGame();Game.deal(state);render();};
document.getElementById('hit').onclick=()=>{Game.hit(state);render();};
document.getElementById('stand').onclick=()=>{
  if(Game.stand(state)) { render(); Game.playDealer(state); }
  render();
};
document.getElementById('next').onclick=()=>{Game.deal(state);render();};
Game.deal(state);
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
