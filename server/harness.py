"""공통 하네스(DEV-131) — 모든 프로젝트·모든 에이전트가 받는 규칙 `harness/COMMON.md`.

검토·실행 프롬프트 앞에 붙고(review·execute), dev MCP 안내문에도 들어간다(mcp_tools). 프로젝트별 규칙은 각 저장소의 AGENTS.md.
"""
import config

PATH = config.REPO_ROOT / "harness" / "COMMON.md"


def text() -> str:
    """실행마다 읽는다 — 파일을 고치면 다음 검토·실행부터 반영된다(MCP 안내문은 재시작 뒤)."""
    try:
        return PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def prompt_section() -> str:
    t = text()
    return t + "\n\n---\n\n" if t else ""
