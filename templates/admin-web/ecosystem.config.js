// 프로젝트의 로컬 Python 서버를 PM2로 실행한다.
// 루트 .env가 있으면 읽어 앱 환경변수에 합친다(비밀값은 .env에만, HOST·PORT는 이 파일 값이 이긴다).
const fs = require('fs');
const path = require('path');

function loadDotEnv() {
  const file = path.join(__dirname, '.env');
  if (!fs.existsSync(file)) return {};
  const out = {};
  for (const line of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const m = line.match(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/);
    if (m) out[m[1]] = m[2].replace(/^(['"])(.*)\1$/, '$2');
  }
  return out;
}

module.exports = {apps: [{name: '{{APP_NAME}}', cwd: __dirname, script: 'python', args: 'server/app.py', interpreter: 'none', env: {...loadDotEnv(), HOST: '127.0.0.1', PORT: '{{PORT}}'}}]};
