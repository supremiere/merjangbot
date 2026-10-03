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


def test_stats_and_personal_records_use_hamsuni_completion_label():
    data = {**DATA, 'runs': [{'mode': 'dungeon', 'runs': 12}, {'mode': 'abyss', 'runs': 3}]}
    for kind in ('stats', 'mine'):
        field = build_embed(data, kind).fields[-1]
        assert field.name == '햄순이로 완료한 횟수'
        assert field.value == '15회'


def test_drop_rates_use_each_dungeon_sample_not_stone_quantity_or_all_runs():
    data = {**DATA, 'items': [{'mode':'dungeon','item':'야생의 영혼석','amount':99}],
            'runs':[{'mode':'dungeon','runs':1000}],
            'observations':[{'mode':'dungeon','item':'야생의 영혼석','observed_runs':40,'hit_runs':5},
                            {'mode':'dungeon','item':'삼림의 영혼석','observed_runs':10,'hit_runs':0},
                            {'mode':'abyss','item':'허상의 마력석','observed_runs':3,'hit_runs':3}]}
    embed = build_embed(data,'stats')
    assert '야생의 영혼석 — 12.5%' in embed.fields[0].value
    assert '획득 5/40판' in embed.fields[0].value
    assert '오늘 99개' in embed.fields[0].value
    assert '삼림의 영혼석 — 0.0%' in embed.fields[0].value
    assert '공명의 영혼석 — 집계 중' in embed.fields[0].value
    assert '허상의 마력석 — 100.0%' in embed.fields[1].value
    assert all(len(field.value) <= 1024 for field in embed.fields)


def test_legacy_totals_without_samples_are_not_presented_as_drop_probability():
    embed = build_embed({**DATA,'items':[{'mode':'dungeon','item':'야생의 영혼석','amount':10}],
                        'runs':[{'mode':'dungeon','runs':100}]},'mine')
    assert '야생의 영혼석 — 집계 중' in embed.fields[0].value
    assert '오늘 10개' in embed.fields[0].value
    assert '%' not in embed.fields[0].value

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
