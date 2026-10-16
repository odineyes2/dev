UI Task면 완료 전에 바뀐 화면만 직접 확인한다.
- 현재 worktree에 scripts/capture_ui.py가 있으면 `python scripts/capture_ui.py --route "#/..."`로 바뀐 로컬 hash 화면을 캡처한다. 상세 화면은 지원되는 --fixture를 함께 지정한다.
- 화면을 바꾸지 않은 서버 Task는 캡처를 실행하지 않는다. 환경·권한 오류로 캡처가 실패하면 같은 오류를 반복 시도하지 않고 이유를 기록한다.
- 임시 데이터·테스트 admin만 사용한다. 운영 데이터·.env·외부 URL·운영 서버는 사용하지 않으며 다운로드/설치나 다른 저장소 수정으로 도구를 마련하지 않는다.
- 낮·밤 × 데스크톱 1300·모바일 390 PNG를 {viewer}로 실제 열어 보고, DESIGN.md 기준으로 잘림·겹침·가로 스크롤을 확인한다. 필요한 수정 뒤 재캡처하고 다시 연다. 바뀐 화면만 열어 이미지 토큰을 아낀다.
- 도구가 없는 프로젝트(별도 도구 구현 전 nightshift 포함)는 미지원 이유를 요약에 적고 진행한다. 캡처 실패·부분 성공은 확인한 PNG 파일과 실패 이유를 완료 요약에 그대로 남긴다. 캡처 실패만으로 blocked/on_hold로 만들지 않는다. 구현·필수 기능 검사 실패는 이 예외로 덮지 않는다.
- .ui-captures/와 tests/shots/ 안 PNG·manifest·로그·임시 DB 전체를 커밋하지 않는다. git add는 `git add -A`만 쓰고(exclude pathspec은 ignore된 폴더에서 실패한다) 커밋 전 staged diff에 산출물이 없는지 확인한다.
