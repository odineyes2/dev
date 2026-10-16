# dev — 에이전트 작업 규칙 (Claude·Codex 공통)

짧게 유지할 것. 계획서: `~/.claude/plans/dev-board-phase1.md`.

## 핵심 모듈 동작
파일마다 무엇이 있는지는 **자동 파일 지도**(`server/filemap.py`, 각 파일 첫 줄 설명)가 맡는다. 에이전트는 프롬프트로 받고, 대화형 세션은 `python server/filemap.py .`로 본다. 새 파일은 첫 줄에 역할을 적는다. 여기는 파일 하나만 봐서는 안 보이는 동작·불변식만 적는다.

### 에이전트에게 주는 것
- 프롬프트 순서: 공통 하네스(`harness/COMMON.md`, `harness.py`) → 파일 지도(`filemap.py`, 실행은 Task worktree 기준) → 코드 속 프롬프트(`harness/prompts/*.md`를 `prompts.py`가 자리표시 `{ref}` 등으로 채움) + 코드가 상황에 따라 덧붙이는 조각(문서 안내·검사 범위·진행 중 이슈·낡은 계획 알림·재검토 메모). 앞 둘은 `review.preamble()`.
- 하네스·프롬프트 파일은 실행마다 읽어 다음 검토·실행부터 반영된다. MCP 안내문(`mcp_tools.INSTRUCTIONS` = 작업 흐름 + COMMON)은 재시작 뒤 반영.
- 프로젝트별 규칙은 각 저장소 AGENTS.md(CLAUDE.md는 `@AGENTS.md`만). 허용·차단 도구·한도 같은 코드 속 설정값은 코드에 둔다(보안 경계 — 화면에서 못 고친다).
- Harness 탭(`harness_view.py`·`harness_edit.py`): 조회는 실제 조립 함수(`prompt_for`·`command_for`)를 그대로 불러 보여주고, 편집은 허용 목록 파일만 base 브랜치에 그 파일만 커밋한다(push 없음). 실행 중 Task·읽은 뒤 바뀐 파일·자리표시 오류(`prompts.problems`)는 거절.

### 저장소·권한
- `db.py` — 마이그레이션은 MIGRATIONS 뒤에 덧붙이기만(PRAGMA user_version). 쓰는 프로세스는 app 하나(WAL·짧은 연결). 상태 변경·댓글·계획서·커밋 연결은 모두 events 한 줄(타임라인 = 감사 기록).
- `issues.py` — REST와 MCP가 같이 쓰는 저장소·권한 판단. 잡기(claim)는 lease가 지나면 풀린다. 계획서 결정은 `decisions` 테이블(gate='plan')에 판별로 붙고 새 판이 올라오면 stale. 승인 때 `## Tasks` 줄로 하위 Task를 만들고, 조건부 승인 메모는 각 Task 본문에 붙인다. 에이전트의 done/closed·지우기 금지와 goal 이슈 보호(`_guard_goal`)는 여기서 코드로 막는다. goal 이슈는 사용자의 최종 상태 하나이고, 부수 이슈는 하위든 독립이든 얼마든 발행해도 된다.
- `auth.py` — 사람은 nightshift 세션(`ns_session` → `/api/auth/me`, admin만), 에이전트는 `Bearer dev_…` 키(DB엔 해시만).

### 검토·실행·병합
- `review.py` "Claude에게 검토 맡기기"(헤드리스 `claude -p --restricted`, 읽기·웹 일부만) — 프롬프트 끝에 같은 프로젝트의 진행 중인 이슈와 남은 Task 파일을 붙여 계획이 겹침을 알고 쓰게 한다(`in_flight_section`). 실행마다 `runs` 한 줄(상태·토큰·비용·로그), 원본 이벤트(도구 호출·턴별 사용량)는 `data/reviews/<로그>.events.jsonl.gz`(요약 `python scripts/run_usage.py --last 3`). 서버가 뜰 때 남은 running은 orphaned로.
- `execute.py` "Claude/Codex에게 실행 맡기기"(Task 전용) — `~/Projects/.dev-worktrees/<ref>`(저장소 밖이라 dev 규칙 파일이 다른 프로젝트 Task에 섞이지 않는다, 검사는 임시 데이터 폴더 안) worktree + `relay/<ref>` 브랜치에서 헤드리스로 구현. Claude 허용 목록은 Bash의 `python tests/*`·읽기성 git·add·commit과 Moai의 `backend.tests` 검사·지정 npm 타입 검사/빌드이고, Claude는 사용자 개인 설정(플러그인·훅·스킬·~/.claude/CLAUDE.md·자동 메모리)을 받지 않는다(`USER_ISOLATION`). Codex는 danger-full-access로 OS sandbox 없이 실행하고 커밋·결과 등록은 서버가 한다. push 불가(pushurl 덮기·토큰 제거), 비용·시간 상한. Claude 실행기는 `rm`·`git rm`을 못 해 삭제 Task는 대화에서 직접 한다.
  - 시작 조건: 부모 계획서 승인·선행 Task done·저장소가 base 브랜치에서 깨끗. 이어 쓰는 worktree가 base보다 뒤처졌으면 서버가 먼저 base를 병합하고(커밋 안 된 변경은 보존 커밋), 충돌이면 프롬프트에 해결 지시를 붙인다 — 병합 중·충돌 표시가 남으면 완료를 거부한다(`refresh_worktree`·`merge_unfinished`).
  - 계획서 판은 올라올 때의 base 커밋을 `plans.base_sha`에 남기고, 그 뒤 다른 이슈가 Task 파일을 바꿨으면 그 커밋 목록과 "가정 확인, 깨졌으면 사람에게 묻기"를 프롬프트 앞에 붙인다(`stale_commits`).
  - 완료 등록 전에 서버가 Task worktree에서 전체 검사를 돌려 실패하면 한 번 스스로 고치게 한다. 환경변수 `DEV_EXEC_BASE`(main)·`DEV_EXEC_BUDGET_USD`(2)·`DEV_EXEC_TIMEOUT_SEC`(1800).
- `orchestrate.py` 자동 병합 — 최신 base와 Task SHA를 고정하고 `<데이터폴더>/merge-worktrees/`의 임시 detached worktree에서 시험 병합한다. `tests/test_*.py` 전체 검사 통과 후 원본 base·Task·tracked 변경이 그대로인지 확인하여 검사한 커밋만 `--ff-only` 반영한다. 충돌·검사 실패는 changes_requested이며 운영 main에 merge/revert를 남기지 않는다. 검사 중 원본 변경은 on_hold이며 최신 상태로 재시도한다. 병합 뒤 재시작은 `data/orchestrate.json`의 `pm2_app`·`restart_when`이 있는 프로젝트만(busy·health 확인), push는 하지 않는다.
- `jobs.py` 검토·실행 대기열 — 줄은 (프로젝트, 검토|실행)마다 하나이고 각 줄은 하나씩 차례로(실행 줄은 병합까지 바쁨). 동시에 바쁜 줄은 Settings의 동시 실행 상한(`app_settings.queue_concurrency`, 기본 2)까지, dev 자신을 재시작할 병합이 진행 중이면 어느 줄도 새로 시작하지 않는다(`start_block`·`orchestrate._busy`). 펌프는 넣을 때·스레드가 끝난 뒤·서버 시작·60초 주기. 꺼내는 순서는 진행 중인 부모의 실행 → 그 밖의 실행 → 검토이고, 진행 중인 부모가 남은 Task로 쓸 파일과 겹치는 다른 부모의 Task는 기다린다 — 그 부모가 사람을 기다리면(on_hold·changes_requested) 예약을 푼다(`jobs.schedule`). 넣기(enqueue)는 사람만.
- `automation.py`·`auto_settings.py` — Auto 검토·계획 승인·실행·Task 승인은 Settings의 전역 위임이 켜졌을 때만 대기열에 넣는다(자동 검토는 backlog 이슈만, 잡힌 이슈·goal은 제외).
- `regression.py` — 검사 선택은 사람 설정 > 실제 고위험 변경 > 승인 계획의 `## 검사 선택` JSON > 변경 범위 기본값 순. 실행 프롬프트의 검사 범위와 서버의 완료 전·병합 검사가 같은 결정을 쓴다. 서버는 `tests/`와 `backend/tests/`(Moai)를 모두 찾는다.
- Moai(MO) — 모델 호출 전에 Python 검사 의존성·로컬 npm 캐시 설치·TypeScript 실행기를 확인하되 코드 타입 오류는 모델이 고치도록 준비 검사에서 컴파일하지 않는다. 완료 등록 전에 타입 검사·실제 웹 빌드가 실패하면 완료를 막고, 병합은 격리 빌드 산출물로 백엔드 검사를 한다. 운영 웹 교체·복원·릴리스 확인은 `project_build`(검사한 커밋 격리 빌드 → rename 교체, 실패 시 복원)와 orchestrate를 쓴다. MO 기본 빌드 선언은 프로젝트별 명시 설정을 덮지 않는다(지금 `MO.build=[]`).

### 실패 뒤 처리
- `rollback.py` — 계획서 거절·Closed가 된 이슈의 병합을 검사 후 revert하고 재시작까지 확인한 뒤 닫는다(아직 적용된 병합은 first-parent 이력으로 계산).
- `troubleshoot.py` — 실패한 승인 Task를 같은 대기열에서 한 번 복구 실행한다(원 실행 기록 보존, 추가 호출 1회·짧은 시간 상한).
- `reproduce.py` 실패 검사 재현 — 관리자 POST `/api/issues/{ref}/reproduce-checks`가 고정된 Task 커밋의 detached 사본에서 그 검사만 돌린다(UI 검사는 traceback의 check 함수와 단일 검사 CLI 옵션을 대조, 고를 수 없으면 전체 UI 검사로 대체하지 않는다). 결과는 `app_settings`의 `regression-reproduce:<ref>`, 로그는 `data/check-reproductions/<id>/log.txt`. 시작 시 Task In Progress, 실패 시 Changes Requested, 통과하면 In Progress로 기존 병합 처리기에 넘겨 병합·운영 반영 확인 뒤에만 In Review. 상태·커밋이 바뀌면 결과가 새 작업을 덮지 않고, 재시작 중단·자동 병합 OFF·승인 변경·정보 부족은 On Hold. 미반영 Task의 완료 승인은 UI·서버에서 막고, 재현 중에는 새 Agent 착수와 Dev 자동 재시작을 기다리게 한다. 확인: `python tests/test_reproduce.py`·`test_reproduce_followup.py`·`python tests/test_ui.py --reproduce-only`.
- `qa.py`·`qa_runner.py` — 최상위 In Review Issue의 QA. 판정 대상 스냅샷과 요구 항목(본문 줄·Plan 확인 항목)을 고정하고 서버가 명령을 검증·실행한다. Auto QA가 켜졌을 때만 결과를 상태에 반영한다(기본 OFF).

### 프로젝트 관리
- `project_init`(기획서 → 새 저장소·개발 초안), `project_docs`(공식 문서 7종 요청, 실행 중엔 worktree에서 읽음), `project_description`(Description 자동 작성 Issue), `project_audit`·`_runner`(기준 SHA 고정·읽기 전용 점검), `project_service`·`service_prepare`(서비스 연결·공용 터널 변경은 한 번에 하나), `project_restart`(pm2 재시작 — dev 자기 재시작은 별도 프로세스가 health까지 확인).

### 화면·검사·운영
- `static/` 화면(빌드 없음, 정적 파일. Cloudflare가 CSS/JS를 4시간 캐시해 index.html이 `?v=내용해시`로 부른다).
- `tests/` 검사(`python tests/test_x.py`, 임시 데이터 폴더). 화면 검사는 `python tests/test_ui.py --<이름>-only`. 관리 화면 검사는 경로별 활성 탭·프로젝트 식별·로딩 완료를 확인한 뒤 포커스를 검증하고, 실패 시 경로·포커스 요소·활성 탭·busy 수만 기록한다(입력값·비밀 제외). boot는 프로젝트·Agent 초기 데이터 준비 뒤에 화면을 공개하고 숨겨진 로그인/초기화 화면에서는 hash 라우팅을 하지 않는다. `--management-delay-probe`(응답 보류·탭 전환·초기화 지연), `--management-layout-only`.
- `ecosystem.config.js` pm2 앱 `dev`(127.0.0.1:8300, Cloudflare 터널이 dev.lomebrote.com으로 연결).

## 규칙
- 이슈 번호: 최상위는 `NS-17`, 그 아래 Task는 `NS-17-1`(만들 때의 부모 번호에 묶여 부모를 바꾸거나 지워도 그대로, DB `sub_of`·`sub_number`). 이전에 만든 Task(DEV-30~35 등)는 옛 번호 그대로.
- 공통 규칙(사람의 결정, 읽기·고치기, git·커밋, 검사, 비밀값, 말투)은 `harness/COMMON.md`. 아래는 dev에만 해당하는 것.
- 자동 병합: `data/orchestrate.json`에서 `auto_merge`가 꺼지지 않은(기본 켜짐) 프로젝트는 오케스트레이터(`server/orchestrate.py`)가 통과한 Task를 병합·재시작하고, 하위가 다 들어가면 상위 이슈를 in_review로 올린다.
- 운영 재시작: `npx pm2 restart ecosystem.config.js --only dev --update-env`
- Claude 웹 정책: 검토는 WebSearch + `review.WEB_DOMAINS`의 도메인만 WebFetch, 비밀 파일 읽기 금지(`review.BLOCKED_TOOLS`). Claude 실행은 오프라인이다. Codex 검토·실행은 `web_search="cached"`이며 필요한 외부 문서 조회·명령 실행·의존성 설치를 허용한다. 비밀값을 외부로 보내거나 운영 서비스를 임의로 변경하지 않는다.
- 사람 로그인은 nightshift가 맡는다(`ns_session` 쿠키 → nightshift `/api/auth/me`, admin만). dev는 nightshift DB를 열지 않는다.
- Codex 정책(2026-10-09 사용자 결정): 검토·실행·프로젝트 생성은 OS sandbox 없이 실행한다. 실행 에이전트는 Task 필수·관련 검사만 수행하고 서버가 전체 검사를 한 번씩 실행한다. 실패 시 수정 재시도에서도 에이전트는 실패한 검사만 확인한다. 선택적 종류 분류 실패는 검토 성공을 막지 않는다.

## 스타일
- 화면 용어는 영어 표준(Issue/Plan/Task). 말투는 공통 하네스.
- 화면을 만들거나 고치기 전에 `C:\Users\Simon Lomebrote\Projects\dev\docs\DESIGN.md`(dev·nightshift 공통 디자인 방향 — 작업 폴더가 worktree여도 이 절대 경로로 연다)를 읽고 따른다. 새 색·크기·모서리 값을 만들지 말고 그 문서의 단계 안에서 고른다. 끝나면 낮·밤 × 데스크톱·모바일을 캡처로 확인한다.
