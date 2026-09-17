# 알림 구독자의 신청·해제와 목록 조회를 담당합니다.
from datetime import datetime, timezone


class _ToggleSubscriptionRepository:
    table_name = ""

    def __init__(self, db):
        self.db = db

    def list_ids(self):
        with self.db.connect() as conn:
            return [
                row[0]
                for row in conn.execute(
                    f"SELECT user_id FROM {self.table_name} ORDER BY subscribed_at ASC"
                )
            ]

    def toggle(self, user_id):
        with self.db.connect() as conn:
            exists = (
                conn.execute(
                    f"SELECT 1 FROM {self.table_name} WHERE user_id = ?", (user_id,)
                ).fetchone()
                is not None
            )
            if exists:
                conn.execute(
                    f"DELETE FROM {self.table_name} WHERE user_id = ?", (user_id,)
                )
            else:
                conn.execute(
                    f"INSERT INTO {self.table_name} (user_id, subscribed_at) VALUES (?, ?)",
                    (user_id, datetime.now(timezone.utc).isoformat()),
                )
            count = conn.execute(
                f"SELECT COUNT(*) FROM {self.table_name}"
            ).fetchone()[0]
            return not exists, count


class SubscriptionRepository(_ToggleSubscriptionRepository):
    """어비스 구멍 사전알림 구독자."""

    table_name = "abyss_subscribers"


class OpenSubscriptionRepository(_ToggleSubscriptionRepository):
    """점검 종료 후 서버 오픈 멘션 구독자."""

    table_name = "server_open_subscribers"
