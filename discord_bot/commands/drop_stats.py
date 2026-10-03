import discord
from unicodedata import east_asian_width

FOOTER = '한국 시간 오늘 · 가방 증가량 기준(구매·거래 포함)'
CATALOG = {
    'dungeon': {'야생의 영혼석': '피오드 던전 1층', '삼림의 영혼석': '피오드 던전 2층',
                '공명의 영혼석': '룬다 던전 1층', '파동의 영혼석': '룬다 던전 2층',
                '망령의 영혼석': '페카 고분 심층 1층', '원념의 영혼석': '페카 고분 심층 2층'},
    'abyss': {'허상의 마력석': '허상의 정박지', '포식의 마력석': '광기의 동굴', '심해의 마력석': '흩어진 물길'},
}

def safe_nickname(value):
    value = ''.join(c for c in str(value) if c.isprintable()).replace('\u202e','')[:40]
    return discord.utils.escape_markdown(discord.utils.escape_mentions(value))


def compact_table(rows):
    rows = [('돌', '획득', '완료', '획득률'), *rows]
    def width(text):
        return sum(2 if east_asian_width(c) in 'WF' else 1 for c in text)
    widths = [max(width(row[i]) for row in rows) for i in range(4)]
    lines = []
    for row in rows:
        lines.append('  '.join(text + ' '*(widths[i]-width(text)) for i,text in enumerate(row)).rstrip())
    return '```\n' + '\n'.join(lines) + '\n```'

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
        embed.description += ' · 획득 개수 ÷ 완료 판수'
        for mode,label in [('dungeon','영혼석'),('abyss','마력석')]:
            amounts = {row['item']: int(row['amount']) for row in data.get('items',[]) if row['mode']==mode}
            total = sum(int(row['runs']) for row in data.get('runs',[]) if row['mode']==mode)
            by_dungeon = {row['dungeon']: int(row['runs']) for row in data.get('dungeon_runs',[]) if row['mode']==mode}
            # Unclassified history is a separate total, never every dungeon's denominator.
            unclassified = max(0,total - sum(by_dungeon.values()))
            rows = []
            for item,dungeon in CATALOG[mode].items():
                runs = by_dungeon.get(dungeon,0)
                amount = amounts.get(item,0)
                rate = f'{amount / runs * 100:.1f}%' if runs > 0 and not unclassified else '—'
                short = item.replace('의 영혼석','').replace('의 마력석','')
                rows.append((short,f'{amount:,}개',f'{runs:,}판',rate))
            value = compact_table(rows)
            if unclassified:
                amount = sum(amounts.get(item,0) for item in CATALOG[mode])
                value += f'\n미분류 {unclassified:,}판 · 전체 {amount / total * 100:.1f}%'
            embed.add_field(name=f'{label} · 던전별 기준',value=value,inline=False)
        runs = sum(int(row['runs']) for row in data.get('runs',[]))
        embed.add_field(name='햄순이로 완료한 횟수',value=f'{runs:,}회',inline=False)
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
    @bot.tree.command(name='드랍통계',description='오늘 영혼석·마력석 수량과 완료 대비 획득률을 확인합니다.')
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
