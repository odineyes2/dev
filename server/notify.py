"""
폰 알림(DEV-38) — ntfy로 보낸다. NTFY_TOPIC이 없으면 아무것도 하지 않는다.
별도 스레드에서 보내고, 실패해도 로그만 남긴다(본 작업을 막지 않는다). 본문·계획서는 보내지 않고 ref·제목·짧은 메모만.
헤더는 latin-1만 되므로 한국어 제목을 위해 JSON으로 게시한다(POST {NTFY_URL}, body에 topic).
"""
import json
import logging
import threading
import urllib.request

import config

log = logging.getLogger("dev.notify")
PRIORITY = {"default": 3, "high": 4}


def _post(payload: dict) -> None:
    headers = {"Content-Type": "application/json"}
    if config.NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {config.NTFY_TOKEN}"
    req = urllib.request.Request(config.NTFY_URL, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    urllib.request.urlopen(req, timeout=5).close()


def _send(ref: str, headline: str, message: str, priority: str) -> None:
    try:
        import issues   # issues가 이 모듈을 부르므로 여기서 가져온다
        title = issues.get_issue(ref)["title"]
        _post({"topic": config.NTFY_TOPIC, "title": headline, "message": f"{ref} {title}" + (f"\n{message}" if message else ""),
               "priority": PRIORITY.get(priority, 3), "click": f"{config.PUBLIC_URL}/#/issue/{ref}"})
    except Exception as e:   # 알림 실패는 본 작업과 상관없다
        log.warning("ntfy 알림 실패(%s): %s", ref, e)


def send(ref: str, headline: str, message: str = "", priority: str = "default") -> None:
    if not config.NTFY_TOPIC:
        return
    threading.Thread(target=_send, args=(ref, headline, message[:500], priority), daemon=True).start()
