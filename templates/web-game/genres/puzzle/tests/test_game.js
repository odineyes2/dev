// 퍼즐의 도달 가능성·경계·턴·완성 뒤 되돌리기를 검사한다.
'use strict';
const assert=require('node:assert/strict');
const Game=require('../static/game.js');
for(let seed=1;seed<=100;seed++) {
  let value=seed;
  const random=()=>{value=(value*1664525+1013904223)>>>0;return value/2**32;};
  const state=Game.createGame(random);
  assert.deepEqual([...state.board].sort((a,b)=>a-b),Array.from({length:16},(_,i)=>i));
  const numbers=state.board.filter(Boolean);
  let inversions=0;
  numbers.forEach((v,i)=>numbers.slice(i+1).forEach(w=>{if(v>w) inversions++;}));
  assert.equal((inversions+4-Math.floor(state.board.indexOf(0)/4))%2,1);
  assert.equal(state.won,false);assert.equal(state.moves,0);assert.equal(state.history.length,0);
  const original=state.board.slice();
  const index=state.board.findIndex((_,i)=>Game.canMove(state,i));
  assert.equal(Game.move(state,index),true);assert.equal(state.moves,1);
  assert.equal(Game.undo(state),true);assert.deepEqual(state.board,original);
  assert.equal(Game.undo(state),false);
}
const state={board:[1,2,3,4,5,6,7,8,9,10,11,12,13,14,0,15],moves:0,history:[],won:false,score:0};
for(const index of [-1,16,0,14,1.5,NaN]) assert.equal(Game.move(state,index),false);
state.paused=true;assert.equal(Game.move(state,15),false);state.paused=false;
assert.equal(Game.move(state,15),true);assert.equal(state.won,true);assert.equal(state.score,999);
assert.equal(Game.move(state,14),false);
assert.equal(Game.undo(state),true);assert.equal(state.won,false);assert.equal(state.score,0);assert.equal(state.moves,0);
state.moves=1200;Game.move(state,15);assert.equal(state.score,1);
const edge={board:[1,2,3,0,4,5,6,7,8,9,10,11,12,13,14,15]};
assert.equal(Game.canMove(edge,4),false);
console.log('OK — 퍼즐 섞기·이동·턴·되돌리기·완성·점수');
