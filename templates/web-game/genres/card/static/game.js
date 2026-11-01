// DOM 없이 덱·손패 점수와 블랙잭의 턴 상태 전이를 관리한다.
'use strict';
const Game=(()=>{
  function createDeck() {
    return ['♠','♥','♦','♣'].flatMap(suit=>Array.from({length:13},(_,i)=>({suit,rank:i+1})));
  }
  function shuffle(deck,random=Math.random) {
    const result=deck.slice();
    for(let i=result.length-1;i>0;i--) {
      const j=Math.floor(random()*(i+1));
      [result[i],result[j]]=[result[j],result[i]];
    }
    return result;
  }
  function total(hand) {
    let value=hand.reduce((sum,card)=>sum+(card.rank===1?11:Math.min(card.rank,10)),0);
    let aces=hand.filter(card=>card.rank===1).length;
    while(value>21 && aces-->0) value-=10;
    return value;
  }
  function createGame(random=Math.random) {
    return {phase:'deal',deck:[],player:[],dealer:[],score:0,result:null,paused:false,random};
  }
  function finish(state,result) {
    state.phase='result'; state.result=result;
    if(result==='win') state.score++;
  }
  function deal(state) {
    if(state.paused || !['deal','result'].includes(state.phase)) return false;
    state.phase='deal'; state.deck=shuffle(createDeck(),state.random);
    state.player=[]; state.dealer=[]; state.result=null;
    for(let i=0;i<2;i++) {state.player.push(state.deck.pop());state.dealer.push(state.deck.pop());}
    state.phase='player'; return true;
  }
  function hit(state) {
    if(state.paused || state.phase!=='player') return false;
    state.player.push(state.deck.pop());
    if(total(state.player)>21) finish(state,'lose');
    return true;
  }
  function stand(state) {
    if(state.paused || state.phase!=='player') return false;
    state.phase='dealer'; return true;
  }
  function playDealer(state) {
    if(state.paused || state.phase!=='dealer') return false;
    while(total(state.dealer)<17) state.dealer.push(state.deck.pop());
    const player=total(state.player),dealer=total(state.dealer);
    finish(state,dealer>21 || player>dealer?'win':player===dealer?'draw':'lose');
    return true;
  }
  return {createDeck,shuffle,total,createGame,deal,hit,stand,playDealer};
})();
if(typeof module!=='undefined') module.exports=Game;
