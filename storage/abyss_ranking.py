# 검증을 마친 시즌 순위표 전체를 하나의 트랜잭션으로 저장합니다.
import json
from datetime import datetime


class AbyssRankingRepository:
    def __init__(self, database):
        self.database = database

    def load(self):
        with self.database.connect() as conn:
            row = conn.execute("SELECT data_json FROM abyss_ranking_cache WHERE id=1").fetchone()
        if row is None:
            return None
        data = json.loads(row[0])
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("지원하지 않는 랭킹 캐시")
        if not isinstance(data.get("entries"), list) or not data["entries"]:
            raise ValueError("랭킹 캐시가 비어 있습니다.")
        if datetime.fromisoformat(data["fetched_at"]).tzinfo is None:
            raise ValueError("캐시 갱신 시각에 시간대가 없습니다.")
        return data

    def save(self, snapshot):
        with self.database.connect() as conn:
            conn.execute("""
                INSERT INTO abyss_ranking_cache (id, data_json) VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET data_json=excluded.data_json
            """, (json.dumps(snapshot, ensure_ascii=False),))
