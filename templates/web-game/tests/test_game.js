// 순수 게임 로직의 이동·벽·수집·정지를 검사한다.
const assert = require('node:assert/strict');
const {createGame, step, collect} = require('../static/game.js');
const s=createGame(() => 0); step(s,{x:1,y:0},1/60); assert.equal(s.x,404);
s.x=788; step(s,{x:1,y:0},1/60); assert.equal(s.x,788);
s.y=12; step(s,{x:0,y:-1},1/60); assert.equal(s.y,12);
s.coin={x:s.x,y:s.y}; assert.equal(collect(s,()=>0),true); assert.equal(s.score,1);
s.paused=true; const before=JSON.stringify(s); step(s,{x:-1,y:1},1/60); assert.equal(JSON.stringify(s),before);
console.log('OK — 이동·벽 충돌·수집·정지');
