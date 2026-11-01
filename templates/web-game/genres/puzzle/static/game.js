// DOM 없이 퍼즐의 이동·섞기·턴·되돌리기·완성을 관리한다.
'use strict';
const Game=(()=>{
  const solved=()=>Array.from({length:16},(_,i)=>(i+1)%16);
  const complete=board=>board.every((value,i)=>value===(i+1)%16);
  function canMove(state,index) {
    const blank=state.board.indexOf(0);
    return Number.isInteger(index) && index>=0 && index<16 &&
      Math.abs(Math.floor(blank/4)-Math.floor(index/4))+Math.abs(blank%4-index%4)===1;
  }
  function swap(state,index) {
    const blank=state.board.indexOf(0);
    [state.board[blank],state.board[index]]=[state.board[index],state.board[blank]];
  }
  function createGame(random=Math.random) {
    const state={board:solved(),moves:0,history:[],won:false,score:0,paused:false};
    let previous=-1;
    for(let turn=0;turn<120;turn++) {
      const choices=state.board.map((_,i)=>i).filter(i=>canMove(state,i)&&i!==previous);
      const blank=state.board.indexOf(0);
      swap(state,choices[Math.min(choices.length-1,Math.max(0,Math.floor(random()*choices.length)))]);
      previous=blank;
    }
    if(complete(state.board)) swap(state,14);
    return state;
  }
  function move(state,index) {
    if(state.paused || state.won || !canMove(state,index)) return false;
    state.history.push(state.board.slice()); swap(state,index); state.moves++;
    state.won=complete(state.board); state.score=state.won?Math.max(1,1000-state.moves):0;
    return true;
  }
  function undo(state) {
    if(state.paused || !state.history.length) return false;
    state.board=state.history.pop(); state.moves--; state.won=complete(state.board);
    state.score=state.won?Math.max(1,1000-state.moves):0;
    return true;
  }
  return {createGame,canMove,move,undo};
})();
if(typeof module!=='undefined') module.exports=Game;
