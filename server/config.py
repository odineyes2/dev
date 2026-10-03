"""환경변수 — .env는 pm2(ecosystem.config.js)가 읽어 넘겨준다. 테스트는 os.environ을 먼저 바꾸고 import한다."""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DEV_DATA_DIR") or REPO_ROOT / "data")
DB_PATH = Path(os.environ.get("DEV_DB_PATH") or DATA_DIR / "dev.db")
PORT = int(os.environ.get("DEV_PORT") or 8300)
# 사람 로그인은 nightshift가 맡는다 — 공유 쿠키(ns_session)로 이 주소의 /api/auth/me에 물어본다.
NIGHTSHIFT_URL = (os.environ.get("DEV_NIGHTSHIFT_URL") or "http://127.0.0.1:8000").rstrip("/")
NIGHTSHIFT_PUBLIC_URL = (os.environ.get("DEV_NIGHTSHIFT_PUBLIC_URL") or "https://nightshift.lomebrote.com").rstrip("/")
PUBLIC_URL = (os.environ.get("DEV_PUBLIC_URL") or "https://dev.lomebrote.com").rstrip("/")
# 폰 알림(ntfy, DEV-38) — 토픽이 없으면 보내지 않는다. 공개 ntfy.sh면 토픽 이름이 곧 비밀이다.
NTFY_URL = (os.environ.get("NTFY_URL") or "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC") or ""
NTFY_TOKEN = os.environ.get("NTFY_TOKEN") or ""
