# 어비스 현재 상태와 사전 알림을 한글 디스코드 카드로 만듭니다.
from datetime import timedelta

import discord

from sites.moblife.abyss import ABYSS_CYCLE, KST

WEEKDAYS_KO = ("월", "화", "수", "목", "금", "토", "일")


def format_korean_date(dt_kst):
    weekday = WEEKDAYS_KO[dt_kst.weekday()]
    return f"{dt_kst.year}년 {dt_kst.month}월 {dt_kst.day}일 ({weekday})"


def format_korean_time(dt_kst):
    hour = dt_kst.hour
    minute = dt_kst.minute

    if hour == 0:
        return f"오전 12:{minute:02d}"
    if hour < 12:
        return f"오전 {hour}:{minute:02d}"
    if hour == 12:
        return f"오후 12:{minute:02d}"
    return f"오후 {hour - 12}:{minute:02d}"


def format_remaining_time(seconds):
    seconds = max(0, int(seconds))
    minutes = (seconds + 59) // 60

    if minutes < 1:
        return "1분 미만"

    hours, mins = divmod(minutes, 60)

    if hours and mins:
        return f"{hours}시간 {mins}분"
    if hours:
        return f"{hours}시간"
    return f"{mins}분"


def build_abyss_alert(spawn_utc, minutes_before, estimated):
    spawn_kst = spawn_utc.astimezone(KST)

    if minutes_before == 1:
        title = "🚨🚨 어비스 구멍 출현 1분 전!"
    elif minutes_before == 10:
        title = "🚨 어비스 구멍 출현 10분 전!"
    else:
        title = f"🕳️ 어비스 구멍 출현 {minutes_before}분 전!"

    description = "어비스 구멍 출현 예정 시간입니다."
    if estimated:
        description += "\n⚠️ 점검 후 예상 시간이라 실제 출현 시각이 변경될 수 있습니다."

    embed = discord.Embed(
        title=title,
        description=description,
    )

    embed.add_field(
        name="📅 날짜",
        value=f"**{format_korean_date(spawn_kst)}**",
        inline=False,
    )
    embed.add_field(
        name="🕒 출현 예정",
        value=f"**{format_korean_time(spawn_kst)}**",
        inline=True,
    )
    embed.add_field(
        name="⏰ 남은 시간",
        value=f"**{minutes_before}분**",
        inline=True,
    )

    # 1분 전 알림에서는 다음 회차의 출현 시간도 같이 안내한다.
    if minutes_before == 1:
        next_spawn_kst = (spawn_utc + ABYSS_CYCLE).astimezone(KST)
        embed.add_field(
            name="🔁 다음 어비스",
            value=(
                f"**{format_korean_date(next_spawn_kst)} "
                f"{format_korean_time(next_spawn_kst)}**"
            ),
            inline=False,
        )

        if estimated:
            embed.add_field(
                name="⚠️ 참고",
                value="현재 시간이 점검 후 예상값이라 다음 회차 시간도 변경될 수 있습니다.",
                inline=False,
            )

    embed.set_footer(text="머장봇")

    return embed


def build_abyss_status(spawn_utc, is_active, estimated, now_utc):
    spawn_kst = spawn_utc.astimezone(KST)

    if is_active:
        active_end = spawn_utc + timedelta(minutes=15)
        remaining = (active_end - now_utc).total_seconds()

        embed = discord.Embed(
            title="🕳️ 어비스 구멍 출현 중!",
            description="현재 어비스 구멍이 출현 중입니다.",
        )
        embed.add_field(
            name="📅 날짜",
            value=f"**{format_korean_date(spawn_kst)}**",
            inline=False,
        )
        embed.add_field(
            name="🕒 출현 시각",
            value=f"**{format_korean_time(spawn_kst)}**",
            inline=True,
        )
        embed.add_field(
            name="⏰ 상태",
            value=f"**출현 중 · 약 {format_remaining_time(remaining)} 남음**",
            inline=True,
        )
    else:
        remaining = (spawn_utc - now_utc).total_seconds()

        description = "다음 어비스 구멍 출현 예정 시간입니다."
        if estimated:
            description += (
                "\n⚠️ 점검 후 예상 시간이라 실제 출현 시각이 변경될 수 있습니다."
            )

        embed = discord.Embed(
            title="🕳️ 다음 어비스 구멍",
            description=description,
        )
        embed.add_field(
            name="📅 날짜",
            value=f"**{format_korean_date(spawn_kst)}**",
            inline=False,
        )
        embed.add_field(
            name="🕒 출현 예정",
            value=f"**{format_korean_time(spawn_kst)}**",
            inline=True,
        )
        embed.add_field(
            name="⏰ 남은 시간",
            value=f"**{format_remaining_time(remaining)}**",
            inline=True,
        )

    embed.set_footer(text="머장봇")
    return embed
