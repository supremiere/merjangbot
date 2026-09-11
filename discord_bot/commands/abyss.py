# /어비스 조회와 /어구알림 구독 신청·해제를 처리합니다.
import logging
from datetime import datetime, timezone

import discord

from discord_bot.presenters.abyss import build_abyss_status

logger = logging.getLogger(__name__)


def register(bot):
    @bot.tree.command(
        name="어비스",
        description="어비스 구멍의 현재 상태와 다음 출현 시간을 확인합니다.",
    )
    async def abyss_command(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        try:
            await bot.abyss.refresh()
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
                "어비스 시간을 불러오는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."
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
                message = "🔔 **어구 알림 신청 완료!**\n앞으로 어비스 구멍 출현 **60분 / 30분 / 10분 / 1분 전** 자동알림마다 멘션해드릴게요."
            else:
                message = "🔕 **어구 알림 해제 완료!**\n앞으로 어비스 구멍 자동알림에서 멘션하지 않습니다."
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
