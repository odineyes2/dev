// 관리 화면의 세션 안내와 낮·밤 선택을 처리한다.
'use strict';
// [option:dark_mode]
const savedTheme = localStorage.getItem('theme');
document.documentElement.dataset.theme = savedTheme === 'dark' || savedTheme === 'light' ? savedTheme : (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
document.getElementById('theme').addEventListener('click', () => {
  const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = theme;
  localStorage.setItem('theme', theme);
});
// [/option:dark_mode]
// [option:login]
fetch('/api/me').then(async response => {
  const status = document.getElementById('session');
  if (response.ok) {
    const body = await response.json();
    status.textContent = `${body.user.username || '관리자'}님, 환영해요.`;
  } else {
    status.textContent = response.status === 403 ? '관리자만 사용할 수 있어요.' : response.status === 401 ? 'nightshift에서 로그인한 뒤 새로고침해 주세요.' : '로그인 확인에 실패했어요. 잠시 뒤 새로고침해 주세요.';
  }
}).catch(() => { document.getElementById('session').textContent = '연결을 확인한 뒤 새로고침해 주세요.'; });
// [/option:login]
