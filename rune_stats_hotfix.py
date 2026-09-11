import re

import rune_stats_bootstrap as rune_stats
import rune_stats_patch as patch


# =========================================================
# 최신 룬통계 페이지 보정
# - 최신 페이지는 '장신구' / '최신 채용률 10% 이상'이 별도 노드
# - '방어구' / '주요 채용 룬 20% 이상'도 별도 노드
# - 이름/퍼센트가 같은 노드든 별도 노드든 처리
# - 핵심 파싱이 무너지면 잘못된 결과로 정상 캐시를 덮지 않음
# =========================================================


def _clean(value):
    return patch._clean(value)


def _extract_name_percent_pairs(values, noise=None):
    noise = set(noise or ())
    tokens = []

    for raw in values:
        text = _clean(raw)
        if not text or text in noise:
            continue
        if text in {
            "NEW", "신규", "1픽", "2픽", "택1", "탭1", "한 자리",
            "20%+", "20% 이상",
        }:
            continue
        if re.fullmatch(r"\d+", text):
            continue
        if text.startswith("세트(") or text.startswith("세트 "):
            continue
        if "두 개 같이 착용" in text:
            continue
        tokens.append(text)

    # '구속 91.2%'처럼 한 노드에 들어오거나
    # '구속' / '91.2%'처럼 분리되어 들어와도 동일하게 처리한다.
    joined = " ".join(tokens)
    joined = re.sub(r"\bNEW\b", " ", joined)
    joined = re.sub(r"\b신규\b", " ", joined)
    joined = re.sub(r"\b(?:1픽|2픽|택1|탭1|한 자리)\b", " ", joined)
    joined = re.sub(r"\s+", " ", joined).strip()

    pairs = []
    cursor = 0
    pattern = re.compile(r"(\d+(?:\.\d+)?)%")

    for match in pattern.finditer(joined):
        name = _clean(joined[cursor:match.start()])
        name = re.sub(r"^\d+\s*", "", name)
        name = re.sub(r"^(?:NEW|신규)\s+", "", name)
        if "%" in name:
            name = _clean(name.rsplit("%", 1)[-1])
        if name:
            pairs.append({"name": name, "rate": f"{match.group(1)}%"})
        cursor = match.end()

    return pairs


def _is_accessory_start(text):
    text = _clean(text)
    return (
        text == "장신구"
        or text == "장신구 채용률"
        or "장신구 최신 채용률" in text
        or "최신 채용률" in text
    )


def _is_defense_start(text):
    text = _clean(text)
    return (
        text == "방어구"
        or "방어구 주요 채용 룬" in text
        or "주요 채용 룬" in text
        or "방어구 계열별" in text
    )


def _parse_accessory(values):
    section = patch._slice_between(
        values,
        _is_accessory_start,
        [
            _is_defense_start,
            lambda x: _clean(x) == "무기",
        ],
    )

    raw_groups = {"80": [], "40": [], "10": []}
    current = None

    for raw in section:
        text = _clean(raw)
        if not text:
            continue

        compact = text.replace(" ", "")

        if re.match(r"^80%\s*이상", text) or compact.startswith("80%이상"):
            current = "80"
            remainder = re.sub(r"^80%\s*이상\s*", "", text)
            if remainder:
                raw_groups[current].append(remainder)
            continue

        if re.match(r"^40\s*[~～-]\s*79%", text):
            current = "40"
            remainder = re.sub(r"^40\s*[~～-]\s*79%\s*", "", text)
            if remainder:
                raw_groups[current].append(remainder)
            continue

        if re.match(r"^10\s*[~～-]\s*39%", text):
            current = "10"
            remainder = re.sub(r"^10\s*[~～-]\s*39%\s*", "", text)
            if remainder:
                raw_groups[current].append(remainder)
            continue

        # 섹션 설명 문구는 데이터가 아니다.
        if "최신 채용률" in text or text == "10% 이상":
            continue

        if current:
            raw_groups[current].append(text)

    return {
        key: _extract_name_percent_pairs(values)
        for key, values in raw_groups.items()
    }


def _parse_defense(values):
    section = patch._slice_between(
        values,
        _is_defense_start,
        lambda x: _clean(x) == "무기",
    )

    labels = ("각성", "용문장", "침식", "그 외")
    result = {label: [] for label in labels}
    current = None
    bucket = []

    noise = {
        "택1", "탭1", "한 자리", "20%+", "20% 이상",
        "주요 채용 룬 20% 이상", "주요 채용 룬",
        "NEW", "신규",
    }

    def flush():
        nonlocal bucket
        if current is not None:
            result[current] = _extract_name_percent_pairs(bucket, noise=noise)
        bucket = []

    for raw in section:
        text = _clean(raw)
        if not text or text in noise:
            continue

        detected = None
        for label in labels:
            if text == label or text.startswith(label):
                detected = label
                break

        if detected:
            flush()
            current = detected
            remainder = _clean(text[len(detected):])
            remainder = re.sub(r"^(?:택1|탭1|한 자리)\s*", "", remainder)
            remainder = re.sub(r"^20%\+?\s*", "", remainder)
            if (
                remainder
                and remainder not in noise
                and not remainder.startswith("세트(")
                and "두 개 같이 착용" not in remainder
            ):
                bucket.append(remainder)
            continue

        if current:
            if text.startswith("세트(") or text.startswith("세트 "):
                continue
            if "두 개 같이 착용" in text:
                continue
            bucket.append(text)

    flush()
    return result


# rune_stats_patch의 최신 페이지 파서가 아래 보정 함수를 사용하게 한다.
patch._extract_name_percent_pairs = _extract_name_percent_pairs
patch._parse_accessory = _parse_accessory
patch._parse_defense = _parse_defense

_original_latest_parser = patch._parse_page_latest


def _parse_page_validated(html):
    parsed = _original_latest_parser(html)

    accessory_ok = 0
    defense_ok = 0

    for data in parsed.values():
        accessory = data.get("accessory") or {}
        defense = data.get("defense") or {}

        if any(accessory.get(key) for key in ("80", "40", "10")):
            accessory_ok += 1
        if any(defense.get(key) for key in ("각성", "용문장", "침식", "그 외")):
            defense_ok += 1

    # 구조 변경 등으로 핵심 정보가 대부분 사라진 결과는 저장하지 않는다.
    # refresh_once()가 예외를 잡고 기존 정상 캐시를 그대로 유지한다.
    if accessory_ok < 15 or defense_ok < 15:
        raise RuntimeError(
            "룬통계 핵심 파싱 검증 실패 "
            f"(장신구 {accessory_ok}/21, 방어구 {defense_ok}/21)"
        )

    return parsed


rune_stats._parse_page = _parse_page_validated
