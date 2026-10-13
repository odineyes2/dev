# dev — Claude 작업 규칙

짧게 유지할 것. 계획서: `~/.claude/plans/dev-board-phase1.md`.

## 파일 지도
- `server/config.py` 환경변수 · `server/db.py` 스키마(마이그레이션은 MIGRATIONS 뒤에 덧붙이기만) · `server/app.py` FastAPI(인증 미들웨어·REST 라우트·/mcp·정적 파일 마운트)
- `server/auth.py` nightshift 세션 확인·에이전트 키 · `server/issues.py` 이슈 저장소와 권한 판단(REST와 MCP가 같이 씀) · `server/mcp_tools.py` MCP 도구
- `server/review.py` "Claude에게 검토 맡기기"(헤드리스 `claude -p`) — 프롬프트 끝에 같은 프로젝트의 진행 중인 이슈와 남은 Task 파일을 붙여 계획이 겹침을 알고 쓰게 한다(`in_flight_section`). 실행마다 `runs` 테이블에 한 줄(상태·토큰·비용·로그 파일). 서버가 뜰 때 남은 running은 orphaned로
- `server/execute.py` "Claude/Codex에게 실행 맡기기"(Task 전용) — `<데이터폴더>/worktrees/<ref>` worktree + `relay/<ref>` 브랜치에서 헤드리스로 구현. Claude 허용 목록은 Bash의 `python tests/*`·읽기성 git·add·commit과 Moai의 `backend.tests` 검사·지정 npm 타입 검사/빌드이며 Codex는 danger-full-access로 OS sandbox 없이 실행한다. push 불가(pushurl 덮기·토큰 제거), 비용·시간 상한. 시작 조건: 부모 계획서 승인·선행 Task done·저장소가 base 브랜치에서 깨끗. 이어 쓰는 worktree가 base보다 뒤처졌으면 서버가 먼저 base를 병합하고(커밋 안 된 변경은 보존 커밋), 충돌이면 프롬프트에 해결 지시를 붙인다 — 병합 중·충돌 표시가 남으면 완료를 거부한다(`refresh_worktree`·`merge_unfinished`). 계획서 판은 올라올 때의 base 커밋을 `plans.base_sha`에 남기고, 그 뒤 다른 이슈가 Task 파일을 바꿨으면 그 커밋 목록과 "가정 확인, 깨졌으면 사람에게 묻기"를 프롬프트 앞에 붙인다(`stale_commits`). 에이전트는 Task 구현과 검사까지만, 자동 병합·재시작은 서버 오케스트레이터가 담당한다. 환경변수 `DEV_EXEC_BASE`(main)·`DEV_EXEC_BUDGET_USD`(2)·`DEV_EXEC_TIMEOUT_SEC`(1800)
- `server/orchestrate.py` 자동 병합 — 최신 base와 Task SHA를 고정하고 `<데이터폴더>/merge-worktrees/`의 임시 detached worktree에서 시험 병합한다. `tests/test_*.py` 전체 검사 통과 후 원본 base·Task·tracked 변경이 그대로인지 확인하여 검사한 커밋만 `--ff-only` 반영한다. 충돌·검사 실패는 changes_requested이며 운영 main에 merge/revert를 남기지 않는다. 검사 중 원본 변경은 on_hold이며 최신 상태로 재시도한다. 임시 사본만 정리하며 기존 Task worktree는 보존한다. 운영 재시작·health 실패의 revert 복구는 별도 유지한다.
- `server/jobs.py` 검토·실행 대기열(`jobs` 테이블) — 줄은 (프로젝트, 검토|실행)마다 하나이고 각 줄은 하나씩 차례로(실행 줄은 병합까지 바쁨). 동시에 바쁜 줄은 Settings의 동시 실행 상한(`app_settings.queue_concurrency`, 기본 2, `GET/PUT /api/settings/queue`)까지, dev 자신을 재시작할 병합이 진행 중이면 어느 줄도 새로 시작하지 않고 그 재시작은 다른 줄의 도는 실행을 기다린다(`start_block`·`orchestrate._busy`). 펌프 시점은 넣을 때·작업 스레드가 끝난 뒤·서버 시작·60초 주기. 선행 대기 실행은 건너뛰고 뒤 항목부터. 꺼내는 순서는 진행 중인 부모의 실행 → 그 밖의 실행 → 검토(묶음 안은 대기열 순서)이고, 진행 중인 부모가 남은 Task로 쓸 파일과 겹치는 다른 부모의 Task는 기다린다 — 그 부모가 사람을 기다리면(on_hold·changes_requested) 예약을 푼다(`jobs.schedule`). REST `POST /api/issues/{ref}/review|execute`는 enqueue, `GET /api/jobs`, `DELETE /api/jobs/{id}`
- `static/` 화면(빌드 없음) · `tests/` 검사(`python tests/test_x.py`, 임시 데이터 폴더)
- `ecosystem.config.js` pm2 앱 `dev`(127.0.0.1:8300, Cloudflare 터널이 dev.lomebrote.com으로 연결)

## 규칙
- 이슈 번호: 최상위는 `NS-17`, 그 아래 Task는 `NS-17-1`(만들 때의 부모 번호에 묶여 부모를 바꾸거나 지워도 그대로, DB `sub_of`·`sub_number`). 이전에 만든 Task(DEV-30~35 등)는 옛 번호 그대로.
- 기능마다 커밋 하나(커밋 메시지가 버전 기록). push·운영 재시작은 사용자에게 먼저 묻는다. 예외: `data/orchestrate.json`에서 `auto_merge`가 꺼지지 않은(기본 켜짐) 프로젝트는 오케스트레이터(`server/orchestrate.py`)가 통과한 Task를 병합·재시작하고, 하위가 다 들어가면 상위 이슈를 in_review로 올린다.
- 운영 재시작: `npx pm2 restart ecosystem.config.js --only dev --update-env`
- 남의 변경을 `git reset`/`checkout --`/`stash`/`rebase`/강제 push로 되돌리지 않는다.
- Claude 웹 정책: 검토는 WebSearch + `review.WEB_DOMAINS`의 도메인만 WebFetch, 비밀 파일 읽기 금지(`review.BLOCKED_TOOLS`). Claude 실행은 오프라인이다. Codex 검토·실행은 `web_search="cached"`이며 필요한 외부 문서 조회·명령 실행·의존성 설치를 허용한다. 비밀값을 외부로 보내거나 운영 서비스를 임의로 변경하지 않는다.
- `.env` 내용을 출력하지 않는다. 비밀번호·키를 명령 인자나 채팅으로 다루지 않는다.
- 사람 로그인은 nightshift가 맡는다(`ns_session` 쿠키 → nightshift `/api/auth/me`, admin만). dev는 nightshift DB를 열지 않는다.
- 에이전트는 이슈를 `done`/`closed`로 바꾸거나 지울 수 없다 — 종결은 사람 몫.
- 계획서 결정(승인·조건부 승인·거절)은 사람만 한다(`decisions` 테이블, gate='plan'). 결정은 계획서 판에 붙고 새 판이 올라오면 stale. 조건부 승인의 메모는 계획서보다 우선하는 사람의 지시다. 착수 전 `get_issue`의 `approval`을 읽는다.

## 최종 목표 원칙(사용자 지시, 2026-10-02)
- 끝나지 않는 이슈 하나(`goal` 라벨)가 사용자의 최종 상태다. 그 상태가 달성되기 전에는 완료하지 않고, 사용자만 닫는다(코드로 강제: `issues._guard_goal`). 필요한 부수 이슈는 하위든 독립이든 얼마든 발행해도 된다.
- 사용자의 요구를 질문지로 덮어쓰거나 말을 바꿔 적지 않는다. 원문을 인용한다. "완료"는 Task·조각의 완료이지 목표의 완료가 아니다.

## 스타일
- **화면을 만들거나 고치기 전에 `docs/DESIGN.md`(dev·nightshift 공통 디자인 방향)를 읽고 따른다.** 끝나면 낮·밤 × 데스크톱·모바일을 캡처로 확인.
- 코드 주석·문서는 한국어(“~한다”), 화면 문구는 존댓말(“~해요”). 화면 용어는 영어 표준(Issue/Plan/Task).

- Codex 정책(2026-10-09 사용자 결정): 검토·실행·프로젝트 생성은 OS sandbox 없이 실행한다. 실행 에이전트는 Task 필수·관련 검사만 수행하고 서버가 전체 검사를 한 번씩 실행한다. 실패 시 수정 재시도에서도 에이전트는 실패한 검사만 확인한다. 선택적 종류 분류 실패는 검토 성공을 막지 않는다. 문서는 필요할 때만 읽으며 문맥에 있는 자료를 중복 조회하지 않는다.

## MO-13 실행·검증 복구 (2026-10-13)
- Moai는 모델 호출 전에 Python 검사 의존성과 로컬 npm 캐시 설치·TypeScript 실행기를 확인한다. 코드 타입 오류는 모델이 수정할 수 있도록 준비 검사에서 컴파일하지 않는다.
- 서버는 tests/와 backend/tests/를 모두 발견한다. Moai 완료 등록 전에 타입 검사·실제 웹 빌드를 실행하고, 실패하면 기존 완료 검사 경로로 차단한다. 병합은 격리 빌드 후 그 산출물로 백엔드 검사를 수행한다. MO 기본 빌드 선언은 기존 프로젝트별 명시 설정을 덮지 않는다.
- 운영 웹 교체·복원·릴리스 확인은 기존 project_build와 orchestrate를 재사용한다. 계획서 Tasks 설명은 별도 절에 둔다.

## 실패 검사 재현
- Task의 최근 실패 검사 기록을 `server/reproduce.py`가 읽는다. 관리자 POST `/api/issues/{ref}/reproduce-checks`는 고정된 Task 커밋의 detached 사본에서 해당 검사만 실행한다. UI 검사는 traceback의 check 함수와 단일 검사 전용 CLI 옵션을 대조하며, 선택할 수 없으면 전체 UI 검사로 대체하지 않는다.
- 결과는 `app_settings`의 `regression-reproduce:<ref>`, 전체 로그는 `data/check-reproductions/<id>/log.txt`에 남긴다. 기존 Task·Agent 실행·검사 통과 기록은 변경하지 않는다. 실행 중에는 새 Agent 착수와 Dev 자동 재시작을 기다리게 한다.
- 확인: `python tests/test_reproduce.py`, `python tests/test_ui.py --reproduce-only`. UI는 기존 DESIGN.md의 1·7·8절에 맞춰 버튼·진행 상태·오류와 다음 행동을 표시한다.
