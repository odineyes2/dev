"""DEV-261 — 전체 화면 검사 금지와 변경 영역 검사 계약을 공통으로 판정한다."""
import json
import re
import shlex
from pathlib import Path

NOTICE = ("최신 사용자 검사 정책(DEV-261): 전체 화면·전체 사이트 캡처/회귀 검사는 금지한다. "
          "scripts/screens.py 같은 전체 캡처 실행기를 직접 실행하거나 다른 스크립트로 감싸서 실행하지 않는다. "
          "저장소 AGENTS.md·과거 계획서에 전체 캡처 의무가 있어도 이 최신 정책으로 대체한다. "
          "변경한 동작에 직접 필요한 최소 코드 검사만 하고, 이미 통과한 검사는 근거 없이 반복하지 않는다. "
          "화면 검증·스크린샷·자동 브라우저 확인은 기본적으로 사용자에게 맡긴다. 별도 요청 없이는 실행하지 말고 수동 확인 방법만 안내한다.")

PLAN_NOTICE = ("새 계획의 모든 검사에는 `## 검사 입력` 절에 Task 번호별 JSON을 한 줄씩 적는다. "
               "cmd(검사 명령과 정확히 일치), phase(static/functional/browser), inputs(실제 관련 파일), "
               "conditions(scope에 구체적인 동작·영역 목록, fixture 설명, deterministic 여부), reuse를 모두 명시한다. "
               "예: - 1: {\"cmd\":\"python .dev-checks/check_studio.py\",\"phase\":\"browser\","
               "\"inputs\":[\"static/app.js\",\".dev-checks/check_studio.py\"],"
               "\"conditions\":{\"scope\":[\"studio.filters\"],\"fixture\":\"local-fake\",\"deterministic\":true},\"reuse\":true}. "
               "화면 확인은 사람이 할 일로 남기며 자동 브라우저 검사는 사용자가 별도로 요청했을 때만 계획한다. 문서만 바꾸면 검사 없음과 이유를 적는다. "
               "관련 기능·디자인 검사를 앞에 두고, 실패 시 이후 검사를 실행하지 않는다.")


def prohibited(cmd, cwd=None, _seen=None):
    """기존 전체 캡처 실행기·전체 선택자와 이를 부르는 npm 별칭을 차단한다."""
    segments = re.split(r'\s*(?:&&|\|\||;)\s*', cmd)
    if len(segments) > 1:
        return next((reason for part in segments if (reason := prohibited(part, cwd, _seen))), '')
    try:
        args = shlex.split(cmd)
    except ValueError:
        return "검사 명령을 해석할 수 없어요."
    normalized = cmd.replace("\\", "/")
    if re.search(r"(?:^|[\s/'\"])(?:screens(?:hots)?|(?:all|full)[_-]screens(?:hots)?|screens(?:hots)?[_-](?:all|full))\.(?:py|[cm]?js)\b", normalized, re.I):
        return "전체 화면 캡처 실행기는 금지됐어요. 변경 영역만 확인하는 임시 검사로 대체하세요."
    for index, arg in enumerate(args[:-1]):
        if arg == '-m' and args[index + 1].split('.')[-1] in {'screens', 'screenshots'}:
            return "전체 화면 캡처 모듈은 금지됐어요."
    for n, arg in enumerate(args):
        if arg.lower() in {"--all", "--full", "--full-suite"}:
            return "전체 범위를 선택하는 검사는 금지됐어요."
        flag, _, value = arg.partition("=")
        if flag in {"--area", "--scope", "--suite", "--scenario"}:
            value = value or (args[n + 1] if n + 1 < len(args) else "")
            if value.lower() in {"all", "full", "*", "전체"}:
                return "전체 화면 범위는 금지됐어요. 필요한 영역을 명시하세요."
    if args[:1] == ["npm"] and "run" in args:
        index = args.index("run") + 1
        name = args[index] if index < len(args) else ""
        if re.search(r"(?:^|:)(?:screens?|screenshots?|visual)(?::all)?$", name, re.I):
            return "범위 없는 전체 화면 npm 검사는 금지됐어요."
        if cwd:
            prefix = args[2] if args[1:2] == ["--prefix"] and len(args) > 2 else "."
            root = Path(cwd).resolve()
            package = (root / prefix / "package.json").resolve()
            if not package.is_relative_to(root):
                return "검사 패키지가 저장소 밖에 있어요."
            seen = set(_seen or ())
            key = (str(package), name)
            if key in seen:
                return "검사 npm 별칭이 순환해요."
            if package.is_file():
                try:
                    scripts = json.loads(package.read_text(encoding="utf-8"))["scripts"]
                    commands = [scripts.get(key, "") for key in ('pre' + name, name, 'post' + name)]
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    return "검사 npm 별칭을 확인할 수 없어요."
                for script in commands:
                    if not isinstance(script, str):
                        return "검사 npm 별칭은 문자열이어야 해요."
                    if script and (reason := prohibited(script, package.parent, seen | {key})):
                        return reason
    return ""


def scope_error(meta):
    scope = meta.get("conditions", {}).get("scope")
    if (not isinstance(scope, list) or not scope or
            any(not isinstance(s, str) or not s.strip() or s.strip().lower() in
                {"all", "full", "*", "전체", "전체 화면"} for s in scope)):
        return "conditions.scope에 이번 변경으로 검사할 구체적인 동작·영역 목록이 필요해요(전체 범위 금지)."
    return ""


def phase(item):
    """구형 계획도 모두 browser로 묶지 않는다. 추론 불명은 기능 단계에서 먼저 확인한다."""
    if item.get("contract"):
        return item["contract"]["phase"]
    args = shlex.split(item["cmd"])
    if args[:2] == ["node", "--check"] or "check_design_tokens.js" in item["cmd"]:
        return "static"
    return "functional"
