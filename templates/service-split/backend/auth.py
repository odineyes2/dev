"""nightshift 세션을 확인하고 관리자만 허용한다."""
import os
import time

import httpx
from fastapi import HTTPException, Request

_cache = {}
CACHE_TTL = 30


async def nightshift_user(cookie):
    if not cookie:
        return None
    now = time.monotonic()
    cached = _cache.get(cookie)
    if cached and cached[1] > now:
        return cached[0]
    url = os.environ.get('NIGHTSHIFT_URL', '').rstrip('/')
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.get(url + '/api/auth/me', headers={'Cookie': 'ns_session=' + cookie})
        user = response.json().get('user') if response.status_code == 200 else None
        if not isinstance(user, dict):
            user = None
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    if len(_cache) >= 500:
        _cache.clear()
    _cache[cookie] = (user, now + CACHE_TTL)
    return user


async def require_admin(request: Request):
    user = await nightshift_user(request.cookies.get('ns_session', ''))
    if not user:
        raise HTTPException(401, 'nightshift에서 로그인해 주세요.')
    if user.get('role') != 'admin':
        raise HTTPException(403, '관리자만 사용할 수 있어요.')
    return {'username': user.get('username', ''), 'role': 'admin'}
