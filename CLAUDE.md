# dev — Claude 작업 규칙

짧게 유지할 것. 계획서: `~/.claude/plans/dev-board-phase1.md`.

## 파일 지도
- `server/config.py` 환경변수 · `server/db.py` 스키마(마이그레이션은 MIGRATIONS 뒤에 덧붙이기만) · `server/app.py` FastAPI(인증 미들웨어·REST 라우트·/mcp·정적 파일 마운트)
- `server/auth.py` nightshift 세션 확인·에이전트 키 · `server/issues.py` 이슈 저장소와 권한 판단(REST와 MCP가 같이 씀) · `server/mcp_tools.py` MCP 도구
- `server/review.py` "Claude에게 검토 맡기기"(헤드리스 `claude -p`) — 실행마다 `runs` 테이블에 한 줄(상태·토큰·비용·로그 파일). 서버가 뜰 때 남은 running은 orphaned로
- `server/execute.py` "Claude에게 실행 맡기기"(Task 전용) — `<데이터폴더>/worktrees/<ref>` worktree + `relay/<ref>` 브랜치에서 헤드리스로 구현. 허용 목록만 통과(Bash는 `python tests/*`·읽기성 git·add·commit뿐), push 불가(pushurl 덮기·토큰 제거), 비용·시간 상한. 시작 조건: 부모 계획서 승인·선행 Task done·저장소가 base 브랜치에서 깨끗. **합치기(merge)·push·재시작은 사람이 터미널에서** — 실행은 브랜치까지만. 환경변수 `DEV_EXEC_BASE`(main)·`DEV_EXEC_BUDGET_USD`(2)·`DEV_EXEC_TIMEOUT_SEC`(1800)
- `static/` 화면(빌드 없음) · `tests/` 검사(`python tests/test_x.py`, 임시 데이터 폴더)
- `ecosystem.config.js` pm2 앱 `dev`(127.0.0.1:8300, Cloudflare 터널이 dev.lomebrote.com으로 연결)

## 규칙
- 기능마다 커밋 하나(커밋 메시지가 버전 기록). push·운영 재시작은 사용자에게 먼저 묻는다.
- 운영 재시작: `npx pm2 restart ecosystem.config.js --only dev --update-env`
- 남의 변경을 `git reset`/`checkout --`/`stash`/`rebase`/강제 push로 되돌리지 않는다.
- `.env` 내용을 출력하지 않는다. 비밀번호·키를 명령 인자나 채팅으로 다루지 않는다.
- 사람 로그인은 nightshift가 맡는다(`ns_session` 쿠키 → nightshift `/api/auth/me`, admin만). dev는 nightshift DB를 열지 않는다.
- 에이전트는 이슈를 `done`/`closed`로 바꾸거나 지울 수 없다 — 종결은 사람 몫.
- 계획서 결정(승인·조건부 승인·거절)은 사람만 한다(`decisions` 테이블, gate='plan'). 결정은 계획서 판에 붙고 새 판이 올라오면 stale. 조건부 승인의 메모는 계획서보다 우선하는 사람의 지시다. 착수 전 `get_issue`의 `approval`을 읽는다.

## 스타일
- **화면을 만들거나 고치기 전에 `docs/DESIGN.md`(dev·nightshift 공통 디자인 방향)를 읽고 따른다.** 끝나면 낮·밤 × 데스크톱·모바일을 캡처로 확인.
- 코드 주석·문서는 한국어(“~한다”), 화면 문구는 존댓말(“~해요”). 화면 용어는 영어 표준(Issue/Plan/Task).
