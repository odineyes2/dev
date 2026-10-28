"""검사용 임시 PostgreSQL — 프로젝트 설정(orchestrate.json의 isolated_pg)이 켠 단계마다 빈 클러스터를 띄우고 끝나면 지운다(DEV-183).

설정 예(프로젝트 키별):
    "MO": {"isolated_pg": {"url_env": "MOAI_TEST_DATABASE_URL", "env": {"MOAI_TEST_DATABASE_ALLOW": "isolated"},
                           "stages": ["agent"], "database": "moai_test"}}
stages는 agent(에이전트 실행)·server_completion(완료 전 서버 검사)·server_merge(병합 검사) 중 켤 것.
클러스터는 데이터 폴더의 pg-clusters 아래(작업 worktree 밖 — 검사 산출물이 변경 파일로 잡히지 않게)에 두고,
127.0.0.1의 비어 있는 포트·trust 인증으로만 띄운다. 운영 DB 접속 정보나 비밀값은 쓰지 않는다.
"""
import contextlib
import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

import config

ROOT = config.DATA_DIR / "pg-clusters"
STAGES = ("agent", "server_completion", "server_merge")
BOOT = uuid.uuid4().hex   # 이 dev 프로세스가 만든 클러스터 표시 — 다르면 이전 프로세스가 남긴 것이다
MAX_AGE_SEC = 6 * 3600   # 같은 프로세스라도 이보다 오래된 클러스터는 버려진 것으로 본다
UNAVAILABLE = "격리 PG 없음 — 실제 PG 검사 미실행"
RESERVED_PORTS = {5432}
_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class ClusterError(RuntimeError):
    pass


def settings(project_key: str) -> dict | None:
    """프로젝트의 isolated_pg 설정(없거나 형식이 틀리면 None)."""
    try:
        cfg = (json.loads((config.DATA_DIR / "orchestrate.json").read_text(encoding="utf-8")).get(project_key) or {}).get("isolated_pg")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(cfg, dict) or not isinstance(cfg.get("url_env"), str) or not cfg["url_env"]:
        return None
    return cfg


def wanted(project_key: str, stage: str) -> dict | None:
    cfg = settings(project_key)
    if cfg is None:
        return None
    stages = cfg.get("stages", list(STAGES))
    return cfg if isinstance(stages, list) and stage in stages else None


def _bin(cfg: dict, name: str) -> str:
    exe = name + (".exe" if os.name == "nt" else "")
    folder = cfg.get("bin_dir")
    if isinstance(folder, str) and folder:
        path = Path(folder) / exe
        if path.is_file():
            return str(path)
    found = shutil.which(name)
    if found:
        return found
    # Windows 설치본: 가장 높은 버전의 bin
    for base in sorted(Path(os.environ.get("ProgramFiles", r"C:\Program Files"), "PostgreSQL").glob("*/bin"),
                       key=lambda p: int(p.parent.name) if p.parent.name.isdigit() else 0, reverse=True):
        if (base / exe).is_file():
            return str(base / exe)
    raise ClusterError(f"{name}을 찾지 못했어요")


def _free_port() -> int:
    for _ in range(20):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in RESERVED_PORTS:
            return port
    raise ClusterError("빈 포트를 찾지 못했어요")


def _run(args: list[str], timeout: int = 120) -> int:
    # pg_ctl start의 출력을 파이프로 받으면 postgres가 그 핸들을 물고 있어 끝나지 않는다 — 출력은 버린다.
    return subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          timeout=timeout, creationflags=_FLAGS).returncode


def _stop(folder: Path, cfg: dict | None = None) -> None:
    data = folder / "data"
    if (data / "postmaster.pid").exists():
        try:
            _run([_bin(cfg or {}, "pg_ctl"), "-D", str(data), "-m", "immediate", "-w", "stop"], timeout=60)
        except (ClusterError, OSError, subprocess.TimeoutExpired):
            pass
    shutil.rmtree(folder, ignore_errors=True)


def cleanup_stale(cfg: dict | None = None) -> int:
    """이전 dev 프로세스가 남겼거나 너무 오래된 클러스터를 멈추고 지운다. 지운 개수."""
    if not ROOT.is_dir():
        return 0
    removed = 0
    for folder in ROOT.iterdir():
        if not folder.is_dir():
            continue
        try:
            owner = json.loads((folder / "owner.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            owner = {}
        age = time.time() - float(owner.get("created", 0) or 0)
        if owner.get("boot") != BOOT or age > MAX_AGE_SEC:
            _stop(folder, cfg)
            removed += 1
    return removed


def start(project_key: str, stage: str, label: str = "") -> tuple[Path, dict]:
    """클러스터를 띄우고 (폴더, 검사에 넘길 환경 변수)를 돌려준다. 실패하면 남은 것을 지우고 ClusterError."""
    cfg = wanted(project_key, stage)
    if cfg is None:
        raise ClusterError("설정되지 않았어요")
    cleanup_stale(cfg)
    ROOT.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in (label or project_key))[:40]
    folder = ROOT / f"{safe}-{stage}-{uuid.uuid4().hex[:8]}"
    folder.mkdir()
    (folder / "owner.json").write_text(json.dumps({"boot": BOOT, "created": time.time(), "label": label, "stage": stage}),
                                       encoding="utf-8")
    try:
        data = folder / "data"
        if _run([_bin(cfg, "initdb"), "-D", str(data), "-U", "postgres", "--auth=trust", "-E", "UTF8"], timeout=180):
            raise ClusterError("initdb 실패")
        database = cfg.get("database") if isinstance(cfg.get("database"), str) and cfg["database"] else "test"
        # 다른 실행과 포트가 겹치면(바인드 확인 뒤 짧은 경쟁) 새 포트로 다시 시도한다.
        for _ in range(3):
            port = _free_port()
            code = _run([_bin(cfg, "pg_ctl"), "-D", str(data), "-l", str(folder / "server.log"), "-w", "-t", "60",
                         "-o", f"-p {port} -c listen_addresses=127.0.0.1", "start"], timeout=120)
            if code == 0:
                break
        else:
            raise ClusterError("pg_ctl start 실패")
        if _run([_bin(cfg, "createdb"), "-h", "127.0.0.1", "-p", str(port), "-U", "postgres", database], timeout=60):
            raise ClusterError("createdb 실패")
    except Exception:
        _stop(folder, cfg)
        raise
    extra = {k: v for k, v in (cfg.get("env") or {}).items() if isinstance(k, str) and isinstance(v, str)}
    return folder, {**extra, cfg["url_env"]: f"postgresql://postgres@127.0.0.1:{port}/{database}"}


@contextlib.contextmanager
def cluster(project_key: str, stage: str, label: str = ""):
    """(환경 변수 또는 None, 안내 문구 또는 None)을 준다. 설정이 꺼진 단계면 (None, None).
    띄우지 못해도 막지 않고 (None, UNAVAILABLE…)을 준다 — 호출한 쪽이 기록한다."""
    if wanted(project_key, stage) is None:
        yield None, None
        return
    folder = None
    try:
        folder, env = start(project_key, stage, label)
    except Exception as error:   # 띄우지 못해도 실행은 계속한다
        reason = str(error) if isinstance(error, ClusterError) else type(error).__name__
        yield None, f"{UNAVAILABLE} ({reason})"
        return
    try:
        yield env, None
    finally:
        _stop(folder, settings(project_key))


def agent_notice(env: dict | None, note: str | None, project_key: str) -> str:
    """에이전트 프롬프트 앞에 붙일 안내."""
    cfg = settings(project_key) or {}
    if env:
        return (f"검사용 격리 PostgreSQL이 이 실행 동안 떠 있다(환경 변수 {cfg.get('url_env')}에 접속 주소가 있다). "
                "이번 Task와 관련된, 실제 PostgreSQL이 필요한 검사를 건너뛰지 말고 실행한다. "
                "이 DB는 빈 상태로 시작해 실행이 끝나면 지워진다. 운영 DB에는 접속하지 않는다.\n\n")
    if note:
        return (f"{note}. 실제 PostgreSQL이 필요한 검사는 실행할 수 없으니, 완료 메모의 검사 목록에 "
                f"'{UNAVAILABLE}'으로 분명히 적는다.\n\n")
    return ""
