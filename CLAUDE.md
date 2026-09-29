# dev — Claude 작업 규칙

짧게 유지할 것. 계획서: `~/.claude/plans/dev-board-phase1.md`.

## 파일 지도
- `server/config.py` 환경변수 · `server/db.py` 스키마(마이그레이션은 MIGRATIONS 뒤에 덧붙이기만) · `server/app.py` FastAPI
- `static/` 화면(빌드 없음) · `tests/` 검사(`python tests/test_x.py`, 임시 데이터 폴더)
- `ecosystem.config.js` pm2 앱 `dev`(127.0.0.1:8300, Cloudflare 터널이 dev.lomebrote.com으로 연결)

## 규칙
- 기능마다 커밋 하나(커밋 메시지가 버전 기록). push·운영 재시작은 사용자에게 먼저 묻는다.
- 운영 재시작: `npx pm2 restart ecosystem.config.js --only dev --update-env`
- 남의 변경을 `git reset`/`checkout --`/`stash`/`rebase`/강제 push로 되돌리지 않는다.
- `.env` 내용을 출력하지 않는다. 비밀번호·키를 명령 인자나 채팅으로 다루지 않는다.
- 사람 로그인은 nightshift가 맡는다(`ns_session` 쿠키 → nightshift `/api/auth/me`, admin만). dev는 nightshift DB를 열지 않는다.
- 에이전트는 이슈를 `done`/`closed`로 바꾸거나 지울 수 없다 — 종결은 사람 몫.

## 스타일
- 코드 주석·문서는 한국어(“~한다”), 화면 문구는 존댓말(“~해요”). 화면 용어는 영어 표준(Issue/Plan/Task).
