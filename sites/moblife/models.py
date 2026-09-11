# 모비라이프 조회 결과의 데이터 형식을 정의합니다.
from typing import TypedDict


class AbyssRecord(TypedDict, total=False):
    start_datetime: str
    is_post_maintenance_estimate: bool


class MarketItem(TypedDict, total=False):
    kind_id: int
    name: str
    min_price: int
    total_count: int
    is_sold_out: bool
    parent_category: str
    pct_change_1h: float
    pct_change_24h: float
    pct_change_7d: float
    icon_url: str


class SheetItem(TypedDict, total=False):
    title: str
    creator: str
    source_url: str
    play_type: str
    harmony_count: int


class MaintenanceStatus(TypedDict, total=False):
    is_maintenance: bool
    last_maintenance_end_time: str
    current_maintenance_start_time: str
