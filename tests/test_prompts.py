"""코드 속 프롬프트 파일(DEV-135) — 모든 파일이 자리표시 규칙을 지키고, 채우기·검증이 동작한다."""
import os, sys, tempfile
from pathlib import Path

os.environ["DEV_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import prompts, review, execute  # noqa: E402

assert sorted(p.name for p in prompts.DIR.glob("*.md")) == sorted(prompts.SLOTS), "파일과 SLOTS 목록이 어긋났다"
for name in prompts.SLOTS:
    text = (prompts.DIR / name).read_text(encoding="utf-8")
    assert prompts.problems(name, text) == [], (name, prompts.problems(name, text))

assert prompts.render("review_claude.md", ref="X-1", web_domains="a.com").count("X-1") >= 2
assert "{ref}" not in prompts.render("execute_codex.md", ref="X-1-1", branch="b", parent_ref="X-1", design_doc="d", api_test="t", test_scope="s")
assert prompts.problems("review_claude.md", "{ref} {oops}") and "oops" in prompts.problems("review_claude.md", "{ref} {oops}")[0]
assert prompts.problems("execute_claude.md", "{ref} 검사 범위 없음") == ["필수 자리표시 {test_scope}가 빠졌어요"]
assert prompts.problems("nope.md", "") and prompts.problems("capture.md", "자리표시 없음") == []

# Codex 검토는 공급자별 파일을 쓴다 — 예전 "Claude→Codex" 문자열 치환 없이도 Claude 문구가 남지 않는다
codex = review.codex_command_for("DEV-1")[-1]
assert "Claude 검토 완료" not in codex and "Codex 검토 완료" in codex and "읽기 전용 명령으로" in codex
assert "Claude 검토 완료" in review.prompt_for("DEV-1")
print("OK prompts")
