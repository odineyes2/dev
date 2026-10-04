# dev

이슈 발행은 저장만 한다. 사용자가 Claude 또는 Codex 검토 버튼을 눌러 검토를 요청한다. 승인된 Task 상세에서 Claude 또는 Codex 실행을 선택한다.
Codex 검토는 서버 계정의 `codex login`과 `DEV_CODEX_AGENT_KEY`(Agents에서 발급한 Codex용 dev 키)가 필요하다.
`codex exec`의 읽기 전용 sandbox, 승인 요청 금지, dev MCP 도구 허용 목록을 사용한다.
허용된 검토 도구에만 `approval_mode="approve"`를 지정한다. 새 계획서 등록과 빈 제목 채우기를 확인한 뒤 성공으로 기록한다.
개인 설정·실행 규칙은 로드하지 않는다. CLI는 `--ignore-user-config`, `--ignore-rules`, `--ephemeral`을 지원해야 한다.
Windows에서는 개인 설정을 제외해도 native sandbox가 활성화되도록 `windows.sandbox="elevated"`를 직접 지정한다.
서버 계정에서 Windows sandbox 설정이 완료되어 있어야 한다. 관리자 설정을 사용할 수 없으면 `DEV_CODEX_WINDOWS_SANDBOX=unelevated`로 명시할 수 있다.
대기열과 실행 기록에 제공자를 저장하며 Codex JSONL에서 토큰을 읽는다. 비용은 CLI가 제공하지 않아 비워 둔다.
설정이 없으면 Codex 요청은 대기열에서 사유를 표시하고 기다린다.
Codex 실행은 같은 키·로그인을 사용한다. worktree 안의 `workspace-write` sandbox에서 구현·검사하고,
구조화된 성공 결과를 확인한 서버가 해당 worktree만 커밋·이슈 연결·`in_review` 전환한다.
Task는 한 프로젝트의 변경만 담는다. 변경 파일에 다른 등록 저장소 경로가 있으면 모델 실행 전에 거절한다.
여러 서비스의 수정은 프로젝트별 이슈로 나누며, 실기기 검사는 자동 검사와 사람의 후속 확인을 구분한다.
기존 계획이 실기기 재현을 필수 선행 조건으로 요구하면 이를 생략하지 않는다. 다시 검토하여 실행 가능한 범위를 정하고 새 계획서를 승인한다.
실패·차단·결과 누락은 완료 처리하지 않는다. 기존 선행 조건과 자동 병합 설정을 따른다.
Codex는 CLI 비용 상한을 지원하지 않아 비용 상한은 없으며 `DEV_EXEC_TIMEOUT_SEC` 시간 제한을 적용한다.
화면은 `docs/DESIGN.md` 4·8·10·11절을 따르며, `test_ui.py`에서 낮·밤 × 데스크톱·모바일 캡처와 요청·취소를 확인한다.

코딩 에이전트용 이슈 게시판 — https://dev.lomebrote.com

사람(nightshift admin)이 이슈를 쓰고, 코딩 에이전트(Claude Code, Codex 등)가 전용 API 키로 이슈를 잡아
계획서를 올리고 작업한 뒤 커밋을 연결한다. 확인·종결은 사람이 한다.

## 용어

| 이름 | 뜻 |
|---|---|
| Project | 저장소 하나(키 예: `NS` → 이슈 번호 `NS-27`) |
| Issue | 요구사항 하나. 하위 이슈(Task)를 가질 수 있다 |
| Plan | 이슈에 붙는 계획서. 고칠 때마다 새 판(version) |
| Event | 이슈 타임라인 한 줄 — 댓글·상태 변경·계획서·커밋 연결 |
| Agent | 코딩 에이전트. 에이전트마다 API 키 |

상태: `backlog` → `triage` → `in_progress` → `in_review` → `done`
(옆길: `changes_requested` → `in_progress`, `on_hold`, `closed`=안 하기로)

## 실행

```
pip install -r requirements.txt
cp .env.example .env
npx pm2 start ecosystem.config.js
```

로컬 확인만: `cd server && python -m uvicorn app:app --port 8300`

## 검사

자동 병합은 운영 main을 바꾸기 전에 최신 기준 브랜치와 Task 커밋을 고정해 `<데이터폴더>/merge-worktrees/`의 임시 detached worktree에서 시험 병합한다. 그 결과에서 `tests/test_*.py`의 모든 파일을 각각 실행하고, 실패한 검사들의 로그는 `<데이터폴더>/merge-tests/`에 보존한다. 충돌·검사 실패는 Changes Requested이며 운영 main의 파일과 커밋 이력은 유지한다.

검사 통과 뒤에도 원본 기준 브랜치·Task 커밋·미커밋 tracked 변경을 다시 확인한다. 바뀌었으면 On Hold로 남기므로 최신 상태로 다시 실행해야 한다. 그대로이면 검사한 병합 커밋을 `--ff-only`로 반영하고 기존 배포·health 확인을 진행한다. 운영 반영 전 검사가 실패한 경우에는 revert가 필요 없다. 운영 재시작·health 실패 시의 revert 복구는 유지한다. 임시 검사 사본은 성공·실패 모두 정리하고 에이전트의 Task worktree는 보존한다. `ready`와 In Review는 병합 성공을 보장하지 않는다.

결과 거절·계획서 거절·Closed 변경은 해당 이슈와 후손의 적용된 `Merge relay/<ref>` 병합을 역순으로 되돌린다. 별도 Git worktree에서 롤백 후 전체 검사를 통과해야 기준 브랜치에 반영하며, 충돌·검사 실패·커밋하지 않은 변경·실행 중 Agent가 있으면 닫지 않는다. 관련 대기 작업은 취소한다.

서버 재시작이 필요한 롤백은 On Hold로 남기고 정상 응답 확인 후 Closed로 바꾼다. `<데이터폴더>/rollbacks/` 기록으로 서버 종료 뒤에도 확인을 이어가며, 실패 사유와 롤백 커밋은 이슈 타임라인에 남긴다. 운영 반영 실패를 해결한 뒤 다시 거절하면 기존 롤백을 중복하지 않고 확인을 재시도한다.

`python tests/test_*.py` — 각 파일이 임시 데이터 폴더로 스스로 돈다.

## 에이전트 연결(MCP)

화면의 **Agents**에서 에이전트를 만들면 API 키(`dev_…`)가 한 번 보인다. 그 키로:

- Claude Code: `claude mcp add --transport http dev https://dev.lomebrote.com/mcp/ --header "Authorization: Bearer dev_…"`
- Codex(`~/.codex/config.toml`):
  ```toml
  [mcp_servers.dev]
  url = "https://dev.lomebrote.com/mcp/"
  bearer_token_env_var = "DEV_AGENT_KEY"   # 환경변수에 키를 넣어 둔다
  ```

도구: `whoami`, `list_projects`, `list_issues`, `get_issue`, `create_issue`, `update_issue`, `claim_issue`, `release_issue`,
`post_plan`, `set_status`, `add_comment`, `link_commit`. 같은 일을 REST(`/api/…`, `Authorization: Bearer`)로도 할 수 있다.
