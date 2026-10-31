// DOM 없이 게임 이동·충돌·수집 상태를 갱신한다.
'use strict';
const Game = (() => {
  function coin(random) { return {x: 20 + random() * 760, y: 20 + random() * 410}; }
  function createGame(random = Math.random) { return {x: 400, y: 225, score: 0, paused: false, coin: coin(random)}; }
  function collect(state, random = Math.random) {
    if (Math.hypot(state.x - state.coin.x, state.y - state.coin.y) <= 22) {
      state.score = Math.min(1000000, state.score + 1); state.coin = coin(random); return true;
    }
    return false;
  }
  function step(state, input, dt, random = Math.random) {
    if (state.paused) return;
    const length = Math.hypot(input.x, input.y) || 1;
    const distance = 240 * Math.max(0, Math.min(dt, 0.1));
    state.x = Math.max(12, Math.min(788, state.x + input.x / length * distance));
    state.y = Math.max(12, Math.min(438, state.y + input.y / length * distance));
    collect(state, random);
  }
  return {createGame, step, collect};
})();
if (typeof module !== 'undefined') module.exports = Game;
