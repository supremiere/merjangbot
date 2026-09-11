import re

from bs4 import BeautifulSoup

import rune_stats_bootstrap as rune_stats


# =========================================================
# 룬통계 DOM 파서 보정
# - 페이지 전체 텍스트를 평탄화하면 서로 다른 룬 칩이 붙는 문제가 있어
#   직업 카드 안의 개별 HTML 요소(stripped_strings)를 우선 사용한다.
# - 기존 파서는 날짜/조합표/무기/엠블럼용 fallback으로 그대로 둔다.
# =========================================================

_original_parse_page = rune_stats._parse_page


def _clean(value):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = text.replace("▲", "").replace("＋", "")
    text = text.replace("1픽 교체", "")
    return re.sub(r"\s+", " ", text).strip(" ·")


def _strings(tag):
    values = []
    for raw in tag.stripped_strings:
        text = _clean(raw)
        if text:
            values.append(text)
    return values


def _section_tags(heading):
    tags = []
    for tag in heading.find_all_next():
        if tag is heading:
            continue
        if getattr(tag, "name", None) == "h3":
            break
        if getattr(tag, "name", None):
            tags.append(tag)
    return tags


def _strip_noise(values):
    result = []
    skip_exact = {
        "", "확정", "유력", "분기", "주류 픽", "동률", "택1",
        "제보의 영역", "시청자 제보", "장신구 조합", "개조", "세공", "비고",
    }
    for value in values:
        text = _clean(value)
        if not text or text in skip_exact:
            continue
        if text in {"택1 · 한 자리", "세트 2자리"}:
            continue
        if text.startswith("룬 보유 공백 지표"):
            continue
        if text not in result:
            result.append(text)
    return result


def _best_row(tags, matcher):
    """조건을 만족하는 태그 중 실제 행에 가장 가까운 작은 태그를 고른다."""
    candidates = []
    for tag in tags:
        values = _strings(tag)
        if not values:
            continue
        joined = " ".join(values)
        if not matcher(values, joined):
            continue
        # 라벨만 있는 작은 span은 제외. 실제 값이 같이 들어있는 행을 찾는다.
        if len(values) == 1 and len(joined) <= 8:
            continue
        candidates.append((len(joined), len(values), tag, values))

    if not candidates:
        return []

    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][3]


def _extract_prefixed_values(values, prefix_pattern):
    if not values:
        return []

    result = []
    first = values[0]
    match = re.match(prefix_pattern, first)
    if match:
        remainder = _clean(first[match.end():])
        if remainder:
            result.append(remainder)
        result.extend(values[1:])
    else:
        joined = " ".join(values)
        match = re.match(prefix_pattern, joined)
        if match:
            remainder = _clean(joined[match.end():])
            if remainder:
                result.append(remainder)

    result = _strip_noise(result)

    # UI 설명 조각이 룬 목록에 섞이지 않도록 제거한다.
    cleaned = []
    for text in result:
        if re.match(r"^(?:60|30|10)(?:~\d+)?%", text):
            continue
        if text in {"확정권", "주류", "대안"}:
            continue
        if text not in cleaned:
            cleaned.append(text)
    return cleaned


def _parse_accessory_dom(tags):
    groups = {"60": [], "30": [], "10": []}
    patterns = {
        "60": r"^60%\s*↑?\s*",
        "30": r"^(?:30(?:~59)?%|30%)\s*",
        "10": r"^(?:10(?:~29)?%|10%)\s*",
    }

    for key, pattern in patterns.items():
        row = _best_row(
            tags,
            lambda values, joined, p=pattern: bool(
                re.match(p, values[0]) or re.match(p, joined)
            ),
        )
        groups[key] = _extract_prefixed_values(row, pattern)

    return groups


def _parse_defense_row(tags, label):
    row = _best_row(
        tags,
        lambda values, joined, target=label: (
            values[0] == target
            or values[0].startswith(target)
            or joined.startswith(target)
        ),
    )
    if not row:
        return []

    values = list(row)
    first = values[0]
    if first == label:
        values = values[1:]
    elif first.startswith(label):
        remainder = _clean(first[len(label):])
        values = ([remainder] if remainder else []) + values[1:]

    cleaned = []
    for text in _strip_noise(values):
        text = re.sub(r"^(택1\s*·\s*한 자리|세트\s*2자리)\s*", "", text).strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _parse_defense_dom(tags):
    return {
        "각성": _parse_defense_row(tags, "각성"),
        "용문장": _parse_defense_row(tags, "용문장"),
        "침식": _parse_defense_row(tags, "침식"),
        "그 외": _parse_defense_row(tags, "그 외"),
    }


def _has_useful_values(mapping):
    return any(mapping.get(key) for key in mapping)


def _parse_page_dom_safe(html):
    parsed = _original_parse_page(html)
    soup = BeautifulSoup(html, "html.parser")

    headings = {}
    for h3 in soup.find_all("h3"):
        name = _clean(h3.get_text(" ", strip=True))
        if name in rune_stats.RUNE_CLASSES:
            headings[name] = h3

    for class_name, heading in headings.items():
        if class_name not in parsed:
            continue

        tags = _section_tags(heading)
        accessory = _parse_accessory_dom(tags)
        defense = _parse_defense_dom(tags)

        # DOM에서 정상적으로 분리된 경우에만 기존 값을 덮어쓴다.
        if _has_useful_values(accessory):
            parsed[class_name]["accessory"] = accessory
        if _has_useful_values(defense):
            parsed[class_name]["defense"] = defense

    return parsed


rune_stats._parse_page = _parse_page_dom_safe
