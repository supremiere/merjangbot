# /오픈알림으로 점검 종료 후 서버 오픈 멘션을 신청·해제합니다.
import logging

import discord

logger = logging.getLogger(__name__)


def register(bot):
    @bot.tree.command(
        name="오픈알림",
        description="점검 종료 후 서버가 열리면 멘션 알림을 신청하거나 해제합니다.",
    )
    async def open_subscription_command(interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령어는 서버 안에서 사용해주세요.", ephemeral=True
            )
            return

        try:
            subscribed, count = bot.open_subscriptions.toggle(interaction.user.id)
            if subscribed:
                message = (
                    "🔔 **오픈 알림 신청 완료!**\n"
                    "점검이 끝나고 서버가 **정상 운영으로 전환되는 순간** 멘션해드릴게요."
                )
            else:
                message = (
                    "🔕 **오픈 알림 해제 완료!**\n"
                    "앞으로 점검 종료 시 멘션하지 않습니다."
                )
            await interaction.response.send_message(
                message + f"\n현재 신청자: **{count}명**", ephemeral=True
            )
        except Exception:
            logger.exception("오픈알림 설정 오류")
            if interaction.response.is_done():
                await interaction.followup.send(
                    "오픈 알림 설정 중 오류가 발생했습니다.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "오픈 알림 설정 중 오류가 발생했습니다.", ephemeral=True
                )
