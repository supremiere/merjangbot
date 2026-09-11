# 서버 상태 카드와 상태 표시가 붙은 채널명을 만듭니다.
import discord

from sites.moblife.maintenance import (
    KST,
    format_datetime,
    format_elapsed,
    parse_iso_datetime,
)

SERVER_STATUS_FOOTER_PREFIX = "머장봇 · 서버 상태"


def strip_status_dot(channel_name):
    """채널명 맨 앞의 기존 상태 동그라미만 제거한다."""
    name = (channel_name or "").strip()
    while name.startswith("🟢") or name.startswith("🔴"):
        name = name[1:].lstrip()
    return name


def build_status_channel_name(base_name, is_maintenance):
    dot = "🔴" if is_maintenance else "🟢"
    clean_name = strip_status_dot(base_name)
    if not clean_name:
        clean_name = "🖥️모비노기-서버상태"
    return f"{dot}{clean_name}"


def build_status_embed(data, now_utc):
    is_maintenance = bool(data.get("is_maintenance"))
    last_end_text = data.get("last_maintenance_end_time")
    current_start_text = data.get("current_maintenance_start_time")

    if is_maintenance:
        embed = discord.Embed(
            title="🔴 마비노기 모바일 서버 점검 중",
            description="현재 서버 점검이 진행 중입니다.",
            color=discord.Color.red(),
        )

        elapsed_text = "확인 불가"
        if current_start_text:
            try:
                elapsed_text = format_elapsed(
                    now_utc - parse_iso_datetime(current_start_text)
                )
            except Exception:
                pass

        embed.add_field(
            name="⏱️ 점검 경과",
            value=f"**{elapsed_text}**",
            inline=False,
        )
        embed.add_field(
            name="🛠️ 점검 시작",
            value=f"**{format_datetime(current_start_text)}**",
            inline=False,
        )
    else:
        embed = discord.Embed(
            title="🟢 마비노기 모바일 서버 정상 운영 중",
            description="최근 점검 종료 시각을 기준으로 계산한 서버 업타임입니다.",
            color=discord.Color.green(),
        )

        uptime_text = "확인 불가"
        if last_end_text:
            try:
                uptime_text = format_elapsed(
                    now_utc - parse_iso_datetime(last_end_text)
                )
            except Exception:
                pass

        embed.add_field(
            name="⏱️ 서버 업타임",
            value=f"**{uptime_text}**",
            inline=False,
        )
        embed.add_field(
            name="🛠️ 최근 점검 종료",
            value=f"**{format_datetime(last_end_text)}**",
            inline=False,
        )

    embed.add_field(
        name="🔄 마지막 확인",
        value=f"**{now_utc.astimezone(KST).strftime('%Y-%m-%d %H:%M KST')}**",
        inline=False,
    )
    embed.set_footer(text=f"{SERVER_STATUS_FOOTER_PREFIX} · 1분마다 갱신")
    return embed
