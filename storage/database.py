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
                CREATE TABLE IF NOT EXISTS abyss_ranking_cache (
                    id INTEGER PRIMARY KEY CHECK (id=1), data_json TEXT NOT NULL
                );
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
                CREATE TABLE IF NOT EXISTS abyss_maintenance_events (
                    end_time TEXT PRIMARY KEY,
                    start_time TEXT,
                    source_url TEXT,
                    observed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS abyss_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    spawn_time TEXT NOT NULL UNIQUE,
                    maintenance_start TEXT,
                    maintenance_end TEXT,
                    reported_by INTEGER,
                    reported_at TEXT NOT NULL,
                    source TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS abyss_subscribers (
                    user_id INTEGER PRIMARY KEY, subscribed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS server_open_subscribers (
                    user_id INTEGER PRIMARY KEY, subscribed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS maintenance_reminders (
                    maintenance_start TEXT PRIMARY KEY,
                    sent_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS train_members (
                    guild_id INTEGER NOT NULL,
                    car_no INTEGER NOT NULL CHECK (car_no BETWEEN 1 AND 3),
                    user_id INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('conductor', 'passenger')),
                    PRIMARY KEY (guild_id, user_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_train_one_conductor
                    ON train_members (guild_id, car_no)
                    WHERE role = 'conductor';
                CREATE TRIGGER IF NOT EXISTS trg_train_capacity
                BEFORE INSERT ON train_members
                WHEN (
                    SELECT COUNT(*)
                    FROM train_members
                    WHERE guild_id = NEW.guild_id AND car_no = NEW.car_no
                ) >= 3
                BEGIN
                    SELECT RAISE(ABORT, 'train_full');
                END;

                CREATE TABLE IF NOT EXISTS train_panels (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL
                );
            """)
