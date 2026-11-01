// 플랫포머 규칙과 화면을 구성한다.
'use strict';
const assert=require('node:assert/strict'), Game=require('../static/game.js');
const advance=(s,input,n=60)=>{for(let i=0;i<n;i++)Game.step(s,input,1/60);};
let s=Game.createGame(()=>0.5);advance(s,{x:0});assert.equal(s.y,408);assert.equal(s.grounded,true);
Game.step(s,{x:0,jump:true},1/60);assert.ok(s.y<408);const vy=s.vy;
Game.step(s,{x:0,jump:true},1/60);assert.ok(s.vy>vy);advance(s,{x:0});assert.equal(s.y,408);
s=Game.createGame();s.x=120;s.y=400;Game.collect(s);assert.equal(s.score,1);Game.collect(s);assert.equal(s.score,1);
s=Game.createGame();s.x=720;s.y=400;Game.step(s,{x:0},1/60);assert.equal(s.won,true);const x=s.x;Game.step(s,{x:1},1);assert.equal(s.x,x);
s=Game.createGame();s.paused=true;Game.step(s,{x:1,jump:true},1);assert.equal(s.x,40);assert.equal(s.y,400);
s=Game.createGame(()=>0.5,['....................','....................','..#.................','####################']);s.x=40;s.y=88;advance(s,{x:1});assert.equal(s.x,56);
s=Game.createGame(()=>0.5,['....................','.##.................','....................','####################']);s.x=40;s.y=88;s.grounded=true;advance(s,{x:0,jump:true},4);assert.ok(s.y>=80);assert.ok(s.vy>=0);
s=Game.createGame();advance(s,{x:-1},100);assert.equal(s.x,0);advance(s,{x:1},300);assert.ok(s.x<=776);
console.log('OK - platformer physics, collisions, coins, goal and pause');
