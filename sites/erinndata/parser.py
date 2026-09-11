# 룬 채용률·장신구 조합·콤보 운용 표를 파싱하고 핵심 데이터 누락을 검증합니다.
import re

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from .models import RUNE_CLASSES, RuneStats


def _clean(value):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = text.replace("▲", "").replace("▼", "")
    text = text.replace("＋", "+")
    return re.sub(r"\s+", " ", text).strip(" ·")


def _class_leaf_strings(heading):
    """현재 h3부터 다음 직업 h3 전까지 실제 텍스트 노드만 수집한다."""
    values = []
    for node in heading.next_elements:
        if isinstance(node, Tag) and node.name == "h3" and node is not heading:
            break
        if isinstance(node, NavigableString):
            text = _clean(node)
            if text:
                values.append(text)
    return values


def _first_index(values, predicates, start=0):
    if not isinstance(predicates, (list, tuple)):
        predicates = [predicates]
    for i in range(start, len(values)):
        for predicate in predicates:
            if predicate(values[i]):
                return i
    return None


def _slice_between(values, start_pred, end_preds):
    start = _first_index(values, start_pred)
    if start is None:
        return []
    end = _first_index(values, end_preds, start + 1)
    if end is None:
        end = len(values)
    return values[start + 1 : end]


def _parse_ranked(values, start_label, end_labels):
    section = _slice_between(
        values,
        lambda x: x == start_label,
        [lambda x, label=label: x == label for label in end_labels],
    )

    pairs = _extract_name_percent_pairs(section)
    return pairs[:3]


def _extract_data_date(page_text):
    for pattern in (
        r"최신\s*채용률\s*\((\d{4}[.-]\d{2}[.-]\d{2})\)",
        r"(\d{4}[.-]\d{2}[.-]\d{2})\s*기준",
        r"데이터\s*기준\s*(\d{4}[.-]\d{2}[.-]\d{2})",
    ):
        match = re.search(pattern, page_text)
        if match:
            return match.group(1).replace(".", "-")
    return ""


def _parse_combo_table(heading):
    for table in heading.find_all_next("table"):
        # 다음 직업 h3를 넘어간 테이블이면 중단
        previous_h3 = table.find_previous("h3")
        if previous_h3 is not heading:
            break

        headers = [_clean(th.get_text(" ", strip=True)) for th in table.find_all("th")]
        if not any("장신구 조합" in header for header in headers):
            continue

        rows = []
        for tr in table.find_all("tr"):
            cells = [_clean(td.get_text(" ", strip=True)) for td in tr.find_all("td")]
            if len(cells) < 3:
                continue
            rows.append(
                {
                    "combo": cells[0],
                    "upgrade": cells[1],
                    "engraving": cells[2],
                    "note": cells[3] if len(cells) > 3 else "",
                }
            )
        return rows[:5]
    return []


def _extract_name_percent_pairs(values, noise=None):
    noise = set(noise or ())
    tokens = []

    for raw in values:
        text = _clean(raw)
        if not text or text in noise:
            continue
        if text in {
            "NEW",
            "신규",
            "1픽",
            "2픽",
            "택1",
            "탭1",
            "한 자리",
            "20%+",
            "20% 이상",
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
        name = _clean(joined[cursor : match.start()])
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
    section = _slice_between(
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
        key: _extract_name_percent_pairs(values) for key, values in raw_groups.items()
    }


def _parse_defense(values):
    section = _slice_between(
        values,
        _is_defense_start,
        lambda x: _clean(x) == "무기",
    )

    labels = ("각성", "용문장", "침식", "그 외")
    result = {label: [] for label in labels}
    current = None
    bucket = []

    noise = {
        "택1",
        "탭1",
        "한 자리",
        "20%+",
        "20% 이상",
        "주요 채용 룬 20% 이상",
        "주요 채용 룬",
        "NEW",
        "신규",
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
            remainder = _clean(text[len(detected) :])
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


def _clean_operation(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _same_class_table(heading, table):
    """해당 table이 현재 직업 h3와 다음 직업 h3 사이에 있는지 확인한다."""
    return table.find_previous("h3") is heading


def _parse_operation_table(heading):
    for table in heading.find_all_next("table"):
        if not _same_class_table(heading, table):
            break

        headers = [
            _clean_operation(th.get_text(" ", strip=True))
            for th in table.find_all("th")
        ]
        header_text = " | ".join(headers)

        # 최신 페이지의 '콤보 · 운용 제보' 표 식별
        if not (
            ("콤보" in header_text and "운용" in header_text)
            or (
                "기준" in header_text
                and "상황" in header_text
                and "비고" in header_text
            )
        ):
            continue

        rows = []
        for tr in table.find_all("tr"):
            cells = [
                _clean_operation(td.get_text(" ", strip=True))
                for td in tr.find_all("td")
            ]
            if not cells:
                continue

            # 보통: # / 기준·상황 / 콤보·운용법 / 비고
            if len(cells) >= 4:
                _, situation, combo, note = cells[:4]
            elif len(cells) == 3:
                situation, combo, note = cells
            elif len(cells) == 2:
                situation, combo = cells
                note = ""
            else:
                continue

            if not situation and not combo and not note:
                continue

            rows.append(
                {
                    "situation": situation or "기본",
                    "combo": combo,
                    "note": note,
                }
            )

        if rows:
            return rows

    return []


def parse_page(html: str) -> dict[str, RuneStats]:
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(" ", strip=True)
    data_date = _extract_data_date(page_text)

    parsed = {}
    for heading in soup.find_all("h3"):
        class_name = _clean(heading.get_text(" ", strip=True))
        if class_name not in RUNE_CLASSES:
            continue

        values = _class_leaf_strings(heading)
        accessory = _parse_accessory(values)
        defense = _parse_defense(values)
        weapon = _parse_ranked(values, "무기", ("엠블럼",))
        emblem = _parse_ranked(
            values,
            "엠블럼",
            ("실전 세팅", "장신구 조합", "콤보 · 운용 제보", "제보의 영역"),
        )
        combos = _parse_combo_table(heading)

        parsed[class_name] = {
            "class_name": class_name,
            "accessory": accessory,
            "defense": defense,
            "weapon": weapon,
            "emblem": emblem,
            "combos": combos,
            "operations": _parse_operation_table(heading),
            "basic_ratio": None,
            "data_date": data_date,
        }

    if len(parsed) < 15:
        raise RuntimeError(f"직업 데이터 파싱 수가 너무 적습니다. ({len(parsed)}개)")

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
    # 갱신 호출자가 예외를 처리하며 기존 정상 캐시를 유지한다.
    if accessory_ok < 15 or defense_ok < 15:
        raise RuntimeError(
            "룬통계 핵심 파싱 검증 실패 "
            f"(장신구 {accessory_ok}/21, 방어구 {defense_ok}/21)"
        )

    return parsed
