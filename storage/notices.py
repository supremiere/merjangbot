# 공지 전송 이력을 조회·저장하여 같은 게시물의 중복 알림을 막습니다.
class NoticeRepository:
    def __init__(self, db):
        self.db = db

    def is_empty(self):
        with self.db.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM sent_posts").fetchone()[0] == 0

    def is_sent(self, url):
        with self.db.connect() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM sent_posts WHERE url = ?", (url,)
                ).fetchone()
                is not None
            )

    def save(self, post):
        with self.db.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sent_posts (url, category, title) VALUES (?, ?, ?)",
                (post["url"], post["category"], post["title"]),
            )
