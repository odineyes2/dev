"""코드 속 프롬프트(DEV-135) — 검토·실행 에이전트에게 주는 작업 지시문은 harness/prompts/*.md에 있고, 여기서 자리표시를 채운다.

글(무엇이라고 말할지)은 파일에, 언제 무엇을 붙일지(조건·DB 조회)는 review·execute 코드에 둔다. 자리표시는 `{이름}`이고 코드가 채운다.
파일은 실행마다 읽어 고치면 다음 검토·실행부터 반영된다. 화면 편집 저장 전에는 problems()로 검증한다.
"""
import re

import config

DIR = config.REPO_ROOT / "harness" / "prompts"
SLOT = re.compile(r"\{([a-z_]+)\}")
# 파일마다 코드가 채우는 자리표시. 목록 밖 이름은 채워지지 않고 글자 그대로 남으니 저장 때 막는다.
SLOTS = {
    "review_claude.md": {"ref", "web_domains"},
    "review_codex.md": {"ref", "web_domains"},
    "description_claude.md": {"ref"},
    "description_codex.md": {"ref"},
    "execute_claude.md": {"ref", "branch", "parent_line", "design_doc", "api_test", "test_scope"},
    "execute_codex.md": {"ref", "branch", "parent_ref", "design_doc", "api_test", "test_scope"},
    "capture.md": {"viewer"},
}
# 빠지면 에이전트가 어느 이슈·어떤 검사 범위인지 모르게 되는 자리표시
REQUIRED = {name: ({"ref"} if name != "capture.md" else set()) | ({"test_scope"} if name.startswith("execute_") else set())
            for name in SLOTS}


def problems(name: str, text: str) -> list[str]:
    """저장해도 되는지 — 모르는 파일·모르는 자리표시·빠진 필수 자리표시. 빈 목록이면 괜찮다."""
    if name not in SLOTS:
        return [f"모르는 프롬프트 파일이에요: {name}"]
    found = set(SLOT.findall(text))
    out = [f"모르는 자리표시 {{{s}}} — 쓸 수 있는 것: " + ", ".join(f"{{{a}}}" for a in sorted(SLOTS[name])) for s in sorted(found - SLOTS[name])]
    out += [f"필수 자리표시 {{{s}}}가 빠졌어요" for s in sorted(REQUIRED[name] - found)]
    return out


def render(name: str, **values) -> str:
    """파일을 읽어 자리표시를 채운다. 파일이 없으면 오류 — 지시문 없이 에이전트를 돌리지 않는다."""
    text = (DIR / name).read_text(encoding="utf-8")
    return SLOT.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), text)
