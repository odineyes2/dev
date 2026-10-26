"""병합 뒤 재시작 전 마이그레이션 적용 여부와 워커 대기 상태를 확인한다."""

import fnmatch
import re
import subprocess
from dataclasses import dataclass
from pathlib import PurePosixPath


_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_COUNT = re.compile(r"[0-9]+\Z")
COMMAND_TIMEOUT = 15


@dataclass(frozen=True)
class MigrationCheck:
    status: str  # clear, pending, failed
    files: tuple[str, ...] = ()
    message: str = ""


@dataclass(frozen=True)
class WorkerBusyCheck:
    status: str  # idle, busy, unconfigured, failed
    count: int | None = None


def _run(command, repo, runner):
    """운영 조회 명령의 값과 오류 출력을 기록하거나 반환하지 않는다."""
    return runner(command, cwd=repo, shell=True, capture_output=True,
                  text=True, encoding="utf-8", errors="replace", timeout=COMMAND_TIMEOUT)


def check_migrations(repo: str, cfg: dict, sha: str, *, git_runner=subprocess.run,
                     command_runner=subprocess.run) -> MigrationCheck:
    """첫 부모와 병합 커밋의 추가·수정 SQL을 적용 목록과 대조한다.

    설정: migrations.glob은 저장소 상대 경로 패턴, applied_cmd는 적용된 파일의
    확장자를 뺀 version을 한 줄에 하나씩 출력하는 읽기 전용 명령이다.
    """
    spec = cfg.get("migrations")
    if spec is None:
        return MigrationCheck("clear")
    if not isinstance(spec, dict) or not isinstance(spec.get("glob"), str) or not spec["glob"]:
        return MigrationCheck("failed", message="마이그레이션 확인 실패: 경로 설정이 올바르지 않아요.")
    try:
        diff = git_runner(["git", "diff", "--name-only", "--diff-filter=AM", f"{sha}^1", sha],
                          cwd=repo, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=COMMAND_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return MigrationCheck("failed", message="마이그레이션 확인 실패: 병합 변경 파일을 읽지 못했어요.")
    if diff.returncode:
        return MigrationCheck("failed", message="마이그레이션 확인 실패: 병합 변경 파일을 읽지 못했어요.")
    files = tuple(sorted(f for f in diff.stdout.splitlines()
                         if fnmatch.fnmatchcase(f, spec["glob"])))
    if not files:
        return MigrationCheck("clear")
    command = spec.get("applied_cmd")
    if not isinstance(command, str) or not command.strip():
        return MigrationCheck("failed", files, "마이그레이션 확인 실패: 적용 목록 조회 명령이 없어요.")
    versions = [PurePosixPath(f).stem for f in files]
    if len(set(versions)) != len(versions) or any(not _VERSION.fullmatch(v) for v in versions):
        return MigrationCheck("failed", files, "마이그레이션 확인 실패: 파일 version이 올바르지 않아요.")
    try:
        result = _run(command, repo, command_runner)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return MigrationCheck("failed", files, "마이그레이션 확인 실패: 적용 목록을 읽지 못했어요.")
    if result.returncode:
        return MigrationCheck("failed", files, "마이그레이션 확인 실패: 적용 목록을 읽지 못했어요.")
    applied = result.stdout.splitlines()
    if any(not _VERSION.fullmatch(v) for v in applied) or len(applied) != len(set(applied)):
        return MigrationCheck("failed", files, "마이그레이션 확인 실패: 적용 목록 형식이 올바르지 않아요.")
    pending = tuple(f for f, version in zip(files, versions) if version not in applied)
    if pending:
        return MigrationCheck("pending", pending,
                              f"마이그레이션 적용 필요: {', '.join(pending)} — cutover 후 재시작")
    return MigrationCheck("clear")


def worker_apps(files, cfg: dict) -> tuple[str, ...]:
    """병합 파일과 when 패턴이 겹치는 워커 앱을 선언 순서로 돌려준다."""
    apps = []
    for worker in cfg.get("workers") or []:
        if any(fnmatch.fnmatchcase(f, pattern) for f in files for pattern in worker["when"]):
            if worker["app"] not in apps:
                apps.append(worker["app"])
    return tuple(apps)


def check_worker_busy(repo: str, cfg: dict, *, command_runner=subprocess.run) -> WorkerBusyCheck:
    """읽기 전용 작업 수 조회 결과를 해석한다. 명령이 없으면 워커를 자동 재시작하지 않는다."""
    command = cfg.get("worker_busy_cmd")
    if not command:
        return WorkerBusyCheck("unconfigured")
    try:
        result = _run(command, repo, command_runner)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return WorkerBusyCheck("failed")
    if result.returncode:
        return WorkerBusyCheck("failed")
    count = result.stdout.strip()
    if not _COUNT.fullmatch(count):
        return WorkerBusyCheck("failed")
    number = int(count)
    return WorkerBusyCheck("busy" if number else "idle", number)
