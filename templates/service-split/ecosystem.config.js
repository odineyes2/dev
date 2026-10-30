// 분리형 API와 별도 워커를 PM2로 실행한다.
module.exports = {apps: [{name: '{{APP_NAME}}', cwd: __dirname, script: 'python', args: 'backend/serve.py', interpreter: 'none', env: {HOST: '127.0.0.1', PORT: '{{PORT}}'}}, {name: '{{APP_NAME}}-worker', cwd: __dirname, script: 'python', args: 'backend/worker.py', interpreter: 'none'}]};
