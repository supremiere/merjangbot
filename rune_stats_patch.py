import re

import discord
from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

import rune_stats_bootstrap as rune_stats


# =========================================================
# 최신 룬통계 페이지 대응
# - 2026.09 구조: 실제 퍼센트 표시
# - 장신구: 80% 이상 / 40~79% / 10~39%
# - 방어구: 20% 이상 주요 채용 룬
# - 무기/엠블럼: 순위 + 실제 채용률
# =========================================================


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
    return values[start + 1:end]


def _is_percent(text):
    return bool(re.fullmatch(r"\d+(?:\.\d+)?%", _clean(text)))


def _extract_name_percent_pairs(values, noise=None):
    noise = set(noise or ())
    pairs = []
    pending = []

    for raw in values:
        text = _clean(raw)
        if not text or text in noise:
            continue
        if re.fullmatch(r"\d+", text):
            continue
        if text in {"NEW", "신규", "1픽", "2픽", "택1", "20%+"}:
            continue
        if text.startswith("세트(") or text.startswith("세트 "):
            continue

        if _is_percent(text):
            if pending:
                name = _clean(" ".join(pending))
                name = re.sub(r"^(?:NEW\s+)", "", name).strip()
                if name:
                    pairs.append({"name": name, "rate": text})
                pending = []
            continue

        # '구속 91.2%'처럼 한 노드에 같이 들어온 경우
        match = re.fullmatch(r"(.+?)\s+(\d+(?:\.\d+)?)%", text)
        if match:
            name = _clean(" ".join(pending + [match.group(1)]))
            if name:
                pairs.append({"name": name, "rate": f"{match.group(2)}%"})
            pending = []
            continue

        pending.append(text)

    return pairs


def _parse_accessory(values):
    section = _slice_between(
        values,
        lambda x: "장신구 최신 채용률" in x or x == "장신구 채용률",
        [
            lambda x: "방어구 주요 채용 룬" in x,
            lambda x: "방어구 계열별" in x,
            lambda x: x == "무기",
        ],
    )

    groups = {"80": [], "40": [], "10": []}
    current = None

    for raw in section:
        text = _clean(raw)
        if not text:
            continue

        joined = text.replace(" ", "")
        if text.startswith("80% 이상") or joined.startswith("80%이상"):
            current = "80"
            remainder = re.sub(r"^80%\s*이상\s*", "", text)
            if remainder:
                groups[current].extend(_extract_name_percent_pairs([remainder]))
            continue
        if re.match(r"^40\s*~\s*79%", text):
            current = "40"
            remainder = re.sub(r"^40\s*~\s*79%\s*", "", text)
            if remainder:
                groups[current].extend(_extract_name_percent_pairs([remainder]))
            continue
        if re.match(r"^10\s*~\s*39%", text):
            current = "10"
            remainder = re.sub(r"^10\s*~\s*39%\s*", "", text)
            if remainder:
                groups[current].extend(_extract_name_percent_pairs([remainder]))
            continue

        if current:
            # 실제 HTML은 룬명 / 퍼센트가 별도 노드라 임시 저장 후 아래에서 재파싱
            groups[current].append(text)

    # 문자열 임시값을 name/rate 쌍으로 변환
    for key in tuple(groups):
        raw = groups[key]
        if raw and isinstance(raw[0], str):
            groups[key] = _extract_name_percent_pairs(raw)

    return groups


def _parse_defense(values):
    section = _slice_between(
        values,
        lambda x: "방어구 주요 채용 룬" in x or "방어구 계열별" in x,
        lambda x: x == "무기",
    )

    labels = ("각성", "용문장", "침식", "그 외")
    result = {label: [] for label in labels}
    current = None
    bucket = []

    def flush():
        nonlocal bucket
        if current is not None and bucket:
            result[current] = _extract_name_percent_pairs(
                bucket,
                noise={"택1", "한 자리", "20%+", "주요 채용 룬 20% 이상"},
            )
        bucket = []

    for raw in section:
        text = _clean(raw)
        detected = None
        for label in labels:
            if text == label or text.startswith(label):
                detected = label
                break

        if detected:
            flush()
            current = detected
            remainder = _clean(text[len(detected):])
            remainder = re.sub(r"^택1\s*", "", remainder)
            remainder = re.sub(r"^20%\+\s*", "", remainder)
            if remainder:
                bucket.append(remainder)
            continue

        if current:
            bucket.append(text)

    flush()
    return result


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
            rows.append({
                "combo": cells[0],
                "upgrade": cells[1],
                "engraving": cells[2],
                "note": cells[3] if len(cells) > 3 else "",
            })
        return rows[:5]
    return []


def _parse_page_latest(html):
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(" ", strip=True)
    data_date = _extract_data_date(page_text)

    parsed = {}
    for heading in soup.find_all("h3"):
        class_name = _clean(heading.get_text(" ", strip=True))
        if class_name not in rune_stats.RUNE_CLASSES:
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
            "basic_ratio": None,
            "data_date": data_date,
        }

    if len(parsed) < 15:
        raise RuntimeError(f"직업 데이터 파싱 수가 너무 적습니다. ({len(parsed)}개)")

    return parsed


def _format_pairs(pairs):
    if not pairs:
        return "-"
    return "  ".join(f"`{item['name']} {item['rate']}`" for item in pairs)


def _format_vertical_pairs(pairs):
    if not pairs:
        return "-"
    medals = ("🥇", "🥈", "🥉")
    lines = []
    for index, item in enumerate(pairs):
        prefix = medals[index] if index < len(medals) else "•"
        lines.append(f"{prefix} **{item['name']}** · {item['rate']}")
    return "\n".join(lines)


def _summary(data):
    top = data.get("accessory", {}).get("80", [])
    if len(top) >= 3:
        names = [item["name"] for item in top[:3]]
        return f"{' · '.join(names)} 3종이 현재 80% 이상 채용 중입니다."
    if top:
        return f"80% 이상 핵심 채용은 {' · '.join(item['name'] for item in top)}입니다."
    return "현재 채용률 데이터를 확인해주세요."


def _build_embed_latest(data):
    embed = discord.Embed(
        title=f"🟣 {data['class_name']} 룬 통계",
        description="현재 실제 채용률 기준으로 정리했습니다.",
        color=discord.Color.purple(),
    )

    acc = data.get("accessory", {})
    lines = []
    if acc.get("80"):
        lines.append(f"🟢 **80% 이상**\n{_format_pairs(acc['80'])}")
    if acc.get("40"):
        lines.append(f"🟡 **40~79%**\n{_format_pairs(acc['40'])}")
    if acc.get("10"):
        lines.append(f"⚪ **10~39%**\n{_format_pairs(acc['10'])}")
    embed.add_field(
        name="💍 장신구 최신 채용률",
        value="\n\n".join(lines) if lines else "확인 가능한 데이터가 없습니다.",
        inline=False,
    )

    defense = data.get("defense", {})
    defense_lines = []
    for label, icon in (("각성", "🌗"), ("용문장", "🐉"), ("침식", "🌫️"), ("그 외", "🛡️")):
        pairs = defense.get(label) or []
        if pairs:
            defense_lines.append(f"{icon} **{label}**\n{_format_pairs(pairs)}")
    if defense_lines:
        embed.add_field(
            name="🛡️ 방어구 주요 채용 룬",
            value="\n\n".join(defense_lines),
            inline=False,
        )

    embed.add_field(
        name="⚔️ 무기",
        value=_format_vertical_pairs(data.get("weapon") or []),
        inline=True,
    )
    embed.add_field(
        name="🏅 엠블럼",
        value=_format_vertical_pairs(data.get("emblem") or []),
        inline=True,
    )

    combos = data.get("combos") or []
    if combos:
        combo_lines = []
        for row in combos[:3]:
            line = f"**{row['combo']}**"
            details = []
            if row.get("upgrade"):
                details.append(f"개조 `{row['upgrade']}`")
            if row.get("engraving"):
                details.append(f"세공 `{row['engraving']}`")
            if row.get("note"):
                details.append(row["note"])
            if details:
                line += "\n" + " · ".join(details)
            combo_lines.append(line)
        embed.add_field(
            name="🧩 실전 장신구 조합",
            value="\n\n".join(combo_lines),
            inline=False,
        )

    embed.add_field(name="📌 한줄 요약", value=_summary(data), inline=False)
    date_text = data.get("data_date") or "확인 불가"
    embed.set_footer(text=f"머장봇 · 데이터 {date_text}")
    return embed


# bootstrap의 네트워크/캐시/명령어 구조는 그대로 쓰고 파싱/표시만 최신화한다.
rune_stats._parse_page = _parse_page_latest
rune_stats._build_embed = _build_embed_latest
