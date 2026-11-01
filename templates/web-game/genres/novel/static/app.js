// 노벨 장면·선택지 표시와 브라우저 저장 한 칸을 연결한다.
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
const SAVE_KEY='{{PROJECT_KEY}}:novel:v1';
let state=Game.createGame();
function render() {
  const scene=Game.SCENES[state.scene];
  document.getElementById('game').dataset.scene=state.scene;
  document.getElementById('speaker').textContent=scene.speaker;
  document.getElementById('dialogue').textContent=scene.text;
  document.getElementById('status').textContent=`방문 ${state.history.length} · ${scene.choices.length?'이야기를 읽고 있어요.':'이야기가 끝났어요.'}`;
  const choices=document.getElementById('choices'); choices.replaceChildren();
  for(const [index,choice] of Game.choices(state)) {
    const button=document.createElement('button'); button.type='button'; button.textContent=choice.text;
    button.onclick=()=>{Game.choose(state,index);render();}; choices.append(button);
  }
}
const message=document.getElementById('save-message');
document.getElementById('restart').onclick=()=>{state=Game.createGame();message.textContent='처음부터 시작해요.';render();};
document.getElementById('save').onclick=()=>{
  try {localStorage.setItem(SAVE_KEY,Game.serialize(state));message.textContent='진행을 저장했어요.';}
  catch {message.textContent='저장하지 못했어요. 브라우저 저장 공간을 확인해 주세요.';}
};
document.getElementById('load').onclick=()=>{
  try {
    const raw=localStorage.getItem(SAVE_KEY), loaded=Game.deserialize(raw);
    if(!loaded) {
      if(raw!==null) localStorage.removeItem(SAVE_KEY);
      message.textContent='불러올 수 있는 저장이 없어요.'; return;
    }
    state=loaded;render();message.textContent='저장한 진행을 불러왔어요.';
  } catch {message.textContent='불러오지 못했어요. 브라우저 저장 설정을 확인해 주세요.';}
};
render();
