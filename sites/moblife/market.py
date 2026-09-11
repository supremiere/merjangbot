# 시세 API를 조회하고 아이템 이름 검색·중복 제거·정렬을 수행합니다.
import re

from sites.http import HttpError

from .models import MarketItem


def normalize_item_name(value):
    return re.sub(r"\s+", "", value or "").lower()


def filter_market_items(items, query):
    """검색어가 이름에 들어가는 모든 아이템을 반환한다."""
    q = normalize_item_name(query)

    matched = [item for item in items if q in normalize_item_name(item.get("name"))]

    # 같은 아이템이 중복으로 들어오는 경우 kind_id 기준 제거
    unique = []
    seen = set()

    for item in matched:
        key = item.get("kind_id") or (
            normalize_item_name(item.get("name")),
            item.get("parent_category"),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    # 이름순으로 보기 좋게 정렬
    unique.sort(key=lambda x: normalize_item_name(x.get("name")))
    return unique


class MarketService:
    def __init__(self, client):
        self.client = client

    async def fetch_market_prices(
        self, search_text, limit=100
    ) -> tuple[list[MarketItem], str | None]:
        try:
            data = await self.client.get_openapi(
                "/market/prices",
                params={
                    "search": search_text,
                    "sort": "pct_change_24h_desc",
                    "limit": limit,
                    "offset": 0,
                },
            )
        except HttpError as error:
            if error.status in (401, 403):
                raise RuntimeError("MOBLIFE_API_KEY_INVALID") from error
            if error.status == 429:
                raise RuntimeError("MOBLIFE_RATE_LIMIT") from error
            raise
        return data.get("data") or [], data.get("last_updated_at")
