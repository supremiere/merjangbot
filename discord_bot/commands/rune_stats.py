# /룬통계 명령어와 21개 직업 자동완성을 등록하고 캐시된 통계를 표시합니다.
import logging

import discord

from discord_bot.presenters.rune_stats import build_rune_stats_embed
from sites.erinndata.models import RUNE_CLASSES

logger = logging.getLogger(__name__)


def register(bot):
    @bot.tree.command(name="룬통계", description="직업별 룬 채용 통계를 확인합니다.")
    @discord.app_commands.describe(직업="확인할 직업")
    async def rune_stats_command(interaction: discord.Interaction, 직업: str):
        await interaction.response.defer(thinking=True)
        class_name = (직업 or "").strip()
        if class_name not in RUNE_CLASSES:
            await interaction.followup.send(
                "직업을 찾지 못했습니다. 직업 자동완성에서 선택해주세요."
            )
            return
        try:
            data = await bot.rune_stats.get_class(class_name)
        except Exception:
            logger.exception("룬통계 초기 갱신 오류")
            data = bot.rune_stats.cache.get(class_name)
        if data is None:
            await interaction.followup.send(
                "룬 통계를 불러오지 못했습니다. 잠시 후 다시 시도해주세요."
            )
            return
        await interaction.followup.send(embed=build_rune_stats_embed(data))

    @rune_stats_command.autocomplete("직업")
    async def rune_stats_autocomplete(interaction: discord.Interaction, current: str):
        needle = (current or "").strip().lower()
        names = [name for name in RUNE_CLASSES if not needle or needle in name.lower()]
        return [
            discord.app_commands.Choice(name=name, value=name) for name in names[:25]
        ]
