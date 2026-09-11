# 룬 통계 지원 직업과 채용률·장신구 조합·콤보 운용 데이터 형식을 정의합니다.
from typing import TypedDict

RUNE_CLASSES = (
    "기사",
    "대검전사",
    "전사",
    "검술사",
    "궁수",
    "장궁병",
    "석궁사수",
    "마법사",
    "빙결술사",
    "화염술사",
    "전격술사",
    "힐러",
    "사제",
    "수도사",
    "암흑술사",
    "도적",
    "듀얼블레이드",
    "격투가",
    "음유시인",
    "악사",
    "댄서",
)


class RuneRate(TypedDict):
    name: str
    rate: str


class AccessoryCombo(TypedDict):
    combo: str
    upgrade: str
    engraving: str
    note: str


class Operation(TypedDict):
    situation: str
    combo: str
    note: str


class RuneStats(TypedDict):
    class_name: str
    accessory: dict[str, list[RuneRate]]
    defense: dict[str, list[RuneRate]]
    weapon: list[RuneRate]
    emblem: list[RuneRate]
    combos: list[AccessoryCombo]
    operations: list[Operation]
    basic_ratio: int | None
    data_date: str
