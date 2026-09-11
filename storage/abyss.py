# 출현 시각과 사전 알림 분 단위로 어비스 전송 이력을 관리합니다.
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
