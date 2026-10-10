/* 브라우저 화면 취향 — 초기 렌더와 Settings가 같은 카탈로그를 사용한다. */
(() => {
  'use strict';
  const key = 'dev.fonts';
  const defaults = {english: 'default', korean: 'default'};
  const catalog = {
    english: [
      {id: 'default', label: '기본값 (내용 Pretendard / 틀 IBM Plex Mono)'},
      {id: 'pretendard', label: 'Pretendard'},
      {id: 'plex', label: 'IBM Plex Mono'},
      {id: 'apple', label: 'Apple 시스템'}
    ],
    korean: [
      {id: 'default', label: '기본값 (Pretendard)'},
      {id: 'apple', label: 'Apple SD Gothic Neo'}
    ]
  };
  const stacks = {
    sans: "'Pretendard Variable', -apple-system, 'Segoe UI', sans-serif",
    mono: "'IBM Plex Mono', 'Pretendard Variable', Consolas, monospace"
  };
  function validate(value){
    if(!value || typeof value !== 'object' || Array.isArray(value)) return {...defaults};
    return Object.fromEntries(Object.keys(defaults).map(language => [language,
      catalog[language].some(option => option.id === value[language]) ? value[language] : 'default']));
  }
  function read(){
    try { return validate(JSON.parse(localStorage.getItem(key))); }
    catch(e){ return {...defaults}; }
  }
  let current = read();
  function apply(value){
    current = validate(value);
    const style = document.documentElement.style;
    for(const role of ['sans', 'mono']){
      if(current.english === 'default' && current.korean === 'default'){
        style.removeProperty('--' + role);
        continue;
      }
      const korean = current.korean === 'apple' ? "'Dev Apple Hangul', 'Dev Pretendard Hangul', " : "'Dev Pretendard Hangul', ";
      const english = {default: '', pretendard: "'Pretendard Variable', ", plex: "'IBM Plex Mono', ",
        apple: '-apple-system, BlinkMacSystemFont, '}[current.english];
      style.setProperty('--' + role, korean + english + stacks[role]);
    }
  }
  function save(value){
    apply(value);
    try { localStorage.setItem(key, JSON.stringify(current)); return true; }
    catch(e){ return false; }
  }
  window.DevFonts = {catalog, defaults, read, apply, save, get: () => ({...current})};
  apply(current);
})();
