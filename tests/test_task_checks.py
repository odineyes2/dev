"""Task 검사(DEV-238) — 계획서 `## 검사` 절 파싱·형식 거절, Task 본문 옮기기, 체크리스트·선택, 명령 실행기, 언제나 절차(문법·비밀값)."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="dev-task-checks-")
os.environ["DEV_DATA_DIR"] = TMP
os.environ["DEV_DB_PATH"] = str(Path(TMP) / "dev.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import db  # noqa: E402
import task_checks as tc  # noqa: E402

db.init()   # 체크리스트가 프로젝트 설정을 읽는다 — 임시 DB

PLAN = """# 계획
## Tasks
1. 첫째 | 파일: a.py | 선행: 없음
2. 둘째 | 파일: README.md | 선행: 1

## 검사
- 1 (필수): `python tests/test_a.py` — a의 새 함수 확인
- 1: `node --check static/app.js` — 문법
- 2: 없음 — 문서만 고친다
"""


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class FormatTest(unittest.TestCase):
    def test_parse_section(self):
        s = tc.parse_section(PLAN)
        self.assertEqual([i["cmd"] for i in s[1]["items"]], ["python tests/test_a.py", "node --check static/app.js"])
        self.assertTrue(s[1]["items"][0]["required"])
        self.assertFalse(s[1]["items"][1]["required"])
        self.assertEqual(s[2]["none"], "문서만 고친다")
        self.assertIsNone(tc.parse_section("## Tasks\n1. x | 파일: a | 선행: 없음\n"))

    def test_rejects_bad_lines(self):
        for line in ("- 1: python tests/x.py — 백틱 없음", "- 1: `python tests/x.py`", "- 1: 없음",
                     "- 1: `curl http://x` — 외부", "- 1: `python -c print(1)` — 코드 실행", "- 1: `python tests/x.py; rm a` — 셸",
                     "- 1: `python C:/x.py` — 절대 경로", "- 1: `python ../x.py` — 위 폴더", "아무 말"):
            with self.subTest(line=line), self.assertRaises(tc.CheckFormatError):
                tc.parse_section("## 검사\n" + line + "\n")
        self.assertEqual(tc.parse_section("## 검사\n아무 말\n- 1: `node --check a.js` — 문법\n", strict=False)[1]["items"][0]["cmd"], "node --check a.js")

    def test_allowed_commands(self):
        for cmd in ("python tests/test_a.py --only x", "python -m unittest tests.test_a", "node --test tests/a.test.js",
                    "node --check static/app.js", "npm run build", "npm --prefix apps/web run typecheck"):
            with self.subTest(cmd=cmd):
                tc.command_args(cmd)

    def test_validate_plan(self):
        tc.validate_plan(PLAN)
        tc.validate_plan("계획만 있고 Task 절이 없다.")
        with self.assertRaises(tc.CheckFormatError):
            tc.validate_plan(PLAN.replace("- 2: 없음 — 문서만 고친다\n", ""))
        with self.assertRaises(tc.CheckFormatError):
            tc.validate_plan(PLAN + "- 3: 없음 — 없는 Task\n")
        with self.assertRaises(tc.CheckFormatError):
            tc.validate_plan("## Tasks\n1. x | 파일: a | 선행: 없음\n")

    def test_body_roundtrip_and_selection(self):
        s = tc.parse_section(PLAN)
        body = "DEV-1 계획서 v1의 Task 1.\n\n**바꿀 파일**: a.py" + tc.body_text(s[1])
        items, none = tc.from_body(body)
        self.assertIsNone(none)
        self.assertEqual([(i["key"], i["required"]) for i in items], [("p1", True), ("p2", False)])
        issue = {"body": body}
        self.assertEqual(tc.selection(issue, None), ["p1", "p2"])
        self.assertEqual(tc.selection(issue, []), ["p1"])   # 필수는 해제해도 남는다
        self.assertEqual(tc.selection(issue, ["p2"]), ["p1", "p2"])
        with self.assertRaises(Exception):
            tc.selection(issue, ["p9"])
        self.assertEqual([i["cmd"] for i in tc.selected(issue, ["p1"])], ["python tests/test_a.py"])
        self.assertIn("python tests/test_a.py", tc.prompt_text(issue, ["p1"]))
        self.assertNotIn("node --check", tc.prompt_text(issue, ["p1"]))
        none_body = "Task 2." + tc.body_text(s[2])
        self.assertEqual(tc.from_body(none_body), ([], "문서만 고친다"))
        self.assertIn("문서만 고친다", tc.prompt_text({"body": none_body}, None))

    def test_items_marks_required(self):
        issue = {"body": "Task 1.\n\n**바꿀 파일**: server/db.py, a.py" + tc.body_text(tc.parse_section(PLAN)[1]), "project_key": "DEV"}
        c = tc.items(issue)
        self.assertEqual([i["required"] for i in c["plan"]], [True, False])
        keys = [i["key"] for i in c["always"]]
        self.assertEqual(keys[:3], ["syntax", "secrets", "migration"])
        self.assertTrue(all(i["required"] for i in c["always"]))
        self.assertNotIn("migration", [i["key"] for i in tc.items({**issue, "body": issue["body"].replace("server/db.py, ", "")})["always"]])


class RunTest(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="dev-task-checks-repo-"))
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        (self.repo / "ok.py").write_text("print('ok')\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "checkout", "-q", "-b", "relay/DEV-1-1")

    def test_run_command(self):
        (self.repo / "pass.py").write_text("print('fine')\n", encoding="utf-8")
        (self.repo / "fail.py").write_text("raise SystemExit('boom')\n", encoding="utf-8")
        (self.repo / "slow.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        r = tc.run_command(self.repo, "python pass.py")
        self.assertTrue(r["ok"])
        self.assertIn("fine", r["output"])
        r = tc.run_command(self.repo, "python fail.py")
        self.assertFalse(r["ok"])
        self.assertIn("boom", r["output"])
        r = tc.run_command(self.repo, "python slow.py", timeout=2)
        self.assertFalse(r["ok"])
        self.assertIn("시간 제한", r["output"])

    def test_always_checks(self):
        (self.repo / "ok.py").write_text("print('changed')\n", encoding="utf-8")
        (self.repo / "broken.py").write_text("def x(:\n", encoding="utf-8")
        (self.repo / "leak.txt").write_text("token = " + "sk-" + "A" * 24 + "\n", encoding="utf-8")
        files, base = tc.changed(self.repo)
        self.assertEqual(set(files), {"ok.py", "broken.py", "leak.txt"})
        syntax = tc.check_syntax(self.repo, files)
        self.assertFalse(syntax["ok"])
        self.assertIn("broken.py", syntax["output"])
        secrets = tc.check_secrets(self.repo, files, base)
        self.assertFalse(secrets["ok"])
        self.assertIn("leak.txt", secrets["output"])
        self.assertNotIn("A" * 24, secrets["output"])   # 값은 남기지 않는다
        (self.repo / "broken.py").unlink()
        (self.repo / "leak.txt").unlink()
        files, base = tc.changed(self.repo)
        self.assertTrue(tc.check_syntax(self.repo, files)["ok"])
        self.assertTrue(tc.check_secrets(self.repo, files, base)["ok"])

    def test_report(self):
        report, ok = tc._report([{"cmd": "바꾼 파일 문법 확인", "ok": True, "seconds": 0.1, "output": "", "always": True},
                                 {"cmd": "python t.py", "ok": False, "seconds": 1.0, "output": "E" * 3000}])
        self.assertFalse(ok)
        self.assertIn("❌ `python t.py`", report)
        self.assertLess(report.count("E"), 2100)


if __name__ == "__main__":
    unittest.main()
