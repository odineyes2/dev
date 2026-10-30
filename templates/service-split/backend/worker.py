"""업무 로직을 추가할 별도 워커의 종료 가능한 대기 루프다."""
from pathlib import Path
import signal
import sys
from threading import Event

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# [option:db]
from backend import db
# [/option:db]


def main():
    stopped = Event()
    for name in ('SIGINT', 'SIGTERM'):
        signal.signal(getattr(signal, name), lambda *_: stopped.set())
    # [option:db]
    db.initialize()
    # [/option:db]
    stopped.wait()


if __name__ == '__main__':
    main()
