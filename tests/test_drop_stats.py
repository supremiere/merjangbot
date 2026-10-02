import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from discord_bot.commands.drop_stats import build_embed,respond

DATA = {'day':'2026-10-03','items':[], 'runs':[], 'leaders':[
    {'mode':'dungeon','nickname':'@everyone **돌왕**','amount':10},
    {'mode':'abyss','nickname':'마력왕','amount':2}]}

def interaction(guild=222):
    return SimpleNamespace(guild_id=guild,user=SimpleNamespace(id=123),
        response=SimpleNamespace(defer=AsyncMock(),send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()))

def test_separate_rankings_and_no_nickname_pings():
    embed = build_embed(DATA,'ranking')
    assert [f.name for f in embed.fields] == ['영혼석 TOP 5','마력석 TOP 5']
    assert '@everyone' not in embed.fields[0].value
    assert '마력왕' not in embed.fields[0].value
    assert '마력왕' in embed.fields[1].value
    assert '구매·거래' in embed.footer.text

def test_stats_are_restricted_to_guild_and_personal_identity():
    async def scenario():
        bot = SimpleNamespace(settings=SimpleNamespace(drop_stats_guild_id=222),
                              drop_stats=SimpleNamespace(fetch=AsyncMock(return_value=DATA)))
        wrong = interaction(999)
        await respond(bot,wrong,'ranking')
        assert bot.drop_stats.fetch.await_count == 0
        assert wrong.response.send_message.call_args.kwargs['ephemeral'] is True
        mine = interaction()
        await respond(bot,mine,'mine')
        bot.drop_stats.fetch.assert_awaited_once_with(123)
        assert mine.followup.send.call_args.kwargs['ephemeral'] is True
        assert not mine.followup.send.call_args.kwargs['allowed_mentions'].everyone
        public = interaction()
        await respond(bot,public,'ranking')
        assert public.followup.send.call_args.kwargs['ephemeral'] is False
    asyncio.run(scenario())
