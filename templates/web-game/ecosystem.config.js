// 프로젝트의 로컬 Python 서버를 PM2로 실행한다.
module.exports = {apps: [{name: '{{APP_NAME}}', cwd: __dirname, script: 'python', args: 'server/app.py', interpreter: 'none', env: {HOST: '127.0.0.1', PORT: '{{PORT}}'}}]};
