// 장면 스크립트·조건·플래그·방문 이력과 저장 검증을 관리한다.
'use strict';
const Game = (() => {
  const SCENES = {
    start: {speaker:'사서', text:'밤의 도서관에 오신 것을 환영해요. 책상 위에 작은 열쇠가 놓여 있어요.', choices:[
      {text:'열쇠를 챙겨 복도로 가요', next:'hall', set:{key:true}},
      {text:'그대로 복도로 가요', next:'hall'}]},
    hall: {speaker:'여행자', text:'복도 끝에 잠긴 문과 정원으로 향하는 길이 있어요.', choices:[
      {text:'열쇠로 문을 열어요', next:'library', if:{key:true}, set:{opened:true}},
      {text:'정원으로 나가요', next:'garden'}]},
    library: {speaker:'사서', text:'문 너머에는 아직 쓰이지 않은 이야기가 기다리고 있어요. 다음 이야기는 직접 만들어 주세요.', choices:[]},
    garden: {speaker:'여행자', text:'달빛 아래 정원에서 잠시 쉬어요. 오늘의 이야기를 여기서 마쳐요.', choices:[]}
  };
  const own=(object,key)=>Object.prototype.hasOwnProperty.call(object,key);
  const flagNames=new Set(['key','opened']);
  function createGame() {return {scene:'start',flags:{},history:['start']};}
  function choices(state) {
    return SCENES[state.scene].choices.map((choice,index)=>[index,choice])
      .filter(([,choice])=>!choice.if || Object.entries(choice.if).every(([key,value])=>state.flags[key]===value));
  }
  function choose(state,index) {
    const entry=choices(state).find(([i])=>i===index);
    if(!entry) return false;
    const choice=entry[1];
    Object.assign(state.flags,choice.set || {});
    state.scene=choice.next;state.history.push(state.scene);return true;
  }
  function valid(state) {
    return state && typeof state==='object' && own(SCENES,state.scene) &&
      state.flags && typeof state.flags==='object' && !Array.isArray(state.flags) &&
      Object.entries(state.flags).every(([key,value])=>flagNames.has(key) && typeof value==='boolean') &&
      Array.isArray(state.history) && state.history.length>0 && state.history.length<=10000 &&
      state.history.every(id=>typeof id==='string' && own(SCENES,id)) &&
      state.history[0]==='start' && state.history.at(-1)===state.scene;
  }
  function serialize(state) {
    if(!valid(state)) throw Error('잘못된 진행 상태');
    return JSON.stringify({version:1,scene:state.scene,flags:state.flags,history:state.history});
  }
  function deserialize(raw) {
    try {
      if(typeof raw!=='string' || raw.length>100000) return null;
      const saved=JSON.parse(raw);
      if(!saved || saved.version!==1 || !valid(saved)) return null;
      return {scene:saved.scene,flags:{...saved.flags},history:[...saved.history]};
    } catch {return null;}
  }
  return {SCENES,createGame,choices,choose,serialize,deserialize};
})();
if(typeof module!=='undefined') module.exports=Game;
