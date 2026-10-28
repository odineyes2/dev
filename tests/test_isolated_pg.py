"""격리 PostgreSQL(DEV-183) — 설정·단계 선택, 실제 클러스터 띄우기·지우기, 동시 실행 분리, 남은 클러스터 정리,
띄우지 못할 때의 안내, 실행 스레드·서버 검사에 접속 주소가 넘어가는지를 임시 데이터 폴더로 확인한다.
PostgreSQL 실행 파일이 없으면 실제 클러스터 검사는 건너뛴다."""
import json, os, socket, sys, tempfile
from pathlib import Path
from unittest.mock import patch

os.environ["DEV_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import config  # noqa: E402
import isolated_pg  # noqa: E402

SETTINGS = config.DATA_DIR / "orchestrate.json"


def configure(stages):
    SETTINGS.write_text(json.dumps({"MO": {"isolated_pg": {
        "url_env": "MOAI_TEST_DATABASE_URL", "env": {"MOAI_TEST_DATABASE_ALLOW": "isolated"},
        "stages": stages, "database": "moai_test"}}}), encoding="utf-8")


def listening(port):
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def port_of(env):
    return int(env["MOAI_TEST_DATABASE_URL"].rsplit(":", 1)[1].split("/")[0])


# 설정 해석: 없는 프로젝트·꺼진 단계는 None, 형식이 틀리면 None
configure(["agent"])
assert isolated_pg.wanted("MO", "agent")["database"] == "moai_test"
assert isolated_pg.wanted("MO", "server_merge") is None
assert isolated_pg.wanted("NS", "agent") is None
with isolated_pg.cluster("MO", "server_merge", "MO-1-1") as (env, note):
    assert (env, note) == (None, None)
SETTINGS.write_text(json.dumps({"MO": {"isolated_pg": {"stages": ["agent"]}}}), encoding="utf-8")
assert isolated_pg.settings("MO") is None

# 띄우지 못하면 막지 않고 안내만 준다
configure(["agent"])
with patch.object(isolated_pg, "_bin", side_effect=isolated_pg.ClusterError("initdb을 찾지 못했어요")):
    with isolated_pg.cluster("MO", "agent", "MO-1-1") as (env, note):
        assert env is None and note.startswith(isolated_pg.UNAVAILABLE), note
assert isolated_pg.UNAVAILABLE in isolated_pg.agent_notice(None, note, "MO")
assert not list(isolated_pg.ROOT.glob("*")) if isolated_pg.ROOT.exists() else True

try:
    isolated_pg._bin({}, "initdb")
    have_pg = True
except isolated_pg.ClusterError:
    have_pg = False
    print("PostgreSQL 실행 파일이 없어 실제 클러스터 검사를 건너뛴다")

if have_pg:
    # 실제 클러스터: 두 실행이 동시에 떠도 포트·폴더가 다르고, 끝나면 멈추고 지워진다
    configure(["agent", "server_completion"])
    with isolated_pg.cluster("MO", "agent", "MO-1-1") as (a, note_a), \
         isolated_pg.cluster("MO", "server_completion", "MO-1-2") as (b, note_b):
        assert note_a is None and note_b is None, (note_a, note_b)
        assert a["MOAI_TEST_DATABASE_ALLOW"] == "isolated" and a["MOAI_TEST_DATABASE_URL"].endswith("/moai_test")
        assert port_of(a) != port_of(b) and 5432 not in (port_of(a), port_of(b))
        assert listening(port_of(a)) and listening(port_of(b))
        folders = sorted(p.name for p in isolated_pg.ROOT.iterdir())
        assert len(folders) == 2 and folders[0] != folders[1], folders
        assert "검사용 격리 PostgreSQL" in isolated_pg.agent_notice(a, None, "MO")
    assert not listening(port_of(a)) and not listening(port_of(b))
    assert not list(isolated_pg.ROOT.iterdir())

    # 이전 dev 프로세스가 남긴 클러스터는 다음 시작 전에 멈추고 지운다
    folder, env = isolated_pg.start("MO", "agent", "MO-1-3")
    owner = json.loads((folder / "owner.json").read_text(encoding="utf-8"))
    (folder / "owner.json").write_text(json.dumps({**owner, "boot": "old-process"}), encoding="utf-8")
    assert isolated_pg.cleanup_stale() == 1
    assert not folder.exists() and not listening(port_of(env))

    # 실행 스레드: 에이전트 명령 앞에 안내가 붙고 환경에 접속 주소가 넘어간다. 끝나면 지워진다.
    import execute, orchestrate, review
    seen = {}

    def fake_run(actor, ref, log_path, run_id, cmd, cwd, env, timeout, label, provider):
        seen.update(cmd=cmd, env=env, alive=listening(port_of(env)))
        return "ok"
    with patch.object(review, "run_headless", fake_run), patch.object(orchestrate, "handle", lambda *a: None):
        execute._run_then_merge({"kind": "agent", "id": 1}, "MO-9-1", Path("x.log"), 1,
                                ["codex", "exec", "원래 프롬프트"], Path("."), {"KEEP": "1"}, 60, "실행", "codex")
    assert seen["alive"] and seen["env"]["KEEP"] == "1"
    assert seen["cmd"][-1].startswith("검사용 격리 PostgreSQL") and seen["cmd"][-1].endswith("원래 프롬프트")
    assert not list(isolated_pg.ROOT.iterdir())

    # 서버 검사: 켜진 단계면 검사 환경에 접속 주소가 들어가고, 꺼진 단계면 기존처럼 env=None
    captured = []
    with patch.object(orchestrate, "_run_tests", lambda repo, stage, ref, changed, env: captured.append(env)):
        orchestrate.run_tests(".", stage="server_completion", ref="MO-9-1")
        orchestrate.run_tests(".", stage="server_merge", ref="MO-9-1")
        orchestrate.run_tests(".", stage="server_merge", ref=None)
    assert captured[0]["MOAI_TEST_DATABASE_URL"].startswith("postgresql://postgres@127.0.0.1:")
    assert "GH_TOKEN" not in captured[0]
    assert captured[1] is None and captured[2] is None

print("isolated_pg ok")
