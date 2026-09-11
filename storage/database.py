# SQLite 연결과 운영 데이터 테이블 초기화를 담당합니다.
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Database:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS rune_stats_cache (
                    class_name TEXT PRIMARY KEY,
                    data_json TEXT NOT NULL,
                    data_date TEXT,
                    fetched_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sent_posts (
                    url TEXT PRIMARY KEY, category TEXT NOT NULL, title TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS abyss_alerts (
                    spawn_time TEXT NOT NULL, minutes_before INTEGER NOT NULL,
                    PRIMARY KEY (spawn_time, minutes_before)
                );
                CREATE TABLE IF NOT EXISTS abyss_subscribers (
                    user_id INTEGER PRIMARY KEY, subscribed_at TEXT NOT NULL
                );
            """)
