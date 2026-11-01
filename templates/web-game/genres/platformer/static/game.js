// 플랫포머 규칙과 화면을 구성한다.
'use strict';
const Game=(()=>{
  const TILE=40, MAP=[
    '....................',
    '....................',
    '....................',
    '....................',
    '....................',
    '....................',
    '..........C.........',
    '.........####.......',
    '.....C..............',
    '....###.............',
    '.P.C..........C...F.',
    '####################'
  ];
  function overlap(a,b) {return a.x<b.x+b.w && a.x+a.w>b.x && a.y<b.y+b.h && a.y+a.h>b.y;}
  function createGame(random=Math.random, map=MAP) {
    const state={map:[...map],x:40,y:400,w:24,h:32,vy:0,grounded:false,paused:false,won:false,score:0,coins:[],goal:{x:720,y:400,w:32,h:40}};
    map.forEach((row,y)=>[...row].forEach((tile,x)=>{
      if(tile==='P'){state.x=x*TILE;state.y=y*TILE;}
      if(tile==='C')state.coins.push({x:x*TILE,y:y*TILE,w:40,h:40});
      if(tile==='F')state.goal={x:x*TILE,y:y*TILE,w:32,h:40};
    }));
    return state;
  }
  function solids(state) {
    const result=[];
    state.map.forEach((row,y)=>[...row].forEach((tile,x)=>{if(tile==='#')result.push({x:x*TILE,y:y*TILE,w:TILE,h:TILE});}));
    return result;
  }
  function collect(state) {
    state.coins=state.coins.filter(coin=>{if(!overlap(state,coin))return true;state.score++;return false;});
  }
  function step(state,input,dt) {
    if(state.paused||state.won)return;
    const elapsed=Math.max(0,Math.min(Number.isFinite(dt)?dt:0,0.1));
    const steps=Math.max(1,Math.ceil(elapsed/(1/120))), tick=elapsed/steps;
    if(input.jump&&state.grounded){state.vy=-520;state.grounded=false;}
    const tiles=solids(state);
    for(let i=0;i<steps;i++) {
      const dx=Math.max(-1,Math.min(1,input.x||0))*240*tick;
      state.x+=dx;
      for(const tile of tiles)if(overlap(state,tile))state.x=dx>0?tile.x-state.w:dx<0?tile.x+tile.w:state.x;
      state.x=Math.max(0,Math.min(800-state.w,state.x));
      state.vy+=1400*tick;state.y+=state.vy*tick;state.grounded=false;
      for(const tile of tiles)if(overlap(state,tile)) {
        if(state.vy>0){state.y=tile.y-state.h;state.grounded=true;}
        else if(state.vy<0)state.y=tile.y+tile.h;
        state.vy=0;
      }
      collect(state);
      if(overlap(state,state.goal)){state.won=true;break;}
      if(state.y>480){state.x=40;state.y=400;state.vy=0;}
    }
  }
  return {createGame,step,collect,overlap,TILE};
})();
if(typeof module!=='undefined')module.exports=Game;
