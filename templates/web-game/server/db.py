"""SQLite 점수 마이그레이션과 상위 기록을 관리한다."""
import os
from pathlib import Path
import sqlite3


def connect():
    path = Path(os.environ.get('DATA_DIR', str(Path(__file__).resolve().parents[1] / 'data'))) / 'app.db'
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    return c


def initialize():
    c = connect()
    try:
        with c:
            if c.execute('PRAGMA user_version').fetchone()[0] < 1:
                c.execute((Path(__file__).parent / 'migrations/001_scores.sql').read_text(encoding='utf-8'))
                c.execute('PRAGMA user_version=1')
    finally:
        c.close()


def top_scores():
    c = connect()
    try:
        return [dict(row) for row in c.execute('SELECT name, score FROM scores ORDER BY score DESC, id ASC LIMIT 10')]
    finally:
        c.close()


def save(name, score):
    c = connect()
    try:
        with c:
            c.execute('INSERT INTO scores(name, score) VALUES (?, ?)', (name, score))
    finally:
        c.close()
