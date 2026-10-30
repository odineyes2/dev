"""SQLite 마이그레이션을 순서대로 적용한다."""
import os
from pathlib import Path
import sqlite3

MIGRATIONS = ["CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"]


def connect():
    path = Path(os.environ.get('DATA_DIR', str(Path(__file__).resolve().parent.parent / 'data'))) / 'app.db'
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA journal_mode=WAL')
    return connection


def initialize():
    connection = connect()
    try:
        with connection:
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            for number, sql in enumerate(MIGRATIONS, 1):
                if number > version:
                    connection.execute(sql)
                    connection.execute(f'PRAGMA user_version={number}')
    finally:
        connection.close()
