# 어비스 알림 이력과 점검/첫 어구 관측 데이터를 저장합니다.
from datetime import datetime, timezone


class AbyssRepository:
    def __init__(self, db):
        self.db = db

    def is_sent(self, spawn_time, minutes_before):
        with self.db.connect() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM abyss_alerts WHERE spawn_time = ? AND minutes_before = ?",
                    (spawn_time, minutes_before),
                ).fetchone()
                is not None
            )

    def save(self, spawn_time, minutes_before):
        with self.db.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO abyss_alerts (spawn_time, minutes_before) VALUES (?, ?)",
                (spawn_time, minutes_before),
            )


class AbyssScheduleRepository:
    def __init__(self, db):
        self.db = db

    def seed_initial_observation(self):
        """독립 전환 기준이 된 2026-09-22 실측값을 1회만 저장한다."""
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO abyss_maintenance_events
                    (end_time, start_time, source_url, observed_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    "2026-09-21T23:45:00+00:00",
                    "2026-09-21T21:00:00+00:00",
                    "https://mabinogimobile.nexon.com/News/Notice/3549901",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO abyss_observations
                    (spawn_time, maintenance_start, maintenance_end,
                     reported_by, reported_at, source)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-09-22T05:28:00+00:00",
                    "2026-09-21T21:00:00+00:00",
                    "2026-09-21T23:45:00+00:00",
                    None,
                    datetime.now(timezone.utc).isoformat(),
                    "seed_2026-09-22",
                ),
            )

    def save_maintenance(self, start_time, end_time, source_url=None):
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO abyss_maintenance_events
                    (end_time, start_time, source_url, observed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(end_time) DO UPDATE SET
                    start_time=excluded.start_time,
                    source_url=COALESCE(excluded.source_url, abyss_maintenance_events.source_url),
                    observed_at=excluded.observed_at
                """,
                (
                    end_time,
                    start_time,
                    source_url,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def latest_maintenance(self):
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT start_time, end_time, source_url
                FROM abyss_maintenance_events
                ORDER BY end_time DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return {"start_time": row[0], "end_time": row[1], "source_url": row[2]}

    def save_observation(
        self,
        *,
        spawn_time,
        maintenance_start,
        maintenance_end,
        reported_by,
        source,
    ):
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO abyss_observations
                    (spawn_time, maintenance_start, maintenance_end,
                     reported_by, reported_at, source)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(spawn_time) DO UPDATE SET
                    maintenance_start=COALESCE(excluded.maintenance_start, abyss_observations.maintenance_start),
                    maintenance_end=COALESCE(excluded.maintenance_end, abyss_observations.maintenance_end),
                    reported_by=COALESCE(excluded.reported_by, abyss_observations.reported_by),
                    reported_at=excluded.reported_at,
                    source=excluded.source
                """,
                (
                    spawn_time,
                    maintenance_start,
                    maintenance_end,
                    reported_by,
                    datetime.now(timezone.utc).isoformat(),
                    source,
                ),
            )

    def latest_observation(self):
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT spawn_time, maintenance_start, maintenance_end,
                       reported_by, reported_at, source
                FROM abyss_observations
                ORDER BY spawn_time DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return {
            "spawn_time": row[0],
            "maintenance_start": row[1],
            "maintenance_end": row[2],
            "reported_by": row[3],
            "reported_at": row[4],
            "source": row[5],
        }

    def observation_count(self):
        with self.db.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM abyss_observations").fetchone()[0]

    def recent_observations(self, limit=50):
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT spawn_time, maintenance_start, maintenance_end,
                       reported_by, reported_at, source
                FROM abyss_observations
                ORDER BY spawn_time DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [
            {
                "spawn_time": row[0],
                "maintenance_start": row[1],
                "maintenance_end": row[2],
                "reported_by": row[3],
                "reported_at": row[4],
                "source": row[5],
            }
            for row in rows
        ]
