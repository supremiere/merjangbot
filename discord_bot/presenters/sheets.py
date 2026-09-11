# 악보 검색 결과를 제목·제작자·원본 링크 카드로 만듭니다.
import discord

from sites.moblife.sheets import (
    get_sheet_creator,
    get_sheet_meta,
    get_sheet_source_url,
    get_sheet_title,
)


def build_sheet_embeds(query, items):
    unique = []
    seen = set()

    for item in items:
        title = get_sheet_title(item)
        source_url = get_sheet_source_url(item)

        key = source_url or (
            title.lower(),
            (get_sheet_creator(item) or "").lower(),
        )
        if key in seen:
            continue

        seen.add(key)
        unique.append(item)

    # 제목에 검색어가 직접 들어간 결과를 위로 올려서 보기 좋게 정렬
    q = query.casefold()
    unique.sort(
        key=lambda item: (
            0 if q in get_sheet_title(item).casefold() else 1,
            get_sheet_title(item).casefold(),
        )
    )

    # API에서 받아온 결과는 가능한 한 모두 페이지로 보여준다.
    # 현재 검색 요청은 최대 30개를 받아오므로 최대 5페이지(페이지당 6개) 정도가 된다.
    if not unique:
        return []

    embeds = []
    per_embed = 6
    total = len(unique)
    total_pages = (total + per_embed - 1) // per_embed
    for start in range(0, total, per_embed):
        chunk = unique[start : start + per_embed]
        page = start // per_embed + 1

        embed = discord.Embed(
            title=f"🎼 {query} · 악보 검색 결과",
            description=f"검색 결과 **{total}개** · {page}/{total_pages} 페이지",
            color=discord.Color.from_rgb(88, 101, 242),
        )

        for index, item in enumerate(chunk, start=start + 1):
            title = get_sheet_title(item)
            source_url = get_sheet_source_url(item)
            meta_parts = get_sheet_meta(item)

            lines = []
            if meta_parts:
                lines.append(
                    "  ·  ".join(f"{icon} {text}" for icon, text in meta_parts)
                )

            if source_url:
                lines.append(f"[🔗 바로 열기]({source_url})")
            else:
                lines.append("🔗 원본 링크 없음")

            field_name = f"{index}. {title}"
            if len(field_name) > 250:
                field_name = field_name[:247] + "..."

            embed.add_field(
                name=field_name,
                value="\n".join(lines),
                inline=False,
            )

        embed.set_footer(text="머장봇")
        embeds.append(embed)

    return embeds
