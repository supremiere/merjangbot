import re

import rune_stats_patch as patch


def _clean(value):
    return patch._clean(value)


def _extract_name_percent_pairs(values, noise=None):
    """룬명/채용률이 같은 노드든 별도 노드든 모두 처리한다."""
    noise = set(noise or ())
    tokens = []

    for raw in values:
        text = _clean(raw)
        if not text or text in noise:
            continue
        if text in {"NEW", "신규", "1픽", "2픽", "택1", "한 자리", "20%+"}:
            continue
        if re.fullmatch(r"\d+", text):
            continue
        if text.startswith("세트(") or text.startswith("세트 "):
            continue
        tokens.append(text)

    joined = " ".join(tokens)
    joined = re.sub(r"\bNEW\s+", "", joined)
    joined = re.sub(r"\b(?:1픽|2픽|택1|한 자리)\b", " ", joined)
    joined = re.sub(r"\s+", " ", joined).strip()

    pairs = []
    for match in re.finditer(r"(.+?)\s+(\d+(?:\.\d+)?)%(?=\s|$)", joined):
        name = _clean(match.group(1))
        # 직전 매치 뒤에 남은 숫자/순위/설명 조각 제거
        name = re.sub(r"^\d+\s*", "", name)
        name = re.sub(r"^(?:NEW|신규)\s+", "", name)
        # 앞선 '이름 00.0%'가 비정상적으로 포함된 경우 마지막 % 뒤만 사용
        if "%" in name:
            name = _clean(name.rsplit("%", 1)[-1])
        if name:
            pairs.append({"name": name, "rate": f"{match.group(2)}%"})

    if pairs:
        return pairs

    # HTML이 이름/퍼센트를 완전히 분리한 경우의 보조 처리
    pending = []
    for text in tokens:
        if re.fullmatch(r"\d+(?:\.\d+)?%", text):
            if pending:
                name = _clean(" ".join(pending))
                if name:
                    pairs.append({"name": name, "rate": text})
                pending = []
        else:
            pending.append(text)
    return pairs


def _parse_accessory(values):
    section = patch._slice_between(
        values,
        lambda x: "장신구 최신 채용률" in x or x == "장신구 채용률",
        [
            lambda x: "방어구 주요 채용 룬" in x,
            lambda x: "방어구 계열별" in x,
            lambda x: x == "무기",
        ],
    )

    groups = {"80": [], "40": [], "10": []}
    raw_groups = {"80": [], "40": [], "10": []}
    current = None

    for raw in section:
        text = _clean(raw)
        compact = text.replace(" ", "")
        if text.startswith("80% 이상") or compact.startswith("80%이상"):
            current = "80"
            remainder = re.sub(r"^80%\s*이상\s*", "", text)
            if remainder:
                raw_groups[current].append(remainder)
            continue
        if re.match(r"^40\s*~\s*79%", text):
            current = "40"
            remainder = re.sub(r"^40\s*~\s*79%\s*", "", text)
            if remainder:
                raw_groups[current].append(remainder)
            continue
        if re.match(r"^10\s*~\s*39%", text):
            current = "10"
            remainder = re.sub(r"^10\s*~\s*39%\s*", "", text)
            if remainder:
                raw_groups[current].append(remainder)
            continue
        if current:
            raw_groups[current].append(text)

    for key in groups:
        groups[key] = _extract_name_percent_pairs(raw_groups[key])
    return groups


patch._extract_name_percent_pairs = _extract_name_percent_pairs
patch._parse_accessory = _parse_accessory
