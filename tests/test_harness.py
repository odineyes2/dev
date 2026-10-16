"""공통 하네스(DEV-131) — 검토·실행 프롬프트와 MCP 안내문에 같은 규칙이 들어가고, Claude 실행은 사용자 개인 설정을 받지 않으며,
운영 worktree는 dev 저장소 밖에 생긴다."""
import json, os, subprocess, sys, tempfile
from pathlib import Path

os.environ["DEV_DATA_DIR"] = tempfile.mkdtemp()
SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))
import config, execute, harness, mcp_tools, review  # noqa: E402

common = harness.text()
assert common.startswith("# 공통 하네스") and "남의 변경" in common
assert common in mcp_tools.INSTRUCTIONS and "작업 흐름:" in mcp_tools.INSTRUCTIONS
assert "Claude" not in common   # Codex 검토 프롬프트는 "Claude"를 "Codex"로 바꾼다 — 공통 문서가 깨지지 않게

for prompt in (review.prompt_for("DEV-1"), execute.prompt_for("DEV-1-1", "DEV-1")):
    assert prompt.startswith(common), prompt[:80]
codex_review = review.codex_command_for("DEV-1")[-1]
codex_exec = execute.codex_command_for("DEV-1-1", "DEV-1")[-1]
assert codex_review.startswith(common) and codex_exec.startswith(common)
assert "AGENTS.md(없으면 CLAUDE.md)" in codex_review and "AGENTS.md(없으면 CLAUDE.md)" in execute.prompt_for("DEV-1-1", "DEV-1")

cmd = execute.command_for("DEV-1-1", "DEV-1", "mcp.json")
assert cmd[cmd.index("--setting-sources") + 1] == "project" and "--disable-slash-commands" in cmd
excludes = json.loads(cmd[cmd.index("--settings") + 1])["claudeMdExcludes"]
assert excludes == [(Path.home() / ".claude" / "CLAUDE.md").as_posix()]

# 검사의 임시 데이터 폴더는 그 안에, 운영 기본값(저장소 안 data/)이면 저장소 옆 .dev-worktrees
assert execute.WORKTREE_DIR == Path(os.environ["DEV_DATA_DIR"]).resolve() / "worktrees"
env = {k: v for k, v in os.environ.items() if k != "DEV_DATA_DIR"}
out = subprocess.run([sys.executable, "-c", "import execute; print(execute.WORKTREE_DIR)"], cwd=SERVER, env=env,
                     capture_output=True, text=True, encoding="utf-8").stdout.strip()
assert Path(out) == config.REPO_ROOT.parent.resolve() / ".dev-worktrees", out
assert not Path(out).is_relative_to(config.REPO_ROOT.resolve())
print("OK harness")
