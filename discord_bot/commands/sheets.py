# /악보 입력을 처리하고 검색 결과와 페이지 버튼을 전송합니다.
import discord

from discord_bot.presenters.sheets import build_sheet_embeds
from discord_bot.views.pagination import PaginationView


def register(bot):
    @bot.tree.command(
        name="악보",
        description="제목 또는 제작자로 악보를 검색합니다.",
    )
    @discord.app_commands.describe(검색어="검색할 곡 제목 또는 제작자 이름")
    async def sheet_search_command(
        interaction: discord.Interaction,
        검색어: str,
    ):
        await interaction.response.defer(thinking=True)

        query = 검색어.strip()

        if len(query) < 2:
            await interaction.followup.send("악보 검색어는 2글자 이상 입력해주세요.")
            return

        try:
            items = await bot.sheets.fetch_sheet_music(query, page_size=30)

            if not items:
                await interaction.followup.send(
                    f"🎼 **{query}** 검색 결과를 찾지 못했습니다."
                )
                return

            embeds = build_sheet_embeds(query, items)

            if not embeds:
                await interaction.followup.send(
                    f"🎼 **{query}** 검색 결과를 표시할 수 없습니다."
                )
                return

            if len(embeds) == 1:
                await interaction.followup.send(embed=embeds[0])
            else:
                view = PaginationView([[embed] for embed in embeds])
                message = await interaction.followup.send(
                    embed=embeds[0],
                    view=view,
                    wait=True,
                )
                view.message = message

        except Exception as e:
            print("[/악보 오류]", e)
            await interaction.followup.send(
                "악보 검색 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."
            )
