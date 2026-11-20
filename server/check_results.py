"""서버 검사 결과의 입력 계약·원자적 보관·무결성과 재사용 가능성을 판정한다."""
import hashlib
import hmac
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

import config

CONTRACT_VERSION = 1
RESULTS_DIR = config.DATA_DIR / "check-results"
TTL_SECONDS = 7 * 86400
GENERATED = {".git", "__pycache__", ".pytest_cache", ".ui-captures", "check-results"}
ARTIFACT_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".pdf"}


class EnvironmentUnknown(ValueError):
    """환경을 확정하지 못하면 명령은 실행하되 결과는 재사용하지 않는다."""


def _link(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def safe_pattern(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("저장소 상대 경로만 쓸 수 있어요")
    parts = PurePosixPath(value).parts
    if value.startswith("/") or ".." in parts or any(p == ".env" or p.startswith(".env.") and p != ".env.example" for p in parts):
        raise ValueError("경로 탈출·비밀 파일 입력은 허용하지 않아요")
    return value


def validate_contract(meta):
    if not isinstance(meta, dict) or set(meta) != {"cmd", "phase", "inputs", "conditions", "reuse"}:
        raise ValueError("cmd, phase, inputs, conditions, reuse를 모두 적어 주세요")
    if not isinstance(meta["cmd"], str) or meta["phase"] not in ("static", "functional", "browser"):
        raise ValueError("명령과 검사 단계가 올바르지 않아요")
    if not isinstance(meta["inputs"], list) or not meta["inputs"] or not all(isinstance(x, str) for x in meta["inputs"]) or len(set(meta["inputs"])) != len(meta["inputs"]):
        raise ValueError("중복 없는 입력 목록이 필요해요")
    for name in meta["inputs"]:
        safe_pattern(name)
    if not isinstance(meta["conditions"], dict) or type(meta["reuse"]) is not bool:
        raise ValueError("conditions 객체와 reuse 불리언이 필요해요")
    # 조건에는 고정된 비밀 아닌 설명만 허용한다. 환경값은 기록하지 않는다.
    import repo_snapshot
    if repo_snapshot.SECRET.search(json.dumps(meta["conditions"])):
        raise ValueError("검사 조건에 비밀값을 넣을 수 없어요")


def _safe_file(root, path):
    relative = path.relative_to(root)
    safe_pattern(relative.as_posix())
    if any(_link(p) for p in (path, *path.parents)):
        raise ValueError("링크 입력은 허용하지 않아요")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("입력이 저장소 밖에 있어요")
    return relative.as_posix()


def _files(root):
    # os.walk는 링크를 따라가지 않는다. 비밀 파일은 열지 않는다.
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in GENERATED and d not in {"data", "node_modules", ".venv", "venv"})
        if any(_link(Path(directory) / d) for d in dirs):
            raise ValueError("링크 입력은 허용하지 않아요")
        for name in sorted(names):
            if name == ".env" or name.startswith(".env.") and name != ".env.example":
                continue
            yield Path(directory) / name


def environment(root):
    """실제 런타임과 의존성을 식별한다. 환경값은 해시로만 남긴다."""
    import task_checks
    versions = sorted((d.metadata.get("Name", ""), d.version) for d in importlib.metadata.distributions())
    runtime = {"python": sys.version, "executable": sys.executable, "os": platform.platform(), "python_packages": versions,
               "env_hash": digest(task_checks._env())}
    for name in ("node", "npm"):
        exe = shutil.which(name)
        if exe:
            r = subprocess.run([exe, "--version"], capture_output=True, timeout=20,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if r.returncode:
                raise ValueError("런타임 버전을 확인하지 못했어요")
            runtime[name] = hashlib.sha256(r.stdout).hexdigest()
    # 설치된 Node 의존성·브라우저 바이너리까지 실제 바이트를 포함한다.
    installed = {}
    candidates = [root / "node_modules", root / ".venv", root / "venv"]
    candidates += [Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".cache"))) / "ms-playwright",
                   Path.home() / ".cache" / "ms-playwright"]
    candidates += list(root.glob("*/node_modules"))
    for index, folder in enumerate(candidates):
        if not folder.exists():
            continue
        if _link(folder):
            raise ValueError("설치 환경 링크를 확정할 수 없어요")
        for directory, dirs, names in os.walk(folder, followlinks=False):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            if any(_link(Path(directory) / d) for d in dirs):
                raise ValueError("설치 환경 링크를 확정할 수 없어요")
            for name in names:
                path = Path(directory) / name
                if _link(path):
                    raise ValueError("설치 환경 링크를 확정할 수 없어요")
                if name == ".env" or name.startswith(".env."):
                    continue
                installed[f"{index}/{path.relative_to(folder).as_posix()}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    runtime["installed"] = digest(installed)
    python_files = {}
    for distribution in importlib.metadata.distributions():
        for entry in distribution.files or []:
            if Path(str(entry)).name != ".env" and not Path(str(entry)).name.startswith(".env.") and not str(entry).endswith(".pyc"):
                path = Path(distribution.locate_file(entry))
                python_files[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    runtime["python_files"] = digest(python_files)
    return digest(runtime)


def snapshot(cwd, item, context=None):
    """승인된 입력과 스크립트·설정·의존성의 추가·삭제·바이트를 식별한다."""
    root = Path(cwd).resolve()
    meta = item.get("contract")
    if meta:
        validate_contract(meta)
    patterns = meta["inputs"] if meta else []
    selected = set()
    for pattern in patterns:
        for path in root.glob(pattern):
            if _link(path):
                raise ValueError("링크 입력은 허용하지 않아요")
            if path.is_file():
                _safe_file(root, path)
                selected.add(path)
            elif path.is_dir():
                selected.update(_files(path))
    manifest = {}
    for path in set(_files(root)) | selected:
        relative = path.relative_to(root).as_posix()
        # 명시한 코드 범위 밖이라도 실행 스크립트·설정·lockfile은 늘 포함한다.
        always = (path.suffix in {".py", ".js", ".mjs", ".cjs", ".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".lock"}
                  or path.name.startswith(("requirements", "Dockerfile")) or relative.startswith(".dev-checks/"))
        # 다른 단계 전용 Python fixture는 입력으로 명시한 경우에만 포함한다.
        script = relative in __import__("shlex").split(item["cmd"])[1:]
        if meta and path.suffix == ".py" and not script and not relative.startswith((".dev-checks/", "scripts/", "tests/")):
            always = False
        if path not in selected and not always:
            continue
        if path.suffix.lower() in ARTIFACT_SUFFIXES and path not in selected:
            continue
        name = _safe_file(root, path)
        manifest[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        env = environment(root)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        raise EnvironmentUnknown("실행 환경을 확정하지 못해 재사용하지 않아요") from e
    payload = {"version": CONTRACT_VERSION, "cmd": item["cmd"], "contract": meta, "context": context,
               "inputs": manifest, "environment": env,
               "checker": digest({name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                                  for name in ("task_checks.py", "check_results.py")})}
    # 승인 신원이 없는 호출이나 결정성이 선언되지 않은 실행은 절대 재사용하지 않는다.
    reusable = bool(meta and meta["reuse"] and meta["conditions"].get("deterministic") is True and context)
    return {**payload, "key": digest(payload), "reusable": reusable}


def _atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=True, sort_keys=True), encoding="utf-8")
    os.replace(temp, path)


class Store:
    """worktree 외부 서버 전용 경로만 사용한다. 완료 표식 없는 시도는 무효다."""
    def __init__(self, ref, run_id):
        if not re.fullmatch(r"[A-Z][A-Z0-9]*-\d+(?:-\d+)?", ref) or type(run_id) is not int or run_id < 1:
            raise ValueError("검사 실행 신원이 올바르지 않아요")
        self.root = RESULTS_DIR / ref / f"run-{run_id}"
        self.ref, self.run_id = ref, run_id

    def begin(self):
        self.root.mkdir(parents=True, exist_ok=True)
        n = 1
        while True:
            target = self.root / f"attempt-{n}"
            try:
                target.mkdir()
                return target
            except FileExistsError:
                n += 1

    def finish(self, attempt, results):
        import repo_snapshot
        files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in attempt.glob("*.json")}
        clean = [{k: repo_snapshot.SECRET.sub("[REDACTED]", v) if isinstance(v, str) else v for k, v in r.items()} for r in results]
        value = {"created": time.time(), "files": files, "results": clean}
        _atomic(attempt / "complete.json", {"payload": value, "signature": self.sign(value)})

    def sign(self, value):
        key_path = RESULTS_DIR / ".signing-key"
        try:
            with key_path.open("xb") as f:
                f.write(os.urandom(32))
        except FileExistsError:
            pass
        return hmac.new(key_path.read_bytes(), digest(value).encode(), hashlib.sha256).hexdigest()

    def find(self, key, cwd):
        attempts = self.root.parent.glob("run-*/attempt-*")
        for attempt in sorted((p for p in attempts if re.fullmatch(r"attempt-\d+", p.name)), key=lambda p: p.stat().st_mtime_ns, reverse=True):
            try:
                envelope = json.loads((attempt / "complete.json").read_text(encoding="utf-8"))
                complete = envelope["payload"]
                if not hmac.compare_digest(envelope["signature"], self.sign(complete)):
                    continue
                if not 0 <= time.time() - complete["created"] <= TTL_SECONDS:
                    continue
                for name, sha in complete["files"].items():
                    if Path(name).name != name:
                        continue
                    path = attempt / name
                    if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                        continue
                    result = json.loads(path.read_text(encoding="utf-8"))
                    if result.get("input", {}).get("key") != key or not result.get("cacheable") or result.get("status") != "passed":
                        continue
                    if any(Path(a).is_absolute() or ".." in Path(a).parts or not (attempt / a).is_file() or hashlib.sha256((attempt / a).read_bytes()).hexdigest() != h for a, h in result["stored_files"].items()):
                        continue
                    if any(not (Path(cwd) / a).is_file() or hashlib.sha256((Path(cwd) / a).read_bytes()).hexdigest() != h for a, h in result.get("artifacts", {}).items()):
                        continue
                    return result, attempt
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return None


def execute(cwd, item, runner, *, store=None, attempt=None, context=None):
    before, reason = None, ""
    try:
        before = snapshot(cwd, item, context)
    except EnvironmentUnknown as e:
        reason = str(e)
    except ValueError as e:
        return {"cmd": item["cmd"], "ok": False, "status": "failed", "seconds": 0.0, "output": "", "reason": str(e)}
    except (OSError, subprocess.SubprocessError) as e:
        reason = str(e)
    if before and before["reusable"] and store:
        found = store.find(before["key"], cwd)
        if found:
            old, origin = found
            try:
                confirmed = snapshot(cwd, item, context)["key"] == before["key"]
            except (OSError, ValueError, subprocess.SubprocessError):
                confirmed = False
            if confirmed:
                return {"cmd": item["cmd"], "ok": True, "status": "reused", "seconds": 0.0,
                        "original_seconds": old["seconds"], "source": str(origin),
                        "source_run_id": old["run_id"], "source_attempt": old["attempt"], "input_key": before["key"],
                        "reason": "승인·입력·환경 식별자가 같아요", "output": ""}
    result = runner(cwd, item["cmd"])
    result["status"] = "passed" if result["ok"] else "failed"
    after = None
    try:
        after = snapshot(cwd, item, context)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        reason = str(e)
    stable = bool(before and after and before["key"] == after["key"])
    if before and after and not stable and result["ok"]:
        result.update(ok=False, status="failed", reason="검사 중 입력이 바뀌었어요")
    if store:
        import repo_snapshot
        number = len(list(attempt.glob("result-*.json"))) + 1
        output = attempt / f"output-{number}.txt"
        output.write_text(repo_snapshot.SECRET.sub("[REDACTED]", result.get("output", "")), encoding="utf-8")
        artifacts = {}
        stored_files = {output.name: hashlib.sha256(output.read_bytes()).hexdigest()}
        for folder in (".dev-checks", ".ui-captures"):
            root = Path(cwd).resolve()
            for path in _files(root / folder):
                if path.suffix.lower() not in ARTIFACT_SUFFIXES:
                    continue
                name = _safe_file(root, path)
                data = path.read_bytes()
                artifacts[name] = hashlib.sha256(data).hexdigest()
                dest = attempt / "artifacts" / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                stored_files[dest.relative_to(attempt).as_posix()] = artifacts[name]
        record = {**result, "output": "", "input": before, "created": time.time(), "run_id": store.run_id,
                  "ref": store.ref, "attempt": attempt.name, "cacheable": stable and before["reusable"] and result["ok"],
                  "reuse_disabled_reason": reason, "stored_files": stored_files, "artifacts": artifacts}
        _atomic(attempt / f"result-{number}.json", record)
        result["detail_path"] = str(attempt / f"result-{number}.json")
    return result
