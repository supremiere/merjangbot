# 시즌별 어비스 순위표를 검증·캐시하고 닉네임 및 집계 순위를 조회합니다.
import asyncio
import logging
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)
API_PATH = "/d/api/v1/abyss-rankings"
REFRESH_SECONDS = 6 * 60 * 60
RETRY_SECONDS = 5 * 60
KST = timezone(timedelta(hours=9))
SERVERS = dict(zip(
    (f"{i:02}" for i in range(1, 9)),
    ("데이안", "아이라", "던컨", "알리사", "메이븐", "라사", "칼릭스", "몰리"),
))
CLASSES = dict(zip(
    [f"{i:02}" for i in range(1, 19)] + ["25", "26", "27"],
    ("전사", "대검전사", "검술사", "궁수", "석궁사수", "장궁병", "마법사",
     "화염술사", "빙결술사", "힐러", "사제", "수도사", "음유시인", "댄서",
     "악사", "도적", "격투가", "듀얼블레이드", "전격술사", "암흑술사", "기사"),
))


def normalized_name(value):
    return unicodedata.normalize("NFKC", value).strip().casefold()


def integer(value, label, minimum=None):
    if type(value) is not int or (minimum is not None and value < minimum):
        raise ValueError(f"잘못된 랭킹 {label}")
    return value


def iso_date(value):
    if not isinstance(value, str):
        raise ValueError("랭킹 기준일 누락")
    return date.fromisoformat(value).isoformat()


def season_time(value):
    if not isinstance(value, str):
        raise ValueError("시즌 시각 누락")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("시즌 시각에 시간대가 없습니다.")
    return parsed


def season_day(value):
    return season_time(value).astimezone(KST).date().isoformat()


def latest_season(payload):
    if not isinstance(payload, list) or not payload:
        raise ValueError("시즌 목록이 비어 있습니다.")
    now = datetime.now(timezone.utc)
    seasons = []
    for item in payload:
        start, end = season_time(item["season_start"]), season_time(item["season_end"])
        if start >= end:
            raise ValueError("잘못된 시즌 기간")
        if start <= now:
            seasons.append({"season_start": item["season_start"], "season_end": item["season_end"]})
    if not seasons:
        raise ValueError("조회할 시즌이 없습니다.")
    return max(seasons, key=lambda x: season_time(x["season_start"]))


def parse_table(payload, season, server, klass):
    if not isinstance(payload, dict):
        raise ValueError("랭킹 표 응답이 아닙니다.")
    snapshot = iso_date(payload.get("snapshot_date"))
    previous = payload.get("previous_snapshot_date")
    if previous is not None:
        previous = iso_date(previous)
        if previous >= snapshot:
            raise ValueError("비교일이 정산일보다 앞서지 않습니다.")
    if (season_time(payload.get("season_start")) != season_time(season)
            or snapshot < season_day(season)):
        raise ValueError("다른 시즌의 랭킹입니다.")
    entries = payload.get("entries")
    total = integer(payload.get("total"), "표본 수", 0)
    if not isinstance(entries, list) or len(entries) != min(total, 100):
        raise ValueError("랭킹 표본 수가 맞지 않습니다.")
    if total and not entries:
        raise ValueError("랭킹 행이 누락됐습니다.")
    parsed, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("잘못된 캐릭터 행")
        if entry.get("server") != server or entry.get("klass") != klass:
            raise ValueError("서버·직업 필터가 적용되지 않았습니다.")
        name, character_id = entry.get("character_name"), entry.get("character_id")
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError("캐릭터 이름이 없습니다.")
        if not isinstance(character_id, (str, int)) or isinstance(character_id, bool):
            raise ValueError("캐릭터 ID가 없습니다.")
        character_id = str(character_id)
        if not character_id or character_id in seen:
            raise ValueError("중복되거나 빈 캐릭터 ID")
        seen.add(character_id)
        rank = integer(entry.get("rank"), "순위", 1)
        score = integer(entry.get("score"), "점수", 0)
        if parsed and (rank < parsed[-1]["rank"] or score > parsed[-1]["score"]):
            raise ValueError("점수순 랭킹이 아닙니다.")
        deltas = {}
        for key in ("score_delta", "rank_delta"):
            value = entry.get(key)
            deltas[key] = None if value is None else integer(value, key)
        is_new = entry.get("is_new", False)
        if type(is_new) is not bool:
            raise ValueError("잘못된 신규 진입 표시")
        parsed.append({
            "character_id": character_id, "character_name": name,
            "server": server, "klass": klass, "rank": rank, "score": score,
            "is_new": is_new, **deltas,
        })
    return {"snapshot_date": snapshot, "previous_snapshot_date": previous,
            "total": total, "entries": parsed}


@dataclass(frozen=True)
class RankingResult:
    entry: dict
    snapshot: dict
    overall_rank: int
    server_rank: int
    class_rank: int
    overall_count: int
    server_count: int
    class_count: int
    stale: bool


class AbyssRankingService:
    def __init__(self, client, repository):
        self.client = client
        self.repository = repository
        self.cache = None
        self.last_error = None
        self._last_attempt = None
        self._task = None

    def load_cache(self):
        try:
            self.cache = self.repository.load()
        except Exception:
            logger.exception("어비스 랭킹 저장 기록 복원 실패")

    @property
    def stale(self):
        if self.cache is None:
            return True
        fetched = datetime.fromisoformat(self.cache["fetched_at"])
        return self.last_error is not None or (
            datetime.now(timezone.utc) - fetched
        ).total_seconds() >= REFRESH_SECONDS

    def request_refresh(self):
        if self._task is not None and not self._task.done():
            return self._task
        now = datetime.now(timezone.utc)
        if not self.stale:
            return None
        if self._last_attempt and (now - self._last_attempt).total_seconds() < RETRY_SECONDS:
            return None
        self._last_attempt = now
        self._task = asyncio.create_task(self._refresh_safely())
        return self._task

    async def _refresh_safely(self):
        try:
            await self.refresh()
            self.last_error = None
        except Exception as exc:
            self.last_error = exc
            logger.exception("어비스 랭킹 갱신 실패: 이전 정산 기록 유지")

    async def refresh(self):
        season = latest_season(await self.client.get(API_PATH + "/seasons"))
        # 한 번에 세 요청만 보내고 모두 검증된 뒤 캐시 전체를 교체합니다.
        semaphore = asyncio.Semaphore(3)

        async def fetch_group(server, klass):
            async with semaphore:
                payload = await self.client.get(API_PATH + "/table", params={
                    "season_start": season["season_start"], "server": server,
                    "klass": klass, "sort": "score",
                })
                return server, klass, parse_table(payload, season["season_start"], server, klass)

        # 요청 실패 시 남은 요청을 취소하여 불필요한 호출을 이어가지 않습니다.
        tasks = [asyncio.create_task(fetch_group(s, k)) for s in SERVERS for k in CLASSES]
        try:
            groups = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        dates = {table["snapshot_date"] for _, _, table in groups}
        if len(dates) != 1:
            raise ValueError("서버별 정산일이 달라 다음 갱신에 다시 집계합니다.")
        snapshot_date = dates.pop()
        if self.cache and snapshot_date < self.cache["snapshot_date"]:
            raise ValueError("이전 날짜로 되돌아간 응답을 거부했습니다.")
        entries, previous = [], {}
        for server, klass, table in groups:
            entries.extend(table["entries"])
            previous[f"{server}:{klass}"] = table["previous_snapshot_date"]
        if not entries:
            raise ValueError("시즌에 아직 정산된 캐릭터가 없습니다.")
        snapshot = {
            "version": 1, **season, "snapshot_date": snapshot_date,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "previous_dates": previous, "entries": entries,
        }
        self.repository.save(snapshot)
        self.cache = snapshot
        logger.info("어비스 랭킹 갱신 완료: %s 정산 / %s건", snapshot_date, len(entries))
        return snapshot

    async def ensure_cache(self):
        task = self.request_refresh()
        if self.cache is None and task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=10)
            except asyncio.TimeoutError:
                pass
        return self.cache

    def find(self, nickname, server=None):
        if self.cache is None:
            return []
        needle = normalized_name(nickname)
        return [entry for entry in self.cache["entries"]
                if normalized_name(entry["character_name"]) == needle
                and (server is None or entry["server"] == server)]

    def result(self, entry, snapshot=None):
        snapshot = snapshot or self.cache
        entries = snapshot["entries"]
        server_entries = [e for e in entries if e["server"] == entry["server"]]
        class_entries = [e for e in entries if e["klass"] == entry["klass"]]

        def rank(rows):
            # 종합 비교는 같은 점수를 공동순위로 계산합니다.
            return 1 + sum(e["score"] > entry["score"] for e in rows)

        return RankingResult(entry, snapshot, rank(entries), rank(server_entries),
                             rank(class_entries), len(entries), len(server_entries),
                             len(class_entries), self.stale or snapshot is not self.cache)

    async def close(self):
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
