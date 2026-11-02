"""코드 속 프롬프트(DEV-135) — 검토·실행 에이전트에게 주는 작업 지시문은 harness/prompts/*.md에 있고, 여기서 자리표시를 채운다.

글(무엇이라고 말할지)은 파일에, 언제 무엇을 붙일지(조건·DB 조회)는 review·execute 코드에 둔다. 자리표시는 `{이름}`이고 코드가 채운다.
파일은 실행마다 읽어 고치면 다음 검토·실행부터 반영된다.
"""
import re

import config

DIR = config.REPO_ROOT / "harness" / "prompts"
SLOT = re.compile(r"\{([a-z_]+)\}")


def render(name: str, **values) -> str:
    """파일을 읽어 자리표시를 채운다. 파일이 없으면 오류 — 지시문 없이 에이전트를 돌리지 않는다."""
    text = (DIR / name).read_text(encoding="utf-8")
    return SLOT.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), text)
