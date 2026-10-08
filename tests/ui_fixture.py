"""UI ??? ??? ???? ?? ??? ?? ? ?? ???."""
import os, socket, subprocess, sys, time
from pathlib import Path

FAKE_NS = r'''
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
app = FastAPI()
@app.get("/api/auth/me")
def me(request: Request):
    users = {"adm": {"id": 1, "username": "admin", "role": "admin"},
             "mem": {"id": 2, "username": "admin", "role": "user"}}
    return {"user": users.get(request.cookies.get("ns_session"))}
@app.post("/api/auth/login")
async def login(request: Request):
    b = await request.json()
    if b.get("password") != "pw":
        return JSONResponse({"detail": "아이디 또는 비밀번호가 올바르지 않아요."}, 401)
    r = JSONResponse({"user": {"id": 1, "username": "admin", "role": "admin"}})
    r.set_cookie("ns_session", "adm", path="/")
    return r
@app.post("/api/auth/logout")
def logout():
    r = JSONResponse({}); r.delete_cookie("ns_session", path="/"); return r
'''


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def wait_port(port):
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close(); return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"port {port} not up")


def child_environment(directory, ns_port, dev_port):
    """시스템 경로만 전달하고 운영 설정과 비밀값을 제거한다."""
    allowed = {'SYSTEMROOT', 'WINDIR', 'PATH', 'PATHEXT', 'COMSPEC',
               'LOCALAPPDATA', 'APPDATA', 'USERPROFILE', 'TEMP', 'TMP', 'VIRTUAL_ENV'}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    scratch = directory / 'tmp'
    scratch.mkdir(parents=True, exist_ok=True)
    env.update(DEV_DATA_DIR=str(directory / 'data'),
               DEV_DB_PATH=str(directory / 'data' / 'dev.db'),
               DEV_PORT=str(dev_port),
               DEV_NIGHTSHIFT_URL=f'http://127.0.0.1:{ns_port}',
               DEV_NIGHTSHIFT_PUBLIC_URL=f'http://127.0.0.1:{ns_port}',
               DEV_PUBLIC_URL=f'http://127.0.0.1:{dev_port}',
               DEV_CLAUDE_BIN=str(directory / 'disabled-claude'),
               DEV_CODEX_BIN=str(directory / 'disabled-codex'),
               DEV_REVIEW_MCP_CONFIG=str(directory / 'no-mcp.json'),
               TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch),
               FASTMCP_HOME=str(directory / 'fastmcp'),
               PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
    return env



# 앱 lifespan 대신 빈 DB만 초기화한다. 복구·병합·작업 타이머는 시작하지 않는다.
BOOTSTRAP = '''
import socket, sys
sys.path.insert(0, sys.argv[1])
allowed = {int(sys.argv[2]), int(sys.argv[3])}
original = socket.socket.connect
original_connect_ex = socket.socket.connect_ex
original_getaddrinfo = socket.getaddrinfo
original_bind = socket.socket.bind
def bind(self, address):
    result = original_bind(self, address)
    if isinstance(address, tuple) and address[0] == '127.0.0.1':
        allowed.add(self.getsockname()[1])
    return result
socket.socket.bind = bind
def connect(self, address):
    if not isinstance(address, tuple) or address[0] != '127.0.0.1' or address[1] not in allowed:
        raise OSError('capture: external connection blocked')
    return original(self, address)
socket.socket.connect = connect
def connect_ex(self, address):
    if not isinstance(address, tuple) or address[0] != '127.0.0.1' or address[1] not in allowed:
        raise OSError('capture: external connection blocked')
    return original_connect_ex(self, address)
socket.socket.connect_ex = connect_ex
def getaddrinfo(host, port, *args, **kwargs):
    if host not in ('127.0.0.1', b'127.0.0.1') or int(port) not in allowed:
        raise OSError('capture: external connection blocked')
    return original_getaddrinfo(host, port, *args, **kwargs)
socket.getaddrinfo = getaddrinfo
import db
db.init()
import app, uvicorn
uvicorn.run(app.app, host='127.0.0.1', port=int(sys.argv[3]), lifespan='off')
'''

