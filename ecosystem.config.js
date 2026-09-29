// pm2 설정 — `npx pm2 start ecosystem.config.js`로 처음 등록, 이후 재시작은
//   npx pm2 restart ecosystem.config.js --only dev --update-env
// .env를 읽어 앱 환경변수로 넘긴다(비밀값은 .env에만, 이 파일에는 적지 않는다).
const fs = require("fs");
const path = require("path");
const { execSync } = require("child_process");

function loadDotEnv() {
  const file = path.join(__dirname, ".env");
  if (!fs.existsSync(file)) return {};
  const out = {};
  for (const line of fs.readFileSync(file, "utf8").split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$/);
    if (m) out[m[1]] = m[2].replace(/^(['"])(.*)\1$/, "$2");
  }
  return out;
}

function resolvePython() {
  try {
    const cmd = process.platform === "win32" ? "where.exe python" : "which python3";
    const found = execSync(cmd, { encoding: "utf8" }).split(/\r?\n/)[0].trim();
    if (found) return found;
  } catch (e) { /* 아래 기본값 */ }
  return process.platform === "win32" ? "python" : "python3";
}

const dotEnv = loadDotEnv();
const port = (dotEnv.DEV_PORT || "8300").trim();

module.exports = {
  apps: [
    {
      name: "dev",
      script: resolvePython(),
      // 터널(cloudflared)이 같은 머신에서 붙으므로 127.0.0.1에만 연다.
      args: `-m uvicorn app:app --host 127.0.0.1 --port ${port}`,
      interpreter: "none",
      cwd: path.join(__dirname, "server"),
      env: { PYTHONUNBUFFERED: "1", ...dotEnv },
      autorestart: true,
      max_restarts: 10,
      restart_delay: 2000,
      watch: false,
    },
  ],
};
