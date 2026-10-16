"""Harness 조회(DEV-132) — 프로젝트 × 에이전트 × 단계별로 에이전트가 실제로 받는 글·규칙 파일·도구를 층별로 모아 보여준다.

기존 prompt_for/command_for가 만든 것을 그대로 쓴다(복제 없음). 규칙 파일은 고정 허용 이름만 읽어 .env·.mcp.json·auth.json 등은 목록에 나올 수 없고,
명령 인자의 MCP 설정 경로·주입 값은 가린다. 조회 전용이다.
"""
import json
import re
from pathlib import Path

import config
import execute
import filemap
import harness
import issues
import review

PROVIDERS = ("claude", "codex")
STAGES = ("review", "execute")
MAX_FILE = 256 * 1024
SECTION_END = "\n\n---\n\n"
HIDDEN = "(비공개)"
# 2층 허용 목록 — 디렉터리를 훑지 않는다
PROJECT_FILES = ("AGENTS.md", "CLAUDE.md")
PROJECT_DOCS = ("docs/project/00_*.md", "docs/project/01_*.md", "docs/project/02_*.md",
                "docs/project/03_*.md", "docs/project/04_*.md", "docs/project/05_*.md")


def _read(path: Path, root: Path | None = None) -> dict:
    """허용 목록 안의 파일 하나 — 없거나 심볼릭 링크·저장소 밖·너무 크면 content 없이 상태만."""
    out = {"exists": False, "content": ""}
    try:
        if path.is_symlink() or not path.is_file():
            return out
        if root is not None and not path.resolve().is_relative_to(root.resolve()):
            return out
        if path.stat().st_size > MAX_FILE:
            return {"exists": True, "content": "", "note": "256KB가 넘어 보여주지 않아요."}
        return {"exists": True, "content": path.read_text(encoding="utf-8", errors="replace")}
    except OSError:
        return out


def _file(layer: int, label: str, source: str, path: Path, root: Path | None = None, editable: bool = True) -> dict:
    return {"layer": layer, "label": label, "source": source, "editable": editable, **_read(path, root)}


def _flag_values(cmd: list[str], flag: str) -> list[str]:
    """`--flag a b c` 뒤 값들 — 다음 `--` 옵션 전까지."""
    if flag not in cmd:
        return []
    out = []
    for a in cmd[cmd.index(flag) + 1:]:
        if a.startswith("--"):
            break
        out.append(a)
    return out


def split_command(cmd: list[str], provider: str) -> tuple[str, list[str]]:
    """명령 → (프롬프트, 프롬프트 뺀 인자). Claude는 cmd[2], Codex는 마지막 인자(with_model·--output-schema 뒤 포함)."""
    i = 2 if provider == "claude" else len(cmd) - 1
    return cmd[i], cmd[:i] + cmd[i + 1:]


def public_args(args: list[str]) -> list[str]:
    """MCP 설정 경로(--mcp-config)·Codex MCP 주입 값(-c mcp_servers=…)과 실행 파일 경로는 싣지 않는다."""
    out, skip = [], False
    for n, a in enumerate(args):
        if skip:
            skip = False
        elif a == "--mcp-config":
            skip = True
        elif a == "-c" and n + 1 < len(args) and args[n + 1].startswith("mcp_servers="):
            skip = True
        elif n >= 1 or a.startswith("-"):   # 실행 파일 경로(첫 인자)는 뺀다
            out.append(a)
    return out


def codex_tools(cmd: list[str]) -> list[str]:
    for a in cmd:
        m = re.search(r"enabled_tools=(\[[^\]]*\])", a) if a.startswith("mcp_servers=") else None
        if m:
            return [f"mcp__dev__{t}" for t in json.loads(m.group(1))]
    return []


def _scrub(text: str) -> str:
    for secret in (str(review.MCP_CONFIG), str(review.MCP_CONFIG).replace("\\", "/")):
        text = text.replace(secret, HIDDEN)
    return text


def _segments(prompt: str, map_text: str) -> list[dict]:
    """프롬프트를 층으로 나눈다 — 공통 하네스(1층) · 파일 지도(자동 생성) · 나머지(dev 프롬프트, 3층)."""
    segs, rest = [], prompt
    head = harness.prompt_section()
    if head and rest.startswith(head):
        segs.append({"layer": 1, "label": "공통 하네스", "source": "harness/COMMON.md", "text": head})
        rest = rest[len(head):]
    if rest.startswith("## 파일 지도"):
        n = rest.find(SECTION_END)
        n = len(rest) if n < 0 else n + len(SECTION_END)
        segs.append({"layer": "map", "label": "파일 지도(자동 생성)", "source": "server/filemap.py", "text": rest[:n]})
        rest = rest[n:]
    segs.append({"layer": 3, "label": "dev 프롬프트", "source": "server/review.py · server/execute.py의 prompt_for", "text": rest})
    return segs


def _command(key: str, provider: str, stage: str) -> list[str]:
    ref = f"{key}-0"   # 없는 이슈 — 조회 실패 경로의 일반 프롬프트로 떨어진다. {ref}는 이 값으로 치환된 모습
    if stage == "review":
        return review.command_for(ref) if provider == "claude" else review.codex_command_for(ref)
    if provider == "claude":
        return execute.command_for(ref, ref, "mcp.json")
    return execute.codex_command_for(ref, ref)


def enforcement(provider: str, stage: str, args: list[str], cmd: list[str]) -> dict:
    """4층 — 코드가 강제하는 도구·도메인·예산·병합 규칙."""
    if provider == "claude":
        allowed, blocked = _flag_values(cmd, "--allowedTools"), _flag_values(cmd, "--disallowedTools")
        tools = _flag_values(cmd, "--tools")
    else:
        allowed, blocked, tools = codex_tools(cmd), [], []
    items = {
        "allowed_tools": allowed, "blocked_tools": blocked, "builtin_tools": tools,
        "web_domains": list(review.WEB_DOMAINS) if provider == "claude" and stage == "review" else [],
        "args": public_args(args),
        "limits": ({"timeout_sec": review.TIMEOUT_SEC} if stage == "review"
                   else {"budget_usd": execute.BUDGET_USD, "timeout_sec": execute.TIMEOUT_SEC, "base_branch": execute.BASE_BRANCH}),
        "source": "server/review.py · server/execute.py",
    }
    if stage == "execute":
        items["secret_env_removed"] = list(execute.SECRET_ENV)
        items["merge_rules"] = ["push 불가(pushurl 덮기·토큰 제거)",
                                "서버가 최신 base와 Task를 임시 worktree에서 시험 병합하고 tests/test_*.py 전체 검사를 통과해야 한다",
                                "검사한 커밋만 --ff-only로 반영, 충돌·검사 실패는 changes_requested"]
    return items


def build(key: str, provider: str, stage: str) -> dict:
    if provider not in PROVIDERS or stage not in STAGES:
        raise issues.StoreError("provider는 claude|codex, stage는 review|execute예요.", 400)
    project = next((p for p in issues.list_projects() if p["key"] == key), None)
    if not project:
        raise issues.StoreError("프로젝트를 찾을 수 없어요.", 404)
    root = Path(project["local_path"]) if project["local_path"] else None
    repo = root if root and root.is_dir() else None
    cmd = _command(key, provider, stage)
    prompt, args = split_command(cmd, provider)
    map_text = ""
    if repo:
        try:
            body = filemap.build(repo)
        except Exception:   # 지도는 보조 자료 — 못 만들면 없이 보여준다
            body = ""
        if body:
            map_text = ("## 파일 지도 — dev가 지금 저장소에서 자동으로 만든 것(추적 파일과 각 파일 첫머리 설명)\n"
                        "이 지도로 바로 해당 파일을 연다. 여기 없는 것만 Grep/Glob로 찾는다.\n" + body + SECTION_END)
    head = harness.prompt_section()
    if map_text and not prompt[len(head):].startswith("## 파일 지도"):   # 없는 이슈라 프롬프트가 지도를 못 만들었다 — 같은 글을 하네스 다음에 끼운다
        prompt = head + map_text + prompt[len(head):] if prompt.startswith(head) else map_text + prompt
    prompt = _scrub(prompt)
    layer2 = []
    if repo:
        for name in PROJECT_FILES:
            layer2.append(_file(2, name, f"{key} 저장소 {name}", repo / name, repo))
        for pattern in PROJECT_DOCS:
            for p in sorted(repo.glob(pattern)):
                layer2.append(_file(2, p.relative_to(repo).as_posix(), f"{key} 저장소 {p.relative_to(repo).as_posix()}", p, repo))
    layer2.append(_file(2, "docs/DESIGN.md (공통 디자인, dev 저장소)", "dev 저장소 docs/DESIGN.md", execute.DESIGN_DOC, editable=True))
    return {
        "project": key, "provider": provider, "stage": stage, "preview_ref": f"{key}-0",
        "note": "{ref} 등은 미리보기용 없는 이슈 번호로 채워져 있어요. 1층 저장은 다음 실행부터, MCP 안내문은 재시작 뒤에 반영돼요.",
        "layers": {
            "common": _file(1, "공통 하네스", "harness/COMMON.md", harness.PATH, config.REPO_ROOT),
            "project": layer2,
            "file_map": {"label": "파일 지도(자동 생성)", "source": "server/filemap.py(DEV-134)", "editable": False, "content": map_text.removesuffix(SECTION_END)},
            "prompt": {"layer": 3, "editable": False, "source": "server/review.py · server/execute.py", "text": prompt,
                       "segments": _segments(prompt, map_text)},
            "enforcement": {"layer": 4, "editable": False, **enforcement(provider, stage, args, cmd)},
        },
    }
