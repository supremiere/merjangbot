# /어비스 조회, /어구알림 구독, /어구제보 기준점 보정을 처리합니다.
import logging
import re
from datetime import datetime, timedelta, timezone

import discord

from discord_bot.presenters.abyss import build_abyss_status
from services.abyss import ABYSS_CYCLE, KST

logger = logging.getLogger(__name__)

TIME_ONLY_RE = re.compile(r"^(\d{1,2})\s*:\s*(\d{2})$")
MONTH_DAY_RE = re.compile(
    r"^(\d{1,2})\s*/\s*(\d{1,2})\s+(\d{1,2})\s*:\s*(\d{2})$"
)
FULL_DATE_RE = re.compile(
    r"^(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\s+(\d{1,2})\s*:\s*(\d{2})$"
)


def _parse_report_time(value, now_kst):
    value = (value or "").strip()

    match = TIME_ONLY_RE.match(value)
    if match:
        hour, minute = map(int, match.groups())
        result = now_kst.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if result > now_kst + timedelta(minutes=5):
            result -= timedelta(days=1)
        return result

    match = MONTH_DAY_RE.match(value)
    if match:
        month, day, hour, minute = map(int, match.groups())
        year = now_kst.year
        if now_kst.month == 1 and month == 12:
            year -= 1
        elif now_kst.month == 12 and month == 1:
            year += 1
        return datetime(year, month, day, hour, minute, tzinfo=KST)

    match = FULL_DATE_RE.match(value)
    if match:
        year, month, day, hour, minute = map(int, match.groups())
        return datetime(year, month, day, hour, minute, tzinfo=KST)

    raise ValueError("시간 형식은 14:28 / 9/22 14:28 / 2026-09-22 14:28 중 하나로 입력해주세요.")


def _format_kst(dt):
    value = dt.astimezone(KST)
    return f"{value.month}/{value.day} {value.hour:02d}:{value.minute:02d}"


def register(bot):
    @bot.tree.command(
        name="어비스",
        description="어비스 구멍의 현재 상태와 다음 출현 시간을 확인합니다.",
    )
    async def abyss_command(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        try:
            await bot.abyss.refresh()

            if bot.abyss.needs_report:
                await interaction.followup.send(
                    "⚠️ **점검 후 첫 어구 제보 대기 중입니다.**\n"
                    "첫 어구가 확인되면 /어구제보 로 기준 시간을 넣어주세요."
                )
                return

            now = datetime.now(timezone.utc)
            spawn, active, estimated = bot.abyss.get_abyss_status(now)
            if spawn is None:
                await interaction.followup.send(
                    "지금은 어비스 출현 시간을 확인할 수 없습니다. 잠시 후 다시 시도해주세요."
                )
                return
            await interaction.followup.send(
                embed=build_abyss_status(spawn, active, estimated, now)
            )
        except Exception:
            logger.exception("어비스 명령어 오류")
            await interaction.followup.send(
                "어비스 시간을 확인하는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."
            )

    @bot.tree.command(
        name="어구제보",
        description="점검 후 첫 어구 시간을 제보해 어비스 기준 시간을 보정합니다.",
    )
    @discord.app_commands.describe(
        시간="예: 14:28 / 9/22 14:28 / 2026-09-22 14:28"
    )
    @discord.app_commands.default_permissions(manage_guild=True)
    async def abyss_report_command(
        interaction: discord.Interaction,
        시간: str,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            now_kst = datetime.now(KST)
            spawn_kst = _parse_report_time(시간, now_kst)

            try:
                maintenance = await bot.maintenance.fetch()
                bot.abyss.note_maintenance(
                    maintenance.get("last_maintenance_start_time"),
                    maintenance.get("last_maintenance_end_time"),
                    maintenance.get("last_maintenance_url"),
                )
            except Exception:
                logger.exception("어구제보 전 점검 정보 동기화 실패")

            observation = bot.abyss.report_spawn(
                spawn_kst.astimezone(timezone.utc),
                interaction.user.id,
            )
            next_spawn = spawn_kst + ABYSS_CYCLE
            count = bot.abyss.repository.observation_count()

            maintenance_end = observation.get("maintenance_end")
            if maintenance_end:
                end_dt = datetime.fromisoformat(maintenance_end).astimezone(KST)
                gap = spawn_kst - end_dt
                total_minutes = int(gap.total_seconds() // 60)
                gap_text = f"{total_minutes // 60}시간 {total_minutes % 60}분"
                maintenance_line = (
                    f"\n최근 점검 종료: **{_format_kst(end_dt)}**"
                    f" → 첫 어구까지 **{gap_text}**"
                )
            else:
                maintenance_line = ""

            await interaction.followup.send(
                "✅ **어구 기준시간 보정 완료**\n"
                f"첫 어구: **{_format_kst(spawn_kst)}**"
                f"{maintenance_line}\n"
                f"다음 어구: **{_format_kst(next_spawn)}**\n"
                f"누적 관측 데이터: **{count}건**",
                ephemeral=True,
            )
        except ValueError as e:
            await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
        except Exception:
            logger.exception("어구제보 오류")
            await interaction.followup.send(
                "어구 제보 저장 중 오류가 발생했습니다.", ephemeral=True
            )

    @bot.tree.command(
        name="어구알림",
        description="어비스 구멍 자동알림 멘션을 신청하거나 해제합니다.",
    )
    async def abyss_subscription_command(interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령어는 서버 안에서 사용해주세요.", ephemeral=True
            )
            return
        try:
            subscribed, count = bot.subscriptions.toggle(interaction.user.id)
            if subscribed:
                message = (
                    "🔔 **어구 알림 신청 완료!**\n"
                    "앞으로 어비스 구멍 출현 **60분 / 30분 / 10분 / 1분 전** "
                    "자동알림마다 멘션해드릴게요."
                )
            else:
                message = (
                    "🔕 **어구 알림 해제 완료!**\n"
                    "앞으로 어비스 구멍 자동알림에서 멘션하지 않습니다."
                )
            await interaction.response.send_message(
                message + f"\n현재 신청자: **{count}명**", ephemeral=True
            )
        except Exception:
            logger.exception("어구알림 설정 오류")
            if interaction.response.is_done():
                await interaction.followup.send(
                    "어구 알림 설정 중 오류가 발생했습니다.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "어구 알림 설정 중 오류가 발생했습니다.", ephemeral=True
                )
