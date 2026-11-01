// 장면 선택·조건·저장 데이터의 검증을 검사한다.
'use strict';
const assert=require('node:assert/strict');
const Game=require('../static/game.js');
const state=Game.createGame();
assert.equal(Game.choices(state).length,2);
assert.equal(Game.choose(state,99),false);
assert.equal(Game.choose(state,0),true);
assert.equal(state.flags.key,true);
assert.deepEqual(state.history,['start','hall']);
assert.equal(Game.choices(state).length,2);
const raw=Game.serialize(state), loaded=Game.deserialize(raw);
assert.deepEqual(loaded,state);
loaded.flags.key=false;loaded.history.push('garden');
assert.equal(state.flags.key,true);
assert.equal(state.history.length,2);
assert.equal(Game.choose(state,0),true);
assert.equal(state.scene,'library');
assert.equal(state.flags.opened,true);
assert.deepEqual(Game.choices(state),[]);
assert.equal(Game.choose(state,0),false);
const withoutKey=Game.createGame();Game.choose(withoutKey,1);
assert.deepEqual(Game.choices(withoutKey).map(([index])=>index),[1]);
assert.equal(Game.choose(withoutKey,0),false);
Game.choose(withoutKey,1);assert.equal(withoutKey.scene,'garden');
for(const value of [null,'bad','null','[]','{}',JSON.stringify({...JSON.parse(raw),version:2})])
  assert.equal(Game.deserialize(value),null);
for(const patch of [{scene:'missing'},{scene:'toString'},{flags:[]},{flags:{key:'yes'}},
  {flags:{unknown:true}},{history:[]},{history:['missing','hall']},{history:['start']},
  {history:'start'},{history:['hall']}])
  assert.equal(Game.deserialize(JSON.stringify({...JSON.parse(raw),...patch})),null);
assert.throws(()=>Game.serialize({}));
console.log('OK — 장면·조건부 선택지·플래그·이력·저장 검증');
