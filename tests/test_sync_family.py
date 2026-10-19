"""family.css 복사 도구(DEV-142-1) — 임시 폴더로 복사·--check 통과, 한 글자 바꾸면 실패, 첫 줄 해시가 본문과 맞는지 확인한다."""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "sync_family.py"
SOURCE = ROOT / "static" / "family.css"


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, encoding="utf-8")


with tempfile.TemporaryDirectory() as tmp:
    dest = Path(tmp) / "static"
    assert run(str(dest)).returncode == 0
    copied = dest / "family.css"
    data = copied.read_bytes()
    first, _, body = data.partition(b"\n")
    assert body == SOURCE.read_bytes(), "본문이 원본과 같아야 한다"
    import hashlib
    m = re.match(r"^/\* family\.css — 원본 dev@\S+ sha256:([0-9a-f]{64}) · 여기서 고치지 말 것 \*/$", first.decode("utf-8"))
    assert m and m.group(1) == hashlib.sha256(body).hexdigest(), "첫 줄 해시가 본문과 맞아야 한다"
    assert run("--check", str(dest)).returncode == 0

    # 본문 한 글자 변경 → 실패
    copied.write_bytes(data.replace(b"--bg:#F7F8FA", b"--bg:#F7F8FB", 1))
    assert run("--check", str(dest)).returncode != 0, "손댄 복사본은 실패해야 한다"

    # 다시 복사하면 통과, 없는 대상은 실패
    assert run(str(dest)).returncode == 0
    assert run("--check", str(dest)).returncode == 0
    assert run("--check", str(Path(tmp) / "none")).returncode != 0

print("ok")
