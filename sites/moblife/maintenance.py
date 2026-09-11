# 점검 상태 API의 응답을 검증하고 시각·경과 시간을 계산합니다.
from datetime import datetime, timedelta, timezone

from .models import MaintenanceStatus

KST = timezone(timedelta(hours=9))


def parse_iso_datetime(value):
    if not value:
        return None

    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_elapsed(delta):
    total_minutes = max(0, int(delta.total_seconds() // 60))
    days, remainder = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)

    parts = []
    if days:
        parts.append(f"{days}일")
    if hours or days:
        parts.append(f"{hours}시간")
    parts.append(f"{minutes}분")
    return " ".join(parts)


def format_datetime(value):
    if not value:
        return "확인 불가"

    try:
        dt = parse_iso_datetime(value)
        return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    except Exception:
        return str(value)


class MaintenanceService:
    def __init__(self, client):
        self.client = client

    async def fetch(self) -> MaintenanceStatus:
        data = await self.client.get("/d/api/v1/maintenance-status")
        if not isinstance(data, dict) or not isinstance(
            data.get("is_maintenance"), bool
        ):
            raise RuntimeError("모비라이프 점검상태 응답 형식이 올바르지 않습니다.")
        return data
