import re

from bs4 import BeautifulSoup

import rune_stats_bootstrap as rune_stats
import rune_stats_patch as patch


# =========================================================
# /룬통계 - 콤보 · 운용 제보 확장
# - 각 직업 카드 안의 콤보/운용 테이블을 자동 수집한다.
# - 표가 없는 직업은 출력하지 않는다.
# =========================================================

_original_parse_page = rune_stats._parse_page
_original_build_embed = rune_stats._build_embed


def _clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _same_class_table(heading, table):
    """해당 table이 현재 직업 h3와 다음 직업 h3 사이에 있는지 확인한다."""
    return table.find_previous("h3") is heading


def _parse_operation_table(heading):
    for table in heading.find_all_next("table"):
        if not _same_class_table(heading, table):
            break

        headers = [
            _clean(th.get_text(" ", strip=True))
            for th in table.find_all("th")
        ]
        header_text = " | ".join(headers)

        # 최신 페이지의 '콤보 · 운용 제보' 표 식별
        if not (
            ("콤보" in header_text and "운용" in header_text)
            or ("기준" in header_text and "상황" in header_text and "비고" in header_text)
        ):
            continue

        rows = []
        for tr in table.find_all("tr"):
            cells = [
                _clean(td.get_text(" ", strip=True))
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

            rows.append({
                "situation": situation or "기본",
                "combo": combo,
                "note": note,
            })

        if rows:
            return rows

    return []


def _parse_page_with_operations(html):
    parsed = _original_parse_page(html)
    soup = BeautifulSoup(html, "html.parser")

    for heading in soup.find_all("h3"):
        class_name = _clean(heading.get_text(" ", strip=True))
        if class_name not in parsed:
            continue
        parsed[class_name]["operations"] = _parse_operation_table(heading)

    return parsed


def _operation_chunks(rows, limit=980):
    blocks = []
    for row in rows:
        lines = []
        situation = _clean(row.get("situation")) or "기본"
        combo = _clean(row.get("combo"))
        note = _clean(row.get("note"))

        lines.append(f"**{situation}**")
        if combo:
            lines.append(f"`{combo}`")
        if note:
            lines.append(f"💬 {note}")
        blocks.append("\n".join(lines))

    chunks = []
    current = ""
    for block in blocks:
        candidate = block if not current else current + "\n\n" + block
        if len(candidate) > limit and current:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _build_embed_with_operations(data):
    embed = _original_build_embed(data)
    rows = data.get("operations") or []
    if not rows:
        return embed

    chunks = _operation_chunks(rows)
    for index, chunk in enumerate(chunks):
        name = "🔄 콤보 · 운용 제보" if index == 0 else "🔄 콤보 · 운용 제보 (계속)"
        embed.add_field(name=name, value=chunk, inline=False)

    return embed


rune_stats._parse_page = _parse_page_with_operations
rune_stats._build_embed = _build_embed_with_operations
