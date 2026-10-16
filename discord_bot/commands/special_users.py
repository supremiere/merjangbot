"""Administrator-only, guild-scoped special access commands."""
import discord
from discord import app_commands

from services.special_users import DISCORD_ID, SpecialUsersService


async def allowed(bot, interaction):
    guild = getattr(bot.settings, 'drop_stats_guild_id', 0)
    if (not guild or interaction.guild_id != guild
            or interaction.guild_id is None
            or not getattr(getattr(interaction, 'permissions', None), 'administrator', False)):
        await interaction.response.send_message('지정 서버의 관리자만 사용할 수 있습니다.', ephemeral=True)
        return False
    return True


async def respond(bot, interaction, action=None, user_id=None, after=None):
    # Recheck at execution; Discord's visibility permissions can be overridden
    # in server settings and must never be the sole authorization mechanism.
    if not await allowed(bot, interaction):
        return
    if ((user_id is not None and not DISCORD_ID.fullmatch(user_id))
            or (after and not DISCORD_ID.fullmatch(after))):
        await interaction.response.send_message('Discord 사용자 ID는 17~20자리 숫자로 입력해주세요.', ephemeral=True)
        return
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        data = await bot.special_users.request(action=action, user_id=user_id, after=after)
        if action == 'add':
            message = f'특별 허용에 추가했습니다: `{user_id}`' if data['changed'] else f'이미 특별 허용된 사용자입니다: `{user_id}`'
        elif action == 'remove':
            message = f'특별 허용에서 삭제했습니다: `{user_id}`' if data['changed'] else f'목록에 없는 사용자입니다: `{user_id}`'
        else:
            users = data['users']
            message = '특별 허용 사용자 목록\n'+'\n'.join(f"`{row['discord_user_id']}`" for row in users) if users else '특별 허용 사용자가 없습니다.'
            if data.get('next_cursor'):
                message += f"\n다음 목록: `/특별허용 목록 다음아이디:{data['next_cursor']}`"
        await interaction.followup.send(message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    except RuntimeError as error:
        await interaction.followup.send(str(error), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class SpecialUsersGroup(app_commands.Group):
    def __init__(self, bot):
        super().__init__(name='특별허용', description='던전헬퍼 특별 허용 사용자를 관리합니다.',
                         guild_only=True, default_permissions=discord.Permissions(administrator=True))
        self.bot = bot

    async def interaction_check(self, interaction):
        return await allowed(self.bot, interaction)

    @app_commands.command(name='추가', description='Discord 사용자 ID를 특별 허용에 추가합니다.')
    @app_commands.describe(사용자아이디='서버 미가입자도 Discord 사용자 ID로 추가할 수 있습니다.')
    async def add(self, interaction: discord.Interaction, 사용자아이디: str):
        await respond(self.bot, interaction, 'add', 사용자아이디)

    @app_commands.command(name='삭제', description='Discord 사용자 ID를 특별 허용에서 삭제합니다.')
    async def remove(self, interaction: discord.Interaction, 사용자아이디: str):
        await respond(self.bot, interaction, 'remove', 사용자아이디)

    @app_commands.command(name='목록', description='특별 허용 목록을 본인에게만 표시합니다.')
    @app_commands.describe(다음아이디='다음 페이지 안내에 표시된 ID (첫 조회는 비워두세요)')
    async def listing(self, interaction: discord.Interaction, 다음아이디: str | None = None):
        await respond(self.bot, interaction, after=다음아이디)


def register(bot):
    guild = getattr(bot.settings, 'drop_stats_guild_id', 0)
    key = getattr(bot.settings, 'special_users_admin_key', '')
    origin = getattr(bot.settings, 'drop_stats_origin', '')
    if guild and len(key) >= 32 and origin:
        bot.special_users = SpecialUsersService(origin, key)
        # Never copy this command to other servers or register it globally.
        bot.tree.add_command(SpecialUsersGroup(bot), guild=discord.Object(id=guild))
