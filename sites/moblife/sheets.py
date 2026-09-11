# 악보 API를 조회하고 여러 응답 형식에서 제목·제작자·메타데이터를 추출합니다.
from .models import SheetItem


def _first_nonempty(mapping, *keys):
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return None


def _text_value(value):
    if value is None:
        return None

    if isinstance(value, (str, int, float)):
        text = str(value).strip()
        return text or None

    if isinstance(value, dict):
        nested = _first_nonempty(
            value,
            "name",
            "nickname",
            "title",
            "display_name",
            "label",
            "value",
        )
        if nested is not None:
            return _text_value(nested)

    return None


def extract_sheet_items(payload):
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]

    if not isinstance(payload, dict):
        return []

    for key in ("items", "results", "rows", "list"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]

    for key in ("data", "payload", "result"):
        value = payload.get(key)

        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]

        if isinstance(value, dict):
            for subkey in ("items", "results", "rows", "list", "data"):
                subvalue = value.get(subkey)
                if isinstance(subvalue, list):
                    return [x for x in subvalue if isinstance(x, dict)]

    return []


def get_sheet_title(item):
    value = _first_nonempty(
        item,
        "title",
        "name",
        "song_title",
        "sheet_title",
        "music_title",
    )
    return _text_value(value) or "제목 없음"


def get_sheet_creator(item):
    value = _first_nonempty(
        item,
        "creator",
        "author",
        "uploader",
        "created_by",
        "writer",
        "nickname",
        "user_name",
    )
    return _text_value(value)


def get_sheet_source_url(item):
    value = _first_nonempty(
        item,
        "source_url",
        "original_url",
        "post_url",
        "url",
        "link",
    )
    text = _text_value(value)
    if text and (text.startswith("http://") or text.startswith("https://")):
        return text
    return None


def get_sheet_meta(item):
    """디스코드에 보여줄 만한 정보만 깔끔하게 추립니다."""
    parts = []

    creator = get_sheet_creator(item)
    if creator:
        creator_clean = creator.strip()
        # API/원본 데이터에서 의미 없는 값은 화면에 노출하지 않음
        if creator_clean.lower() not in {
            "unknown",
            "none",
            "null",
            "-",
        } and creator_clean not in {"광고", "알 수 없음"}:
            parts.append(("👤", creator_clean))

    play_type = _text_value(
        _first_nonempty(item, "play_type", "type", "performance_type")
    )
    if play_type:
        play_map = {
            "solo": "솔로",
            "ensemble": "합주",
            "both": "솔로/합주",
            "unknown": None,
        }
        label = play_map.get(play_type.lower(), play_type)
        if label and label.lower() not in {"unknown", "none", "null", "-"}:
            parts.append(("🎵", label))

    harmony = _first_nonempty(item, "harmony_count", "part_count")
    if harmony not in (None, ""):
        try:
            harmony_num = int(harmony)
            if harmony_num > 0:
                parts.append(("🎹", f"{harmony_num}파트"))
        except (TypeError, ValueError):
            harmony_text = str(harmony).strip()
            if harmony_text and harmony_text.lower() not in {
                "unknown",
                "none",
                "null",
                "-",
            }:
                parts.append(("🎹", f"{harmony_text}파트"))

    return parts


class SheetService:
    def __init__(self, client):
        self.client = client

    async def fetch_sheet_music(self, search_text, page_size=30) -> list[SheetItem]:
        payload = await self.client.get(
            "/d/api/v1/sheet-music",
            params={"q": search_text, "page": 1, "page_size": page_size},
        )
        return extract_sheet_items(payload)
