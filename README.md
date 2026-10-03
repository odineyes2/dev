# dev

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

## Codex 검토 버튼

이슈 화면의 **Codex에게 검토 맡기기**는 홈서버에서 `codex exec`를 읽기 전용 sandbox로 실행한다. 서버를 돌리는 계정이
Codex CLI에 로그인되어 있어야 하고, 위 `[mcp_servers.dev]` 설정과 `DEV_AGENT_KEY` 환경변수가 서버 프로세스에도 필요하다.
실행 파일·제한 시간은 `DEV_CODEX_BIN`, `DEV_CODEX_TIMEOUT_SEC`로 바꿀 수 있다. Codex는 코드를 고치지 않고 dev MCP로
계획서·질문을 남긴다. 서버 실행은 Windows daemon의 권한 상속 문제를 피하도록 `codex --no-daemon exec`를 사용한다.

`test -f ~/.codex/config.toml`은 파일이 있으면 아무것도 출력하지 않고 종료 코드 0을 반환한다. 설치 위치와 설정을 확인하려면
각각 `command -v codex`, `codex mcp list`를 사용한다. JupyterLab 터미널에서 daemon 권한 오류가 나면 `codex --no-daemon`으로
대화형 실행을 확인할 수 있다.

`codex mcp list`의 `enabled`는 설정을 읽었다는 뜻일 뿐, Bearer 키가 현재 프로세스에 들어 있다는 뜻은 아니다. 직접 실행하는
터미널은 프로젝트의 `.env`를 자동으로 읽지 않으므로 같은 터미널에서 `export DEV_AGENT_KEY='dev_…'`를 먼저 실행해야 한다.
키 값을 출력하지 않고 전달 여부만 확인하려면 `test -n "$DEV_AGENT_KEY" && echo set || echo missing`을 사용한다. MCP 호출에서
HTTP 401과 `에이전트 키 ... 필요`가 나오면 설치 위치가 아니라 이 환경변수가 누락됐거나 유효하지 않은 것이다. 새 터미널을
열면 다시 설정하거나 사용자 환경변수/비밀 저장소를 통해 주입해야 하며, 실제 키를 Git에 커밋해서는 안 된다.
