# 기존 룬 통계 캐시 테이블을 그대로 사용해 직업별 데이터와 갱신 시각을 저장합니다.
import json
from datetime import datetime, timezone


class RuneStatsRepository:
    def __init__(self, database):
        self.database = database

    def load(self):
        result = {}
        with self.database.connect() as conn:
            for class_name, data_json in conn.execute(
                "SELECT class_name, data_json FROM rune_stats_cache"
            ):
                try:
                    data = json.loads(data_json)
                except (TypeError, ValueError):
                    continue
                if isinstance(data, dict):
                    result[class_name] = data
        return result

    def save(self, parsed):
        fetched_at = datetime.now(timezone.utc).isoformat()
        with self.database.connect() as conn:
            conn.executemany(
                """
                INSERT INTO rune_stats_cache (class_name, data_json, data_date, fetched_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(class_name) DO UPDATE SET
                    data_json=excluded.data_json,
                    data_date=excluded.data_date,
                    fetched_at=excluded.fetched_at
            """,
                [
                    (
                        name,
                        json.dumps(data, ensure_ascii=False),
                        data.get("data_date") or "",
                        fetched_at,
                    )
                    for name, data in parsed.items()
                ],
            )
