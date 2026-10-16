"""파일 지도(DEV-134) — 추적 파일과 첫머리 설명을 폴더별로 묶고, 검토·실행 프롬프트 앞머리에 들어가며, Codex 문구 치환에 깨지지 않는다."""
import os, subprocess, sys, tempfile
from pathlib import Path

os.environ["DEV_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import filemap, harness, review  # noqa: E402

repo = Path(tempfile.mkdtemp())
files = {
    "server/app.py": '"""\nClaude에게 검토 맡기기 — 서버 본체.\n"""\nimport os\n',
    "server/one.py": '#!/usr/bin/env python\n# -*- coding: utf-8 -*-\n"""한 줄 문서 문자열."""\n',
    "static/app.js": "// 화면 진입점\nconst x = 1;\n",
    "static/style.css": "/* 공통 스타일 */\nbody{}\n",
    "README.md": "# 프로젝트 소개\n",
    "server/bare.py": "import os\n",
    "data/seed.json": "{}",            # 하위 json은 데이터라 뺀다
    "package.json": "{}",               # 최상위 설정은 넣는다
}
for name, body in files.items():
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(body, encoding="utf-8")
(repo / "untracked.py").write_text("# 추적 안 됨\n", encoding="utf-8")
git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, check=True, capture_output=True)
git("init", "-q"); git("add", *files); git("commit", "-qm", "init")

m = filemap.build(repo)
assert "server/\n  app.py — Claude에게 검토 맡기기 — 서버 본체." in m, m
assert "one.py — 한 줄 문서 문자열." in m and "app.js — 화면 진입점" in m and "style.css — 공통 스타일" in m
assert "README.md — 프로젝트 소개" in m and "\n  bare.py\n" in m + "\n"
assert "package.json" in m and "seed.json" not in m and "untracked" not in m
assert filemap.build(tempfile.mkdtemp()) == ""   # git 저장소가 아니면 빈 지도

# 프롬프트 앞머리 = 공통 하네스 + 지도. Codex 검토의 "Claude"→"Codex" 치환은 본문에만
review.filemap.section = lambda ref, execution=False: "## 파일 지도\n" + m + "\n\n---\n\n"
assert review.prompt_for("DEV-1").startswith(harness.text())
assert m in review.prompt_for("DEV-1")
codex = review.codex_command_for("DEV-1")[-1]
assert "Claude에게 검토 맡기기" in codex and "Codex 검토" not in codex.split("---")[0]
print("OK filemap")
