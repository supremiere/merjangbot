# 시세 결과를 이미지·가격 변동이 포함된 페이지별 카드로 만듭니다.
from datetime import timedelta, timezone

import discord

from sites.moblife.abyss import parse_iso_datetime

KST = timezone(timedelta(hours=9))


def format_number(value):
    if value is None:
        return "-"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def format_percent(value):
    if value is None:
        return "-"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return str(value)

    if value > 0:
        return f"+{value:.2f}%"
    return f"{value:.2f}%"


def format_api_updated_at(value):
    if not value:
        return None

    try:
        dt = parse_iso_datetime(value).astimezone(KST)
        return dt.strftime("%Y-%m-%d %H:%M:%S KST")
    except Exception:
        return str(value)


def _market_embed_color(change_24h):
    """24시간 등락에 따라 카드 색상을 살짝 구분한다."""
    try:
        value = float(change_24h)
    except (TypeError, ValueError):
        return discord.Color.from_rgb(88, 101, 242)

    if value > 0:
        return discord.Color.from_rgb(46, 204, 113)
    if value < 0:
        return discord.Color.from_rgb(231, 76, 60)
    return discord.Color.from_rgb(149, 165, 166)


def _normalize_icon_url(value):
    if not value:
        return None

    value = str(value).strip()

    if value.startswith("https://") or value.startswith("http://"):
        return value

    if value.startswith("/"):
        return "https://mabimobi.life" + value

    return None


def build_market_pages(query, items, updated_at):
    """
    /시세 결과를 아이템 카드 형태로 만든다.
    한 페이지에 4개 아이템을 보여주며 각 카드에 아이템 이미지를 붙인다.
    """
    pages = []
    per_page = 4
    total = len(items)
    total_pages = max(1, (total + per_page - 1) // per_page)
    updated_text = format_api_updated_at(updated_at)

    for start in range(0, total, per_page):
        chunk = items[start : start + per_page]
        page = start // per_page + 1

        header = discord.Embed(
            title=f"💰 {query} · 시세 검색",
            description=(
                f"검색 결과 **{total}개**"
                + (f" · **{page}/{total_pages} 페이지**" if total_pages > 1 else "")
            ),
            color=discord.Color.from_rgb(88, 101, 242),
        )

        if updated_text:
            header.set_footer(text=f"머장봇 · 갱신 {updated_text}")
        else:
            header.set_footer(text="머장봇")

        page_embeds = [header]

        for index, item in enumerate(chunk, start=start + 1):
            name = item.get("name") or "이름 없음"
            min_price = item.get("min_price")
            total_count = item.get("total_count")
            sold_out = bool(item.get("is_sold_out"))
            category = item.get("parent_category") or "-"
            change_1h = item.get("pct_change_1h")
            change_24h = item.get("pct_change_24h")
            change_7d = item.get("pct_change_7d")

            price_text = "매물 없음" if sold_out else format_number(min_price)

            card = discord.Embed(
                title=f"{index}. {name}",
                color=_market_embed_color(change_24h),
            )

            card.add_field(
                name="💵 최저가",
                value=f"**{price_text}**",
                inline=True,
            )
            card.add_field(
                name="📦 등록 수량",
                value=f"**{format_number(total_count)}**",
                inline=True,
            )
            card.add_field(
                name="🗂️ 분류",
                value=f"**{category}**",
                inline=True,
            )

            card.add_field(
                name="📊 가격 변동",
                value=(
                    f"1시간 **{format_percent(change_1h)}**  ·  "
                    f"24시간 **{format_percent(change_24h)}**  ·  "
                    f"7일 **{format_percent(change_7d)}**"
                ),
                inline=False,
            )

            icon_url = _normalize_icon_url(item.get("icon_url"))
            if icon_url:
                card.set_thumbnail(url=icon_url)

            if sold_out:
                card.description = "⚠️ 현재 등록된 매물이 없습니다."

            page_embeds.append(card)

        pages.append(page_embeds)

    return pages
