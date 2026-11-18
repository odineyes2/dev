"""Task 검사(DEV-238) — 계획서 `## 검사` 절에서 Task별 검사를 읽고, 맡길 때 사람이 고른 것과 언제나 하는 절차만 서버가 돌린다.

지킴이만 영구, 나머지는 임시다.
최소 검사 원칙: 서버는 검사를 고르거나 보태지 않는다. 돌리는 것은 (1) 계획서가 적고 사람이 맡길 때 남긴 명령,
(2) 언제나 하는 짧은 안전 절차(바뀐 파일 문법·diff 비밀값·DEV 마이그레이션 사본 적용)뿐이다.
검사는 PC 한 대에서 한 번에 하나씩 돈다(부하로 실패하지 않게).
"""
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath

import config
import db
import issues

SECTION = re.compile(r"^#{1,6}[ \t]*검사[ \t]*$", re.M)
LINE = re.compile(r"^\s*[-*]\s*(\d+)\s*(\(필수\))?\s*:\s*(.+?)\s*$")
CMD = re.compile(r"^`([^`]+)`\s*(?:[—–-]+\s*(.*))?$")
NONE = re.compile(r"^없음\s*(?:[—–-]+\s*(.*))?$")
SHELL = re.compile(r"[;&|<>$`\r\n]")
TIMEOUT_SEC = int(os.environ.get("DEV_CHECK_TIMEOUT_SEC") or 600)
SCRIPTS_DIR = config.DATA_DIR / "check-scripts"
TAIL = 2000
ACTOR = {"kind": "human", "name": "system/checks"}
ALWAYS = (
    ("syntax", "바꾼 파일 문법 확인", "바뀐 .py는 컴파일, .js는 node --check로 서버가 뜨지 않는 사고를 막아요."),
    ("secrets", "비밀값 검사", "추가된 줄과 새 파일에 키·토큰 모양이 있으면 막아요."),
    ("migration", "마이그레이션 사본 적용", "server/db.py가 바뀌면 운영 DB 사본에 마이그레이션을 적용해 깨지지 않는지 봐요."),
    ("build", "웹 빌드(병합 때)", "선언된 웹 빌드가 실패하면 병합하지 않아요."),
    ("health", "재시작 뒤 접속 확인(병합 때)", "재시작 뒤 응답이 없으면 병합을 되돌려요."),
)
_lock = threading.Lock()   # PC 전역 — 검사는 한 번에 하나만 돈다


class CheckFormatError(issues.StoreError):
    def __init__(self, message):
        super().__init__("계획서 `## 검사` 절: " + message, 400)


# ---- 계획서 형식 -------------------------------------------------------------

def _allowed(args: list[str]) -> bool:
    head = args[0] if args else ""
    if head == "python":
        return len(args) >= 2 and (args[1].endswith(".py") or args[1] == "-m" and len(args) >= 3)
    if head == "node":
        return len(args) >= 3 and args[1] in ("--check", "--test") or len(args) >= 2 and args[1].endswith((".js", ".mjs", ".cjs"))
    if head == "npm":
        rest = args[1:]
        if rest[:1] == ["--prefix"]:
            rest = rest[2:]
        return len(rest) >= 2 and rest[0] == "run"
    return False


def command_args(cmd: str) -> list[str]:
    """허용하는 명령 모양만 받아 인자 목록으로 바꾼다. 아니면 CheckFormatError."""
    if not cmd.strip() or SHELL.search(cmd):
        raise CheckFormatError(f"`{cmd}` — 셸 기호(; & | < > $ 백틱)는 쓸 수 없어요.")
    args = shlex.split(cmd, posix=True)
    for a in args[1:]:
        if not a.startswith("-") and (PurePosixPath(a.replace("\\", "/")).is_absolute() or ".." in a.replace("\\", "/").split("/") or re.match(r"^[A-Za-z]:", a)):
            raise CheckFormatError(f"`{cmd}` — 절대 경로나 `..`는 쓸 수 없어요.")
    if not _allowed(args):
        raise CheckFormatError(f"`{cmd}` — 명령은 `python 파일.py …`, `python -m 모듈 …`, `node --check|--test 파일`, `npm [--prefix 폴더] run 이름`만 쓸 수 있어요.")
    return args


def parse_section(body: str, strict=True) -> dict | None:
    """`## 검사` 절 → {Task 번호: {"none": 이유} 또는 {"items": [{cmd, why, required}]}}. 절이 없으면 None.
    strict가 아니면 형식이 틀린 줄을 건너뛴다(옛 계획서·승인 뒤 다시 읽기)."""
    m = SECTION.search(body or "")
    if not m:
        return None
    rest = body[m.end():]
    nxt = re.search(r"^#{1,6}[ \t]", rest, re.M)
    out = {}
    for line in (rest[:nxt.start()] if nxt else rest).splitlines():
        if not line.strip():
            continue
        mm = LINE.match(line)
        try:
            if not mm:
                raise CheckFormatError(f"`{line.strip()}` — `- 번호: \\`명령\\` — 이유` 또는 `- 번호: 없음 — 이유` 형식으로 적어 주세요.")
            n, required, rest_text = int(mm.group(1)), bool(mm.group(2)), mm.group(3)
            entry = out.setdefault(n, {"items": []})
            none = NONE.match(rest_text)
            if none:
                if entry["items"]:
                    raise CheckFormatError(f"Task {n}에 검사와 `없음`이 함께 있어요.")
                if not (none.group(1) or "").strip():
                    raise CheckFormatError(f"Task {n} `없음`에는 이유를 적어 주세요.")
                out[n] = {"none": none.group(1).strip(), "items": []}
                continue
            if "none" in entry:
                raise CheckFormatError(f"Task {n}에 검사와 `없음`이 함께 있어요.")
            cm = CMD.match(rest_text)
            if not cm:
                raise CheckFormatError(f"`{line.strip()}` — 명령은 백틱으로 감싸 주세요.")
            command_args(cm.group(1))
            if not (cm.group(2) or "").strip():
                raise CheckFormatError(f"`{cm.group(1)}` — 무엇을 증명하는지 한 줄 적어 주세요.")
            entry["items"].append({"cmd": cm.group(1).strip(), "why": cm.group(2).strip(), "required": required})
        except CheckFormatError:
            if strict:
                raise
    return out


def validate_plan(body: str) -> None:
    """post_plan 때 — `## Tasks`가 있으면 모든 Task 번호가 `## 검사`에 있어야 한다."""
    section = parse_section(body)
    tasks = issues.parse_tasks(body)
    import execute
    guard_section = re.search(r"^## 지킴이 변경[ \t]*$([\s\S]*?)(?=^#{1,6} |\Z)", body, re.M)
    guards = set()
    if guard_section:
        guards = {m.group(1) for m in re.finditer(
            r"^\s*-\s+`?(tests/guard/[^`:\s]+)`?\s*:\s*(?:추가|수정|삭제)\s*—\s*\S.*$",
            guard_section.group(1), re.M)}
    for task in tasks:
        for raw in re.split(r"[,、]", task["files"]):
            path = raw.strip(" `*").replace("\\", "/")
            while path.startswith("./"):
                path = path[2:]
            if path.startswith("tests/guard/"):
                if path not in guards:
                    raise CheckFormatError(f"`## 지킴이 변경`에 `{path}: 추가|수정|삭제 — 이유`를 적어 사람 승인을 받아 주세요")
            elif execute.is_check_file(path) or path == ".dev-checks" or path.startswith(".dev-checks/"):
                raise CheckFormatError("이슈별 검사는 파일 칸에 넣지 않는다: " + path)
    numbers = {t["n"] for t in tasks}
    if not tasks:
        return
    if section is None:
        raise CheckFormatError("절이 없어요. Task마다 `- 번호: \\`명령\\` — 무엇을 증명하나` 또는 `- 번호: 없음 — 이유`를 적어 주세요.")
    missing = sorted(numbers - set(section))
    if missing:
        raise CheckFormatError("Task " + ", ".join(map(str, missing)) + "의 검사가 없어요. 필요 없으면 `없음 — 이유`를 적어 주세요.")
    extra = sorted(set(section) - numbers)
    if extra:
        raise CheckFormatError("`## Tasks`에 없는 번호예요: " + ", ".join(map(str, extra)))


def body_text(entry: dict | None) -> str:
    """Task 본문에 붙이는 `**검사**` 부분. 계획서에 그 Task의 검사가 없으면 빈 문자열."""
    if not entry:
        return ""
    if "none" in entry:
        return f"\n\n**검사**: 없음 — {entry['none']}"
    lines = [f"- {'(필수) ' if i['required'] else ''}`{i['cmd']}` — {i['why']}" for i in entry["items"]]
    return "\n\n**검사**:\n" + "\n".join(lines) if lines else ""


def from_body(body: str) -> tuple[list[dict], str | None]:
    """Task 본문의 `**검사**` → ([{key, cmd, why, required}], 없음 이유)."""
    m = re.search(r"^\*\*검사\*\*:[ \t]*(.*)$", body or "", re.M)
    if not m:
        return [], None
    none = NONE.match(m.group(1).strip())
    if none:
        return [], (none.group(1) or "").strip()
    items = []
    for line in body[m.end():].splitlines():
        if not line.strip():
            if items:
                break
            continue
        mm = re.match(r"^\s*-\s*(\(필수\)\s*)?`([^`]+)`\s*(?:[—–-]+\s*(.*))?$", line)
        if not mm:
            break
        items.append({"key": f"p{len(items) + 1}", "cmd": mm.group(2).strip(), "why": (mm.group(3) or "").strip(), "required": bool(mm.group(1))})
    return items, None


def _planned_files(body: str) -> list[str]:
    m = re.search(r"^\*\*바꿀 파일\*\*:[ \t]*(.+)$", body or "", re.M)
    return [f.strip() for f in m.group(1).split(",")] if m else []


def items(issue: dict) -> dict:
    """맡기기 확인창 체크리스트 — 계획 검사(해제 가능, 필수는 비활성)와 언제나 절차(비활성)."""
    import orchestrate
    plan, none = from_body(issue.get("body", ""))
    key = issue["project_key"]
    cfg = orchestrate.settings(key)
    files = _planned_files(issue.get("body", ""))
    applies = {"syntax": True, "secrets": True, "migration": key == "DEV" and "server/db.py" in files,
               "build": bool(cfg.get("build")), "health": bool(cfg.get("pm2_app"))}
    always = [{"key": k, "label": label, "why": why, "required": True} for k, label, why in ALWAYS if applies[k]]
    return {"plan": [{**i, "label": i["cmd"]} for i in plan], "none": none, "always": always}


def selection(issue: dict, chosen) -> list[str]:
    """사람이 고른 계획 검사 키 → 저장할 키 목록(필수는 늘 포함). chosen이 None이면 전부(자동 위임·옛 화면)."""
    plan, _ = from_body(issue.get("body", ""))
    keys = [i["key"] for i in plan]
    if chosen is None:
        return keys
    if not isinstance(chosen, list) or not all(isinstance(k, str) for k in chosen) or set(chosen) - set(keys):
        raise issues.StoreError("고른 검사가 이 Task의 계획 검사와 맞지 않아요. 새로고침한 뒤 다시 맡겨 주세요.", 400)
    return [i["key"] for i in plan if i["required"] or i["key"] in chosen]


def selected(issue: dict, keys) -> list[dict]:
    """저장된 선택(None이면 전부)으로 돌릴 계획 검사."""
    plan, _ = from_body(issue.get("body", ""))
    return plan if keys is None else [i for i in plan if i["key"] in keys]


def prompt_text(issue: dict, keys) -> str:
    """실행 프롬프트 `{checks}` — 이번 실행에서 통과시킬 검사."""
    chosen = selected(issue, keys)
    _, none = from_body(issue.get("body", ""))
    if not chosen:
        return ("이번 실행에는 계획 검사가 없다" + (f"(계획서: {none})" if none else "") + ". 검사 명령을 새로 만들어 돌리지 않는다. "
                "서버는 언제나 하는 절차(바뀐 파일 문법·비밀값)만 확인한다.")
    lines = "\n".join(f"   - `{i['cmd']}` — {i['why']}" for i in chosen)
    return ("이번 실행에서 통과시킬 검사(사람이 맡길 때 고른 것)는 아래뿐이다. 구현을 마치면 이 명령을 돌려 통과시키고, 다른 검사로 넓히지 않는다.\n"
            + lines + "\n   검사 파일이 아직 없으면 `.dev-checks/`에 만들고 커밋하지 않는다. 서버가 보관해 같은 명령을 다시 돌린다.")


# ---- 실행 -------------------------------------------------------------------

def _env():
    import execute
    env = {k: v for k, v in os.environ.items() if k not in execute.SECRET_ENV}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", DEV_TEST_RUN="1")
    return env


def _kill_tree(proc):
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        proc.kill()


def run_command(cwd, cmd: str, timeout: int = TIMEOUT_SEC) -> dict:
    """명령 하나를 작업 폴더에서 돌린다. 출력은 파일로 받아 남은 자식이 파이프를 붙잡아도 멈추지 않는다."""
    args = command_args(cmd)
    exe = sys.executable if args[0] == "python" else shutil.which(args[0])
    if not exe:
        return {"cmd": cmd, "ok": False, "seconds": 0.0, "output": f"{args[0]}을(를) 찾지 못했어요."}
    started = time.monotonic()
    with tempfile.TemporaryFile() as sink:
        proc = subprocess.Popen([exe, *args[1:]], cwd=cwd, stdout=sink, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=_env(),
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            code = proc.wait(timeout=timeout)
            note = ""
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.wait(timeout=30)
            code, note = 124, f"\n시간 제한 {timeout}초를 넘겨 멈췄어요."
        sink.seek(0)
        output = sink.read().decode("utf-8", errors="replace") + note
    return {"cmd": cmd, "ok": code == 0, "seconds": round(time.monotonic() - started, 1), "output": output}


def changed(cwd) -> tuple[list[str], str]:
    """Task가 바꾼 파일(커밋·커밋 전·새 파일)과 비교 기준(base와의 공통 조상)."""
    import execute
    base = execute._git(cwd, "merge-base", execute.BASE_BRANCH, "HEAD")
    names = execute._git(cwd, "diff", "--name-only", base).splitlines()
    names += execute._git(cwd, "ls-files", "--others", "--exclude-standard").splitlines()
    return [n for n in dict.fromkeys(names) if n and (Path(cwd) / n).is_file()], base


def check_syntax(cwd, files) -> dict:
    started, bad = time.monotonic(), []
    node = shutil.which("node")
    for name in files:
        path = Path(cwd) / name
        if name.endswith(".py"):
            try:
                compile(path.read_text(encoding="utf-8"), name, "exec")
            except (SyntaxError, UnicodeDecodeError, ValueError) as e:
                bad.append(f"{name}: {e}")
        elif name.endswith((".js", ".mjs", ".cjs")) and node:
            r = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace",
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if r.returncode:
                bad.append(f"{name}: {(r.stderr or r.stdout).strip()[-600:]}")
    count = sum(1 for n in files if n.endswith((".py", ".js", ".mjs", ".cjs")))
    return {"cmd": "바꾼 파일 문법 확인", "ok": not bad, "seconds": round(time.monotonic() - started, 1),
            "output": "\n".join(bad), "detail": f"{count}개 파일"}


SECRET_HITS_MAX = 30
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _secret_shape(text: str) -> str:
    """repo_snapshot.SECRET에 걸린 문자열의 갈래 이름만 돌려준다. 값은 돌려주지 않는다(DEV-241)."""
    low = text.lower()
    if text.startswith("-----BEGIN"):
        return "개인 키 블록"
    for prefix in ("sk-", "github_pat_"):
        if low.startswith(prefix):
            return f"{prefix} 키 모양"
    if re.match(r"gh[pousr]_", low):
        return "gh*_ 키 모양"
    if low.startswith("akia"):
        return "AKIA 키 모양"
    if re.match(r"bearer\s", low):
        return "Bearer 토큰 모양"
    if low.startswith("eyj"):
        return "JWT 모양"
    if re.match(r"[a-z][a-z0-9+.-]*://", low):
        return "URL 속 계정:암호 모양"
    # 이름=값 갈래는 고정 키워드로 시작하므로 문자·_·-까지만 읽으면 값이 섞이지 않는다
    return "이름=값 모양: " + re.match(r"[A-Za-z_-]*", text).group(0).lower()


def check_secrets(cwd, files, base) -> dict:
    """추가된 줄과 새 파일에서 키·토큰 모양을 찾는다. 값은 출력하지 않고 `파일:줄 (모양)`만 남긴다."""
    import execute
    import repo_snapshot
    started, hits = time.monotonic(), {}

    def scan(name, number, text):
        shapes = [_secret_shape(m.group(0)) for m in repo_snapshot.SECRET.finditer(text)]
        if shapes:
            hits.setdefault(f"{name}:{number}", list(dict.fromkeys(shapes)))

    tracked = set(execute._git(cwd, "ls-files").splitlines())
    current, number, left = None, 0, 0
    for line in execute._git(cwd, "diff", "-U0", base).splitlines():
        if left > 0 and line.startswith("+"):   # 추가 줄 수를 세어 `++ `로 시작하는 내용을 머리로 오인하지 않는다
            if current:
                scan(current, number, line[1:])
            number, left = number + 1, left - 1
        elif line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else None
        elif m := _HUNK.match(line):
            number, left = int(m.group(1)), int(m.group(2) or 1)
    for name in files:
        if name not in tracked:
            try:
                text = (Path(cwd) / name).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                scan(name, i, line)
    rows = [f"{where} ({', '.join(shapes)})" for where, shapes in hits.items()]
    if len(rows) > SECRET_HITS_MAX:
        rows = rows[:SECRET_HITS_MAX] + [f"… 외 {len(rows) - SECRET_HITS_MAX}곳"]
    return {"cmd": "비밀값 검사", "ok": not hits, "seconds": round(time.monotonic() - started, 1),
            "output": "\n".join(["키·토큰 모양이 있는 곳(값은 표시하지 않아요):", *rows]) if hits else ""}


def check_migration(cwd) -> dict:
    """운영 DB 사본에 이 worktree의 db.init()을 적용하고 무결성을 본다(DEV 전용)."""
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "dev.db"
        if config.DB_PATH.is_file():
            src = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
            dst = sqlite3.connect(copy)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
        code = ("import sqlite3, config, db\nv = db.init()\nc = sqlite3.connect(config.DB_PATH)\n"
                "r = c.execute('PRAGMA integrity_check').fetchone()[0]\nprint('user_version', v, 'integrity', r)\n"
                "raise SystemExit(0 if r == 'ok' else 1)\n")
        env = {**_env(), "DEV_DB_PATH": str(copy), "DEV_DATA_DIR": tmp}
        r = subprocess.run([sys.executable, "-c", code], cwd=Path(cwd) / "server", capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=300, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    return {"cmd": "마이그레이션 사본 적용", "ok": r.returncode == 0, "seconds": round(time.monotonic() - started, 1),
            "output": (r.stdout + r.stderr).strip()}


def _report(results) -> tuple[str, bool]:
    ok = all(r["ok"] for r in results)
    lines = [f"🧪 검사 결과 — {'통과' if ok else '실패'}"]
    for r in results:
        label = r["cmd"] if r.get("always") else f"`{r['cmd']}`"
        lines.append(f"- {'✅' if r['ok'] else '❌'} {label}" + (f" · {r['detail']}" if r.get("detail") else "") + f" · {r['seconds']}초")
    for r in results:
        if not r["ok"]:
            lines.append(f"\n**{r['cmd']}** 출력(끝 {TAIL}자):\n```\n{r['output'][-TAIL:]}\n```")
    return "\n".join(lines), ok


def run_all(ref: str, cwd, plan_items: list[dict]) -> tuple[str, bool]:
    """언제나 절차 + 고른 계획 검사를 PC 전역 잠금 아래 차례로 돌려 (보고, 통과 여부)를 돌려준다."""
    with _lock:
        files, base = changed(cwd)
        results = [{**check_syntax(cwd, files), "always": True}, {**check_secrets(cwd, files, base), "always": True}]
        if ref.startswith("DEV-") and "server/db.py" in files:
            results.append({**check_migration(cwd), "always": True})
        for item in plan_items:
            results.append(run_command(cwd, item["cmd"]))
    return _report(results)


def run_keys(run_id: int):
    with db.connect() as c:
        row = c.execute("SELECT checks_json FROM runs WHERE id=?", (run_id,)).fetchone()
    return json.loads(row["checks_json"]) if row and row["checks_json"] else None


def _script_files(root):
    """링크를 따라 외부 파일을 보관하거나 복원하지 않는다."""
    if root.is_symlink() or not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_file() and not any(p.is_symlink() for p in (path, *path.parents)):
            yield path, relative


def stash_scripts(ref: str, run_id: int, cwd) -> list[tuple[str, str, int]]:
    """임시 검사 전체와 내용 식별자를 실행별로 보관한다."""
    target = SCRIPTS_DIR / ref / f"run-{run_id}"
    rows = []
    for path, relative in _script_files(Path(cwd) / ".dev-checks"):
        data = path.read_bytes()
        dest = target / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        rows.append((relative.as_posix(), hashlib.sha256(data).hexdigest()[:12], len(data)))
    return rows


def restore_scripts(ref: str, dest) -> None:
    """마지막 보관본을 복원하되 현재 작업 파일은 보존한다."""
    root = SCRIPTS_DIR / ref
    runs = [p for p in root.glob("run-*") if p.name[4:].isdigit() and p.is_dir() and not p.is_symlink()]
    if not runs:
        return
    source = max(runs, key=lambda p: int(p.name[4:]))
    for path, relative in _script_files(source):
        target = Path(dest) / ".dev-checks" / relative
        if target.exists() or any(p.is_symlink() for p in (target, *target.parents)):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


def verify(ref: str, cwd, run_id: int) -> str | None:
    """실행이 끝난 worktree에서 검사를 돌린다. 통과하면 None, 실패하면 재작업에 붙일 보고."""
    issue = issues.get_issue(ref)
    restore_scripts(issue["ref"], cwd)
    report, ok = run_all(issue["ref"], cwd, selected(issue, run_keys(run_id)))
    stored = stash_scripts(issue["ref"], run_id, cwd)
    if stored:
        report += "\n\n🗂 임시 검사 보관: " + " · ".join(f"{name} · {sha} · {size}바이트" for name, sha, size in stored)
    issues.add_comment(ACTOR, issue["ref"], report)
    return None if ok else report


def merge_recheck(repo, worktree, ref: str, before: str, task_sha: str) -> str | None:
    """시험 병합 사본에서 — 이 Task와 base가 공통 조상 이후 같은 파일을 바꿨을 때만 고른 계획 검사를 다시 돌린다."""
    import execute
    base = execute._git(repo, "merge-base", before, task_sha)
    if not base:
        return None
    task_files = set(execute._git(repo, "diff", "--name-only", base, task_sha).splitlines())
    base_files = set(execute._git(repo, "diff", "--name-only", base, before).splitlines())
    overlap = sorted(task_files & base_files)
    if not overlap:
        return None
    issue = issues.get_issue(ref)
    with db.connect() as c:
        row = c.execute("SELECT checks_json FROM runs WHERE issue_id=? AND mode='execute' AND checks_json IS NOT NULL ORDER BY id DESC LIMIT 1",
                        (issue["id"],)).fetchone()
    plan = selected(issue, json.loads(row["checks_json"]) if row else None)
    if not plan:
        return None
    with _lock:
        restore_scripts(ref, worktree)
        results = [run_command(worktree, item["cmd"]) for item in plan]
    report, ok = _report(results)
    issues.add_comment(ACTOR, issue["ref"], "🔀 병합 전 다시 검사 — 다른 병합이 같은 파일(" + ", ".join(f"`{f}`" for f in overlap[:10])
                       + ")을 바꿨어요.\n\n" + report)
    return None if ok else report
