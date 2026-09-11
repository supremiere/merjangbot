# 오래된 공지·알림 기록을 정리하고 SQLite 파일을 압축합니다.
from datetime import datetime, timedelta, timezone


def cleanup_database(db, now=None):
    now = now or datetime.now(timezone.utc)
    with db.connect() as conn:
        conn.execute(
            "DELETE FROM sent_posts WHERE rowid NOT IN (SELECT rowid FROM sent_posts ORDER BY rowid DESC LIMIT 500)"
        )
        conn.execute(
            "DELETE FROM abyss_alerts WHERE spawn_time < ?",
            ((now - timedelta(days=30)).isoformat(),),
        )
        conn.commit()
        conn.execute("VACUUM")
