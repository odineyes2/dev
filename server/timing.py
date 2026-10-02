"""요청 속도 측정(DEV-42) — 요청마다 구간별 ms를 모아 Server-Timing 헤더로 내보내고, 느린 요청은 timing.log에 한 줄 남긴다."""
import contextvars
import time
from contextlib import contextmanager

import config

SLOW_MS = 300   # 이보다 오래 걸린 요청만 기록한다
LOG_PATH = config.DATA_DIR / "timing.log"

_spans: contextvars.ContextVar[list | None] = contextvars.ContextVar("timing_spans", default=None)


def begin() -> list:
    """요청 시작 — 이 요청의 구간 목록을 만든다(같은 dict·list를 스레드풀 핸들러도 같이 본다)."""
    spans = []
    _spans.set(spans)
    return spans


@contextmanager
def span(name: str):
    """with timing.span("db"): … — 요청 밖(테스트·스레드)에서 불러도 아무 일 없다."""
    spans = _spans.get()
    t = time.perf_counter()
    try:
        yield
    finally:
        if spans is not None:
            spans.append((name, (time.perf_counter() - t) * 1000))


def header(spans: list, total_ms: float) -> str:
    """Server-Timing 값 — 같은 이름이 여러 번이면 더해서 한 항목으로."""
    acc: dict[str, float] = {}
    for name, ms in spans:
        acc[name] = acc.get(name, 0) + ms
    return ", ".join([f"{k};dur={v:.1f}" for k, v in acc.items()] + [f"total;dur={total_ms:.1f}"])


def log_slow(method: str, path: str, status: int, value: str, total_ms: float):
    if total_ms < SLOW_MS:
        return
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {method} {path} {status} {value}\n")
    except OSError:
        pass   # 기록 실패가 응답을 망치지 않게
