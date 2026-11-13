// 프로젝트 ecosystem에서 환경값 없이 앱 신원 메타데이터만 읽는다.
'use strict';
const path = require('path');
const root = path.resolve(process.argv[2]);
// 설정 로그·예외 원문은 부모 프로세스에 보내지 않는다.
console.log = console.error = console.warn = console.info = () => {};
const write = process.stdout.write.bind(process.stdout);
process.stdout.write = process.stderr.write = () => true;
try {
  const config = require(path.join(root, 'ecosystem.config.js'));
  if (!config || !Array.isArray(config.apps)) throw new Error();
  const apps = config.apps.map(app => {
    if (!app || typeof app.name !== 'string' || typeof app.script !== 'string' ||
        (app.cwd !== undefined && typeof app.cwd !== 'string')) throw new Error();
    return {name: app.name, cwd: path.resolve(root, app.cwd || '.'), script: app.script};
  });
  write(JSON.stringify(apps), () => process.exit(0));
} catch (_) { process.exit(1); }
