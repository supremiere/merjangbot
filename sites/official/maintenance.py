# 공식 점검 공지를 읽어 실제 게임 점검 시작/완료 상태와 시각을 계산합니다.
import asyncio
import re
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup

from .notices import NOTICE_URL, OfficialNotices

KST = timezone(timedelta(hours=9))
MAX_CANDIDATES = 4
ACTIVE_MAX_AGE = timedelta(days=2)

# '(완료)'라는 말만 보지 않는다. 반드시 게임 점검 공지인지 먼저 확인한다.
MAINTENANCE_TITLE_RE = re.compile(r"(?:정기|임시|긴급)?\s*점검\s*안내")
EXCLUDED_TITLE_RE = re.compile(r"(공식\s*홈페이지|웹|결제).*점검", re.IGNORECASE)

# 실제 게임 점검 공지 본문에 반복해서 사용되는 표지.
GAME_MAINTENANCE_BODY_RE = re.compile(
    r"(?:게임\s*점검\s*안내|정기점검\s*진행\s*안내|임시점검\s*진행\s*안내|대상\s*:?\s*전체\s*서버)"
)

# 완료 표시는 제목의 '(완료)'가 아니라 본문의 '게임 이용 가능' 문구로 판정한다.
COMPLETION_RE = re.compile(
    r"(?:현재\s*)?점검이\s*완료되어.{0,30}?게임(?:을)?\s*이용하실\s*수\s*있습니다",
    re.DOTALL,
)

UPDATE_STAMP_RE = re.compile(
    r"\((?:(20\d{2})\s*/\s*)?"
    r"(\d{1,2})\s*/\s*(\d{1,2})\s*\([^)]*\)\s*"
    r"(\d{1,2})\s*:\s*(\d{2})\s*업데이트\)"
)

BODY_DATE_RE = re.compile(
    r"적용\s*날짜\s*:?\s*(20\d{2})\s*년\s*"
    r"(\d{1,2})\s*월\s*(\d{1,2})\s*일"
)
TITLE_DATE_RE = re.compile(r"(\d{1,2})\s*/\s*(\d{1,2})")
TITLE_RANGE_RE = re.compile(
    r"\((\d{1,2})\s*:\s*(\d{2})\s*~\s*"
    r"(\d{1,2})\s*:\s*(\d{2})\)"
)
COMPLETED_TITLE_RE = re.compile(r"\(완료\)")


def _text_from_html(html):
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


def _candidate_title(title):
    title = title or ""
    return bool(MAINTENANCE_TITLE_RE.search(title)) and not EXCLUDED_TITLE_RE.search(title)


def _year_for_month(month, now_kst):
    year = now_kst.year
    # 연말에 다음 해 1월 점검 공지가 미리 올라오는 경우를 보정한다.
    if now_kst.month == 12 and month == 1:
        year += 1
    elif now_kst.month == 1 and month == 12:
        year -= 1
    return year


def _parse_start(title, text, now_kst):
    body_date = BODY_DATE_RE.search(text)
    title_date = TITLE_DATE_RE.search(title or "")
    time_range = TITLE_RANGE_RE.search(title or "")
    if time_range is None:
        return None

    if body_date:
        year, month, day = map(int, body_date.groups())
    elif title_date:
        month, day = map(int, title_date.groups())
        year = _year_for_month(month, now_kst)
    else:
        return None

    hour, minute = map(int, time_range.groups()[:2])
    try:
        return datetime(year, month, day, hour, minute, tzinfo=KST)
    except ValueError:
        return None


def _parse_title_completion(title, maintenance_start):
    """검증된 게임 점검 공지의 '(완료)' 제목에 적힌 종료 시각을 읽는다."""
    if not COMPLETED_TITLE_RE.search(title or ""):
        return None

    time_range = TITLE_RANGE_RE.search(title or "")
    if time_range is None or maintenance_start is None:
        return None

    end_hour, end_minute = map(int, time_range.groups()[2:])
    try:
        completed = maintenance_start.replace(hour=end_hour, minute=end_minute)
    except ValueError:
        return None

    # 자정을 넘기는 점검도 처리한다.
    if completed < maintenance_start:
        completed += timedelta(days=1)
    return completed


def _parse_completion(text, maintenance_start):
    matches = list(COMPLETION_RE.finditer(text))
    if not matches:
        return None

    # 여러 업데이트 문구가 있을 수 있으므로 완료 문구 바로 앞의 업데이트 시각을 찾는다.
    for completion in matches:
        before = text[max(0, completion.start() - 240):completion.start()]
        stamps = list(UPDATE_STAMP_RE.finditer(before))
        if not stamps:
            continue
        year_text, month, day, hour, minute = stamps[-1].groups()
        year = int(year_text) if year_text else (
            maintenance_start.year if maintenance_start else datetime.now(KST).year
        )
        try:
            return datetime(
                year, int(month), int(day), int(hour), int(minute), tzinfo=KST
            )
        except ValueError:
            continue

    return None


def parse_maintenance_notice(title, html, now_utc=None):
    if not _candidate_title(title):
        return None

    now_kst = (now_utc or datetime.now(timezone.utc)).astimezone(KST)
    text = _text_from_html(html)

    # 제목에 '점검'이 있어도 홈페이지/결제 등 게임 서버 점검이 아니면 제외한다.
    if not GAME_MAINTENANCE_BODY_RE.search(text):
        return None

    start = _parse_start(title, text, now_kst)
    if start is None:
        return None

    # 1순위: 실제 완료 공지 제목의 종료 시각.
    # 넥슨은 조기/연장 종료 시 '(완료) ... (시작 ~ 실제 종료)' 형태로 제목을 갱신한다.
    # 단, 위에서 이미 '게임 점검 공지 + 전체 서버'를 검증했으므로 다른 '(완료)' 글과 혼동하지 않는다.
    completed_at = _parse_title_completion(title, start)

    # 2순위: 본문 완료 문구 + 업데이트 시각.
    if completed_at is None:
        completed_at = _parse_completion(text, start)
    return {
        "title": title,
        "start": start,
        "completed_at": completed_at,
    }


class OfficialMaintenanceService:
    """모비라이프 없이 공식 점검 공지만으로 게임 서버 상태를 계산한다."""

    def __init__(self, http):
        self.http = http
        self.notices = OfficialNotices(http)
        self._lock = asyncio.Lock()

    async def _fetch_notice(self, post, now_utc):
        html = await self.http.get_text(
            post["url"], headers={"User-Agent": "Mozilla/5.0"}
        )
        parsed = parse_maintenance_notice(post["title"], html, now_utc)
        if parsed is not None:
            parsed["url"] = post["url"]
        return parsed

    async def fetch(self):
        async with self._lock:
            now_utc = datetime.now(timezone.utc)
            now_kst = now_utc.astimezone(KST)
            posts = await self.notices.get_posts(NOTICE_URL, "공지")
            candidates = [post for post in posts if _candidate_title(post["title"])][
                :MAX_CANDIDATES
            ]

            observations = []
            for post in candidates:
                try:
                    item = await self._fetch_notice(post, now_utc)
                except Exception:
                    # 한 공지의 일시적 파싱/통신 오류 때문에 전체 상태 판정을 버리지 않는다.
                    continue
                if item is not None:
                    observations.append(item)

            if not observations:
                raise RuntimeError("공식 점검 공지에서 서버 상태를 판정하지 못했습니다.")

            active = [
                item
                for item in observations
                if item["start"] <= now_kst
                and item["completed_at"] is None
                and now_kst - item["start"] <= ACTIVE_MAX_AGE
            ]
            active_notice = max(active, key=lambda item: item["start"]) if active else None

            completed_items = [
                item for item in observations if item["completed_at"] is not None
            ]
            last_completed = (
                max(completed_items, key=lambda item: item["completed_at"])
                if completed_items
                else None
            )
            last_end = last_completed["completed_at"] if last_completed else None

            return {
                "is_maintenance": active_notice is not None,
                "last_maintenance_start_time": (
                    last_completed["start"].astimezone(timezone.utc).isoformat()
                    if last_completed
                    else None
                ),
                "last_maintenance_end_time": (
                    last_end.astimezone(timezone.utc).isoformat() if last_end else None
                ),
                "last_maintenance_url": (
                    last_completed.get("url") if last_completed else None
                ),
                "current_maintenance_start_time": (
                    active_notice["start"].astimezone(timezone.utc).isoformat()
                    if active_notice
                    else None
                ),
            }
