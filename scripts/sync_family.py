"""패밀리룩 공용 CSS(static/family.css)를 다른 프로젝트 static 폴더로 복사하거나, 복사본이 원본과 같은지 확인한다(DEV-142).

사용: python scripts/sync_family.py <대상 static 폴더>           복사 (첫 줄에 원본 커밋·본문 해시를 적는다)
      python scripts/sync_family.py --check <대상 static 폴더>   복사본이 원본과 다르면 0이 아닌 값으로 끝난다
"""
import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "static" / "family.css"
NAME = "family.css"
HEADER_RE = re.compile(r"^/\* family\.css — 원본 dev@(\S+) sha256:([0-9a-f]{64}) · 여기서 고치지 말 것 \*/$")


def body_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def source_commit() -> str:
    """원본 저장소의 현재 커밋(짧게). 알 수 없으면 unknown."""
    try:
        out = subprocess.run(["git", "rev-parse", "--short=10", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def header_line(body: bytes, commit: str) -> str:
    return f"/* family.css — 원본 dev@{commit} sha256:{body_hash(body)} · 여기서 고치지 말 것 */"


def split_copy(data: bytes):
    """복사본을 (첫 줄 문자열, 본문 bytes)로 가른다."""
    first, _, rest = data.partition(b"\n")
    return first.decode("utf-8", "replace").rstrip("\r"), rest


def check(dest_dir: Path) -> list[str]:
    """어긋난 점의 목록. 비어 있으면 같다."""
    target = dest_dir / NAME
    if not target.is_file():
        return [f"{target} 없음"]
    first, body = split_copy(target.read_bytes())
    problems = []
    m = HEADER_RE.match(first)
    if not m:
        problems.append("첫 줄이 sync 형식이 아님")
    elif m.group(2) != body_hash(body):
        problems.append("첫 줄 해시가 본문과 다름 — 복사본을 손으로 고친 듯")
    if body != SOURCE.read_bytes():
        problems.append("본문이 원본(dev static/family.css)과 다름")
    return problems


def copy(dest_dir: Path) -> Path:
    body = SOURCE.read_bytes()
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / NAME
    target.write_bytes(header_line(body, source_commit()).encode("utf-8") + b"\n" + body)
    return target


def main(argv: list[str]) -> int:
    args = [a for a in argv if a != "--check"]
    if len(args) != 1:
        print(__doc__)
        return 2
    dest = Path(args[0])
    if "--check" in argv:
        problems = check(dest)
        for p in problems:
            print("불일치:", p)
        if not problems:
            print("family.css 같음")
        return 1 if problems else 0
    print("복사:", copy(dest))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
