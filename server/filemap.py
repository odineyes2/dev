"""파일 지도(DEV-134) — 저장소의 추적 파일과 각 파일 첫머리 설명 한 줄을 폴더별로 묶는다.

검토·실행 프롬프트에 공통 하네스 다음으로 붙어, 에이전트가 저장소를 처음부터 뒤지지 않고 바로 해당 파일을 열게 한다.
실행 시점의 저장소에서 매번 만들어 따로 갱신할 일이 없다. 설명은 파일 첫머리 주석·문서 문자열·마크다운 첫 제목에서 온다.
"""
import re
import subprocess
from collections import defaultdict
from pathlib import Path

import issues

CODE = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".css", ".html", ".sh", ".ps1", ".sql", ".rs", ".go", ".md"}
ROOT_CONFIG = {".json", ".toml", ".yml", ".yaml"}   # 설정 파일은 최상위 것만 — 하위의 json은 대개 데이터다
MAX_CHARS = 12000   # 지도는 매 호출 문맥에 실린다. 넘으면 뒤쪽 폴더를 파일 수로만 줄인다
COMMENT = re.compile(r"^\s*(?:#|//|/\*+|\*|<!--|--|\"\"\"|''')\s*(.*?)\s*(?:\*/|-->|\"\"\"|''')?\s*$")
SKIP = re.compile(r"^(?:#!|-\*-|coding[:=]|eslint|@ts-|prettier|noqa|type: ignore|use strict|<!doctype|<\?xml)", re.I)


def describe(path: Path) -> str:
    """첫머리 설명 한 줄(없으면 빈 글). 문서 문자열이 다음 줄에서 시작하는 경우도 본다."""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            lines = [next(f, "") for _ in range(30)]
    except OSError:
        return ""
    if path.suffix == ".md":
        return next((l.lstrip("#").strip() for l in lines if l.startswith("#")), "")[:90]
    opened = False
    for line in lines:
        text = line.strip()
        if not text or SKIP.match(text) or SKIP.match(text.lstrip("#/*<!- \"'")):
            continue
        m = COMMENT.match(line)
        if m:
            body = m.group(1).strip("\"' ")
            if body and not SKIP.match(body):
                return body[:90]
            opened = opened or text.startswith(('"""', "'''", "/*"))   # 여는 따옴표·주석만 있는 줄 — 다음 줄이 설명
            continue
        return text.strip("\"' ")[:90] if opened else ""
    return ""


def entries(repo) -> tuple[list[dict], list[dict]] | None:
    """저장소 → (지도에 들어가는 파일 {dir, name, desc} 목록, 크기 상한으로 뺀 폴더 {dir, files}). git 저장소가 아니면 None.
    프롬프트 글(build)과 Harness 화면 표가 같이 쓴다."""
    r = subprocess.run(["git", "-c", "core.quotepath=false", "ls-files", "-z"], cwd=repo, capture_output=True, timeout=20)
    if r.returncode:
        return None
    dirs = defaultdict(list)
    for name in r.stdout.decode("utf-8", "replace").split("\0"):
        p = Path(name)
        if name and (p.suffix in CODE or (p.suffix in ROOT_CONFIG and len(p.parts) == 1)):
            dirs[p.parent.as_posix()].append(p)
    rows, dropped, size = [], [], 0
    for d in sorted(dirs, key=lambda d: (d != ".", d)):
        block = [{"dir": d, "name": p.name, "desc": describe(Path(repo) / p)} for p in sorted(dirs[d])]
        length = len(_block_text(d, block))
        if size + length > MAX_CHARS:
            dropped.append({"dir": d, "files": len(block)})
            continue
        rows += block
        size += length
    return rows, dropped


def _block_text(d: str, block: list[dict]) -> str:
    return "\n".join([f"{'/' if d == '.' else d + '/'}"] + [f"  {e['name']}" + (f" — {e['desc']}" if e["desc"] else "") for e in block])


def build(repo) -> str:
    """저장소 → 파일 지도 글(프롬프트용). git 저장소가 아니면 빈 글."""
    got = entries(repo)
    return text(*got) if got is not None else ""


def text(rows: list[dict], dropped: list[dict]) -> str:
    """entries()의 결과 → 프롬프트에 붙는 지도 글."""
    by_dir = defaultdict(list)
    for e in rows:
        by_dir[e["dir"]].append(e)
    out = [_block_text(d, block) for d, block in by_dir.items()]
    if dropped:
        out.append("생략한 폴더(파일 수): " + ", ".join(f"{x['dir']}/({x['files']})" for x in dropped))
    return "\n".join(out)


def section(ref: str, execution: bool = False) -> str:
    """프롬프트에 붙일 지도 — 실행은 Task worktree, 검토는 프로젝트 저장소. 못 만들면 빈 글(지도 없이 진행)."""
    try:
        key = issues.get_issue(ref)["project_key"]
        repo = next((p["local_path"] for p in issues.list_projects() if p["key"] == key), "")
        if execution:
            import execute
            wt = execute.worktree_path(ref)
            repo = str(wt) if wt.is_dir() else repo
        text = build(repo) if repo and Path(repo).is_dir() else ""
    except Exception:   # 지도는 보조 자료라 실패해도 검토·실행을 막지 않는다
        return ""
    if not text:
        return ""
    return ("## 파일 지도 — dev가 지금 저장소에서 자동으로 만든 것(추적 파일과 각 파일 첫머리 설명)\n"
            "이 지도로 바로 해당 파일을 연다. 여기 없는 것만 Grep/Glob로 찾는다.\n" + text + "\n\n---\n\n")
