"""
누가 부르는지 — 사람(nightshift admin 세션) 또는 에이전트(API 키).

- 사람: nightshift가 Domain=lomebrote.com으로 심은 `ns_session` 쿠키를 nightshift `/api/auth/me`에 물어
  role이 admin인지 본다(jupyter 게이트와 같은 방식). dev는 nightshift DB를 직접 열지 않는다.
  로그인·로그아웃도 nightshift API로 그대로 넘기고, 돌아온 Set-Cookie를 브라우저에 전해 준다.
- 에이전트: `Authorization: Bearer dev_<키>`. DB에는 sha256 해시와 앞 몇 글자(화면 표시용)만 둔다.

actor 문자열(이벤트·작성자에 적는 값): 사람 "human:<username>", 에이전트 "agent:<id>".
"""
import hashlib
import secrets
import time

import httpx

import config
import db

SESSION_COOKIE = "ns_session"
KEY_PREFIX = "dev_"
_client = httpx.AsyncClient(timeout=10.0)   # 테스트가 가짜 transport로 바꿔 끼운다

# 같은 쿠키로 곧 다시 오면 nightshift에 또 묻지 않는다.
_AUTH_CACHE_TTL = 15.0
_auth_cache: dict[str, tuple[dict | None, float]] = {}


async def nightshift_user(cookie_value: str) -> dict | None:
    """쿠키의 nightshift 사용자(dict) 또는 None(비로그인·연결 실패)."""
    if not cookie_value:
        return None
    now = time.monotonic()
    cached = _auth_cache.get(cookie_value)
    if cached and cached[1] > now:
        return cached[0]
    if len(_auth_cache) > 500:
        _auth_cache.clear()
    user = None
    try:
        resp = await _client.get(f"{config.NIGHTSHIFT_URL}/api/auth/me", headers={"Cookie": f"{SESSION_COOKIE}={cookie_value}"})
        if resp.status_code == 200:
            user = resp.json().get("user") or None
    except (httpx.HTTPError, ValueError):
        return None   # 연결 실패는 기억하지 않는다 — nightshift가 돌아오면 바로 다시 된다
    _auth_cache[cookie_value] = (user, now + _AUTH_CACHE_TTL)
    return user


def forget_cookie(cookie_value: str) -> None:
    _auth_cache.pop(cookie_value, None)


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def agent_for_key(key: str) -> dict | None:
    """켜져 있는 에이전트면 dict, 아니면 None. 부를 때마다 last_seen_at을 남긴다."""
    if not key.startswith(KEY_PREFIX):
        return None
    with db.connect() as c:
        row = c.execute("SELECT * FROM agents WHERE key_hash=? AND enabled=1", (_hash_key(key),)).fetchone()
        if row is None:
            return None
        c.execute("UPDATE agents SET last_seen_at=? WHERE id=?", (db.now_iso(), row["id"]))
    return public_agent(row)


def public_agent(row) -> dict:
    d = dict(row)
    d.pop("key_hash", None)
    d["enabled"] = bool(d["enabled"])
    return d


def _new_key() -> tuple[str, str, str]:
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    return key, _hash_key(key), key[:len(KEY_PREFIX) + 6]


def create_agent(name: str, vendor: str = "", model: str = "") -> tuple[dict, str]:
    """새 에이전트와 키. 키는 여기서 한 번만 돌려준다(DB에는 해시만)."""
    key, key_hash, prefix = _new_key()
    with db.connect() as c:
        aid = c.execute("INSERT INTO agents(name, vendor, model, key_hash, key_prefix, created_at) VALUES(?,?,?,?,?,?)",
                        (name, vendor, model, key_hash, prefix, db.now_iso())).lastrowid
        row = c.execute("SELECT * FROM agents WHERE id=?", (aid,)).fetchone()
    return public_agent(row), key


def rotate_key(agent_id: int) -> str | None:
    key, key_hash, prefix = _new_key()
    with db.connect() as c:
        n = c.execute("UPDATE agents SET key_hash=?, key_prefix=? WHERE id=?", (key_hash, prefix, agent_id)).rowcount
    return key if n else None


def actor_label(actor: dict) -> str:
    return f"human:{actor['name']}" if actor["kind"] == "human" else f"agent:{actor['id']}"
