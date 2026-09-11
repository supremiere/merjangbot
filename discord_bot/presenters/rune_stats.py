# 직업별 룬 통계와 콤보 운용 정보를 디스코드 카드로 구성하며 한줄 요약은 표시하지 않습니다.
import re

import discord


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


def _clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


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


def build_rune_stats_embed(data):
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
    for label, icon in (
        ("각성", "🌗"),
        ("용문장", "🐉"),
        ("침식", "🌫️"),
        ("그 외", "🛡️"),
    ):
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

    date_text = data.get("data_date") or "확인 불가"
    embed.set_footer(text=f"머장봇 · 데이터 {date_text}")
    for index, chunk in enumerate(_operation_chunks(data.get("operations") or [])):
        label = "🔄 콤보 · 운용 제보" if index == 0 else "🔄 콤보 · 운용 제보 (계속)"
        embed.add_field(name=label, value=chunk, inline=False)
    return embed
