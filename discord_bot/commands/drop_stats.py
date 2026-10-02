import discord

FOOTER = '한국 시간 오늘 기준 · 앱에서 확인한 가방 수량 증가 · 구매·거래 포함 가능'

def safe_nickname(value):
    value = ''.join(c for c in str(value) if c.isprintable()).replace('\u202e','')[:40]
    return discord.utils.escape_markdown(discord.utils.escape_mentions(value))

def build_embed(data,kind):
    title = {'stats':'오늘 드랍 통계','ranking':'오늘 수량 랭킹','mine':'내 오늘 기록'}[kind]
    embed = discord.Embed(title=title,description=data['day'],color=0x23D9C3)
    if kind == 'ranking':
        for mode,label in [('dungeon','영혼석 TOP 5'),('abyss','마력석 TOP 5')]:
            leaders = [row for row in data.get('leaders',[]) if row['mode']==mode]
            rows = [f"{i}. {safe_nickname(row['nickname'])} — {int(row['amount']):,}개"
                    for i,row in enumerate(leaders[:5],1)]
            embed.add_field(name=label,value='\n'.join(rows) or '아직 기록이 없습니다.',inline=False)
    else:
        for mode,label in [('dungeon','영혼석'),('abyss','마력석')]:
            rows = [f"{row['item']}: {int(row['amount']):,}개" for row in data.get('items',[]) if row['mode']==mode]
            embed.add_field(name=label,value='\n'.join(rows) or '아직 기록이 없습니다.',inline=False)
        runs = sum(int(row['runs']) for row in data.get('runs',[]))
        embed.add_field(name='헬퍼로 완료한 횟수',value=f'{runs:,}회',inline=False)
    embed.set_footer(text=FOOTER)
    return embed

async def respond(bot,interaction,kind):
    guild_id = getattr(bot.settings,'drop_stats_guild_id',0)
    if not guild_id or interaction.guild_id != guild_id:
        await interaction.response.send_message('던전헬퍼 지정 서버에서 사용할 수 있습니다.',ephemeral=True)
        return
    private = kind == 'mine'
    await interaction.response.defer(thinking=True,ephemeral=private)
    try:
        # Personal identity comes from Discord's interaction, not a typed nickname.
        data = await bot.drop_stats.fetch(interaction.user.id if private else None)
        await interaction.followup.send(embed=build_embed(data,kind),ephemeral=private,
                                        allowed_mentions=discord.AllowedMentions.none())
    except RuntimeError as error:
        await interaction.followup.send(str(error),ephemeral=True)

def register(bot):
    @bot.tree.command(name='드랍통계',description='던전헬퍼 참여자의 오늘 영혼석·마력석 수량을 확인합니다.')
    @discord.app_commands.guild_only()
    async def stats(interaction: discord.Interaction):
        await respond(bot,interaction,'stats')

    @bot.tree.command(name='오늘랭킹',description='오늘 수량이 가장 많은 던전헬퍼 참여자를 확인합니다.')
    @discord.app_commands.guild_only()
    async def ranking(interaction: discord.Interaction):
        await respond(bot,interaction,'ranking')

    @bot.tree.command(name='내기록',description='내 던전헬퍼 오늘 통계를 나에게만 표시합니다.')
    @discord.app_commands.guild_only()
    async def mine(interaction: discord.Interaction):
        await respond(bot,interaction,'mine')
