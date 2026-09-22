# 모비라이프 없이 제보된 첫 어구를 기준으로 36시간 15분 주기를 계산합니다.
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
ABYSS_CYCLE = timedelta(hours=36, minutes=15)
ABYSS_ACTIVE_DURATION = timedelta(minutes=15)
ABYSS_ALERT_MINUTES = (60, 30, 10, 1)

# 독립 전환 시점의 검증된 첫 어구. DB 제보가 하나라도 있으면 DB 값이 우선합니다.
INITIAL_ANCHOR_KST = datetime(2026, 9, 22, 14, 28, tzinfo=KST)


def parse_iso_datetime(value):
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class LocalAbyssService:
    def __init__(self, repository):
        self.repository = repository
        # DB 초기화 전에도 객체를 만들 수 있게 기본 기준점만 메모리에 둔다.
        self.anchor = INITIAL_ANCHOR_KST.astimezone(timezone.utc)
        self.last_maintenance = None

    async def refresh(self):
        observation = self.repository.latest_observation()
        if observation:
            self.anchor = parse_iso_datetime(observation["spawn_time"])
        else:
            self.anchor = INITIAL_ANCHOR_KST.astimezone(timezone.utc)
        self.last_maintenance = self.repository.latest_maintenance()

    def note_maintenance(self, start_time=None, end_time=None, source_url=None):
        start = parse_iso_datetime(start_time)
        end = parse_iso_datetime(end_time)
        if end is None:
            return
        self.repository.save_maintenance(
            start.isoformat() if start else None,
            end.isoformat(),
            source_url,
        )
        self.last_maintenance = self.repository.latest_maintenance()

    @property
    def needs_report(self):
        if self.anchor is None or not self.last_maintenance:
            return self.anchor is None
        end = parse_iso_datetime(self.last_maintenance.get("end_time"))
        return end is not None and self.anchor <= end

    def report_spawn(self, spawn_time, reported_by):
        spawn = spawn_time.astimezone(timezone.utc)
        maintenance = self.repository.latest_maintenance()
        maintenance_start = maintenance.get("start_time") if maintenance else None
        maintenance_end = maintenance.get("end_time") if maintenance else None

        end = parse_iso_datetime(maintenance_end)
        if end is not None and spawn <= end:
            raise ValueError("첫 어구 시각은 최근 점검 종료시각보다 뒤여야 합니다.")

        self.repository.save_observation(
            spawn_time=spawn.isoformat(),
            maintenance_start=maintenance_start,
            maintenance_end=maintenance_end,
            reported_by=reported_by,
            source="slash_report",
        )
        self.anchor = spawn
        return self.repository.latest_observation()

    def get_next_abyss_spawn(self, now_utc):
        if self.anchor is None or self.needs_report:
            return None, False

        now_utc = now_utc.astimezone(timezone.utc)
        spawn = self.anchor
        if spawn > now_utc:
            return spawn, False

        elapsed = now_utc - spawn
        cycles = int(elapsed.total_seconds() // ABYSS_CYCLE.total_seconds())
        current = spawn + ABYSS_CYCLE * cycles

        if current <= now_utc < current + ABYSS_ACTIVE_DURATION:
            return current + ABYSS_CYCLE, False

        if current <= now_utc:
            return current + ABYSS_CYCLE, False

        return current, False

    def get_abyss_status(self, now_utc):
        if self.anchor is None or self.needs_report:
            return None, False, False

        now_utc = now_utc.astimezone(timezone.utc)
        if self.anchor > now_utc:
            return self.anchor, False, False

        elapsed = now_utc - self.anchor
        cycles = int(elapsed.total_seconds() // ABYSS_CYCLE.total_seconds())
        current = self.anchor + ABYSS_CYCLE * cycles

        if current <= now_utc < current + ABYSS_ACTIVE_DURATION:
            return current, True, False

        return current + ABYSS_CYCLE, False, False

    def due_alerts(self, now_utc):
        if self.needs_report:
            return []

        spawn, estimated = self.get_next_abyss_spawn(now_utc)
        if spawn is None:
            return []

        remaining = (spawn - now_utc.astimezone(timezone.utc)).total_seconds()
        return [
            (spawn, minutes, estimated)
            for minutes in ABYSS_ALERT_MINUTES
            if minutes * 60 - 59 <= remaining <= minutes * 60
        ]
