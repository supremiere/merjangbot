# 어비스 알림 구독자의 신청·해제와 목록 조회를 담당합니다.
from datetime import datetime, timezone


class SubscriptionRepository:
    def __init__(self, db):
        self.db = db

    def list_ids(self):
        with self.db.connect() as conn:
            return [
                row[0]
                for row in conn.execute(
                    "SELECT user_id FROM abyss_subscribers ORDER BY subscribed_at ASC"
                )
            ]

    def toggle(self, user_id):
        with self.db.connect() as conn:
            exists = (
                conn.execute(
                    "SELECT 1 FROM abyss_subscribers WHERE user_id = ?", (user_id,)
                ).fetchone()
                is not None
            )
            if exists:
                conn.execute(
                    "DELETE FROM abyss_subscribers WHERE user_id = ?", (user_id,)
                )
            else:
                conn.execute(
                    "INSERT INTO abyss_subscribers (user_id, subscribed_at) VALUES (?, ?)",
                    (user_id, datetime.now(timezone.utc).isoformat()),
                )
            count = conn.execute("SELECT COUNT(*) FROM abyss_subscribers").fetchone()[0]
            return not exists, count
