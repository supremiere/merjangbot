# /어비스랭킹 닉네임 [서버]와 동명이인 선택을 처리합니다.
import logging

import discord

from discord_bot.presenters.abyss_ranking import build_abyss_ranking_embed
from sites.moblife.abyss_ranking import CLASSES, SERVERS

logger = logging.getLogger(__name__)


class CharacterPicker(discord.ui.View):
    def __init__(self, service, entries, owner_id):
        super().__init__(timeout=180)
        self.service, self.entries, self.owner_id = service, entries, owner_id
        self.snapshot = service.cache
        self.message = None
        menu = discord.ui.Select(placeholder="확인할 캐릭터를 선택하세요", options=[
            discord.SelectOption(
                label=f"{e['character_name']} · {SERVERS[e['server']]}", value=str(index),
                description=f"{CLASSES[e['klass']]} · 서버 직업 {e['rank']}위 · {e['score']:,}점",
            ) for index, e in enumerate(entries)
        ])
        menu.callback = self.select_character
        self.add_item(menu)
        self.menu = menu

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("직접 /어비스랭킹 명령어로 조회해주세요.", ephemeral=True)
            return False
        return True

    async def select_character(self, interaction):
        entry = self.entries[int(self.menu.values[0])]
        result = self.service.result(entry, self.snapshot)
        await interaction.response.edit_message(content=None, embed=build_abyss_ranking_embed(result),
                                                view=None, allowed_mentions=discord.AllowedMentions.none())
        self.stop()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


def register(bot):
    @bot.tree.command(name="어비스랭킹", description="닉네임으로 어비스 순위와 서버 내 직업 순위를 확인합니다.")
    @discord.app_commands.describe(닉네임="정확한 캐릭터 이름", 서버="동명이인이 있으면 서버를 선택하세요")
    async def command(interaction: discord.Interaction, 닉네임: str, 서버: str | None = None):
        nickname = 닉네임.strip()
        server = (서버 or "").strip() or None
        if server in SERVERS.values():
            server = next(key for key, name in SERVERS.items() if name == server)
        if not nickname or len(nickname) > 100 or (server is not None and server not in SERVERS):
            await interaction.response.send_message("닉네임과 서버를 확인해주세요. 서버는 자동완성에서 선택할 수 있습니다.",
                                                     ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            snapshot = await bot.abyss_ranking.ensure_cache()
            if snapshot is None:
                if bot.abyss_ranking.last_error is not None:
                    message = "랭킹 데이터를 불러오지 못했습니다. 잠시 후 다시 시도해주세요."
                else:
                    message = "처음 랭킹을 집계하고 있습니다. 잠시 후 다시 조회해주세요."
                await interaction.followup.send(message)
                return
            matches = bot.abyss_ranking.find(nickname, server)
            if not matches:
                await interaction.followup.send(
                    f"{snapshot['snapshot_date']} 정산의 서버·직업별 상위 100명 기록에서 찾지 못했습니다.\n"
                    "닉네임·서버를 확인해주세요. 기록에 없더라도 캐릭터가 없는 것은 아닙니다."
                    + ("\n갱신이 지연되어 이전 기록으로 검색했습니다." if bot.abyss_ranking.stale else "")
                )
            elif len(matches) == 1:
                await interaction.followup.send(embed=build_abyss_ranking_embed(bot.abyss_ranking.result(matches[0])),
                                                allowed_mentions=discord.AllowedMentions.none())
            elif len(matches) > 25:
                await interaction.followup.send("일치하는 기록이 많습니다. 서버를 지정해 다시 조회해주세요.")
            else:
                view = CharacterPicker(bot.abyss_ranking, matches, interaction.user.id)
                view.message = await interaction.followup.send("이름이 같은 기록이 있습니다. 캐릭터를 선택해주세요.",
                                                               view=view, wait=True)
        except Exception:
            logger.exception("어비스랭킹 명령어 오류")
            await interaction.followup.send("랭킹 조회 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")

    @command.autocomplete("서버")
    async def server_autocomplete(interaction: discord.Interaction, current: str):
        return [discord.app_commands.Choice(name=name, value=key) for key, name in SERVERS.items()
                if current.strip() in name or current.strip() in key]
