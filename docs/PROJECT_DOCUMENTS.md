# 프로젝트 문서 뷰어 — DEV-265

2026-11-22. 문서 원본은 각 프로젝트 Git 저장소의 `/docs`에 둔다. Dev는 원격 저장소 기본 브랜치의 문서를 읽기 전용으로 보여준다. 별도 문서 생성·편집·승인·DB 사본을 만들지 않는다. 예전 공식 문서 생성 기능은 현재 기준 코드에서 이미 제거돼 있어 이를 복원하지 않고 문서 뷰어만 추가했다.

## 사용 방법

1. 프로젝트 관리에서 Repository에 GitHub 저장소 주소를 저장한다.
2. 로컬 `/docs`에 Markdown을 작성하고 **원격 기본 브랜치에 커밋·푸시**한다.
3. Dev → Projects → 프로젝트 → **문서**를 연다.
4. 목록에서 문서를 고르고 **주소 복사**로 공유한다. 공유 주소도 기존 Dev 로그인이 필요하다.
5. 방금 푸시한 내용은 **새로고침**으로 확인한다. 일반 조회는 최대 60초 동안 캐시를 재사용하며 자동 폴링하지 않는다.

예: `https://dev.lomebrote.com/#/projects?project=CITY&tab=docs&doc=docs%2FV3_2_DEVELOPMENT.md`

`docs/HANDOFF.md`, `docs/README.md`, `docs/INDEX.md` 순으로 진입 문서를 우선 선택하고, 없으면 경로순 첫 문서를 연다. 하위 폴더 경로와 한글·공백 이름도 보존한다. 문서 제목의 `#fragment` 링크와 문서 사이 상대 링크를 지원한다. 목록은 데스크톱에서 왼쪽, 모바일에서는 본문 위의 접을 수 있는 영역이다.

지원 문서: UTF-8 `.md`, `.markdown`, `.txt`. 제목·목록·체크리스트·인용·구분선·코드 블록·기본 표·링크를 표시한다. Markdown 원문 HTML은 실행하지 않고 문자로 보여준다. 이미지는 `/docs` 안의 PNG·JPEG·GIF·WebP만 지원하며 외부 이미지·SVG·임의 HTML·JavaScript·Mermaid 렌더링은 지원하지 않는다. `/docs` 밖을 가리키는 상대 링크도 열지 않는다. 외부 HTTPS/HTTP 링크는 별도 탭으로 연다. 중첩 목록·복잡한 Markdown 확장 문법은 간소화해서 표시한다.

## 원본과 인증

- `server/project_documents.py`가 프로젝트에 등록된 **원격 GitHub 주소**를 정규화하고 기본 브랜치를 조회한다. HTTPS 및 GitHub SSH 주소를 지원한다. 현재 로컬 브랜치·미커밋 변경·등록 Local path는 읽지 않는다.
- 서버의 기존 Git Credential Manager 또는 SSH 인증을 사용한다. 비공개 저장소를 읽으려면 서버 사용자에게 해당 저장소의 읽기 권한이 있어야 한다. 브라우저로 Git 자격 정보를 전달하지 않는다.
- `data/document-repositories/<원격 주소 해시>/`에 전용 bare 캐시를 둔다. shallow·blob:none fetch 후 필요한 문서 blob만 지연 조회하므로 작업 저장소의 checkout·branch·remote를 변경하지 않는다.
- 모든 REST 조회는 기존 Dev 인증 미들웨어를 거친다. MCP는 기존 에이전트 인증을 거친다. 현재 시스템의 기존 프로젝트 읽기 권한 범위를 그대로 따른다.
- 응답에 기본 브랜치·기준 commit·원격 확인 시각을 포함한다. 새로고침 실패 시 실패를 명시하고 오래된 결과를 최신처럼 표시하지 않는다. 캐시는 서버 내부에만 두며 응답은 no-store다.
- 문서는 1MiB, 이미지 2MiB, 목록 1,000개로 제한한다. Git 트리의 일반 파일만 읽으며 symlink/submodule·숨김 경로·경로 이탈은 제외한다. 자격 정보로 보이는 본문은 기존 repo_snapshot 탐지 규칙으로 표시를 막는다. 탐지 결과가 예제 문자열인 경우에도 차단될 수 있다.
- 이미지 요청에는 문서 기준 commit을 붙인다. 원격 기준이 변경되면 다른 버전 이미지를 섞어 표시하지 않으며 문서를 새로고침해야 한다.

## 모듈과 API

| 파일 | 역할 |
| --- | --- |
| `server/project_documents.py` | 원격 기본 브랜치·격리 캐시·문서/이미지 검증·원문 조회 |
| `server/app.py` | 로그인 보호 REST 세 경로 및 새 JS의 내용 해시 |
| `server/mcp_tools.py` | `list_project_documents(project, refresh)`·`read_project_document(project, path, refresh)` |
| `static/project_docs.js` | 문서 목록/본문·안전한 Markdown·문서 간 이동·주소 복사 |
| `static/app.js` | Projects의 문서 탭·공유 주소 라우팅 연결 |
| `static/app.css` | 기존 디자인 토큰을 사용하는 모바일/데스크톱 문서 레이아웃 |
| `static/index.html` | 뷰어 스크립트 로딩 |

REST는 `/api/projects/{key}/documents`, `/documents/read?path=docs/...`, `/documents/asset?path=docs/...&commit=...`이다. 문서 생성/수정 API는 없다. DB 스키마·기존 문서·프로젝트 저장 데이터는 변경하지 않았다. 기존 정적 파일 내용 해시 정책을 확장해 새 스크립트 캐시도 갱신한다.

## 검증

필요한 최소 검사만 수행했다. `.dev-checks/test_docs_viewer.py`는 임시 DB와 실제 Git 커밋 트리를 만들고 원격 전송만 로컬 fixture로 대체해 다음을 검증했다: 기본 브랜치·우선 문서·한글 경로·미커밋 파일 제외·캐시/명시 갱신·잘못된 경로·symlink 제외·용량/UTF-8/민감값 검사·이미지 commit·인증 없는 REST 거절·관리자 읽기·읽기 전용 API·정적 해시. `.dev-checks/docs-markdown.cjs`는 브라우저 없이 코드/표/제목/체크리스트·HTML 이스케이프·위험 URL·상대 링크/이미지·한글 공유 주소를 확인했다. 두 검사와 변경 Python/JS 문법 검사가 통과했다. 저장소 정책에 따라 이슈별 검사는 커밋하지 않았다.

실제 CITY 원격 저장소 `main`, commit `9de3c693e5f4b41c7f4a93ced162501a8deeefc2`의 문서 3개와 `docs/V3_2_DEVELOPMENT.md` 12,649바이트를 읽었다. 사용자 선호에 따라 **브라우저 실행·화면 캡처는 하지 않았다**. DESIGN 2~4, 7~8, 10~11절을 적용했고 공유 디자인 문서/토큰은 바꾸지 않았다.

수동 확인: 모바일에서 위 CITY 문서 주소 열기 → 로그인 후 본문 확인 → 목록에서 다른 문서 이동 → 주소 복사 → 푸시 후 새로고침. 화면의 실제 줄바꿈·모바일 터치·클립보드 권한은 사용자가 확인할 항목이다.

## 남은 범위

GitHub 이외 호스팅, 브랜치 선택·과거 버전 탐색, 온라인 편집, PDF/Word 미리보기는 제외했다. 서버 네트워크/인증 문제는 문서 탭에서 오류로 표시한다. 오래된 MCP 클라이언트는 도구 목록 새로고침이 필요할 수 있다. 프로젝트 인수인계는 `/docs/HANDOFF.md`를 최신 상태로 유지하고 이 문서를 먼저 읽도록 해당 프로젝트 AGENTS.md에 지정하는 방식이 권장된다.
