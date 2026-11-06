"""Exercise discord.py's actual Webhook.send validation without network calls."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord.webhook.async_ import async_context

from discord_bot.commands.drop_stats import respond
from test_drop_stats import DATA, client_record


class LocalWebhook(discord.Webhook):
    def _create_message(self, data, *, thread):
        return SimpleNamespace(id=123)


def make_case(data):
    state = SimpleNamespace(allowed_mentions=None, store_view=Mock())
    webhook = LocalWebhook({'id':'123', 'type':3, 'token':'synthetic-offline-only'},
                           session=object(), state=state)
    interaction = SimpleNamespace(guild_id=222, user=SimpleNamespace(id=123),
        response=SimpleNamespace(defer=AsyncMock()), followup=webhook)
    bot = SimpleNamespace(settings=SimpleNamespace(drop_stats_guild_id=222),
                          drop_stats=SimpleNamespace(fetch=AsyncMock(return_value=data)))
    return bot, interaction, state


@pytest.mark.parametrize('count', [None, 0, 1, 2, 3, 6])
def test_actual_webhook_accepts_personal_pages_and_keeps_them_private(count):
    async def run():
        data = dict(DATA)
        if count is not None:
            data['clients'] = [client_record(i,10,1) for i in range(1,count+1)]
        bot, interaction, state = make_case(data)
        adapter = SimpleNamespace(execute_webhook=AsyncMock(return_value={}))
        token = async_context.set(adapter)
        try:
            await respond(bot, interaction, 'mine')
            interaction.response.defer.assert_awaited_once_with(thinking=True,ephemeral=True)
            bot.drop_stats.fetch.assert_awaited_once_with(123)
            adapter.execute_webhook.assert_awaited_once()
            args = adapter.execute_webhook.call_args.kwargs
            payload = args['payload']
            assert len(payload['embeds']) == (2 if count and count >= 2 else 1)
            assert payload['flags'] & 64  # EPHEMERAL
            assert payload['allowed_mentions']['parse'] == []
            assert args['with_components'] == bool(count and count > 2)
            assert state.store_view.call_count == bool(count and count > 2)
        finally:
            for call in state.store_view.call_args_list:
                call.args[0].stop()
            async_context.reset(token)
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['stats', 'ranking'])
def test_public_stats_still_send_without_components(kind):
    async def run():
        bot, interaction, _ = make_case(DATA)
        adapter = SimpleNamespace(execute_webhook=AsyncMock(return_value={}))
        token = async_context.set(adapter)
        try:
            await respond(bot, interaction, kind)
            bot.drop_stats.fetch.assert_awaited_once_with(None)
            args = adapter.execute_webhook.call_args.kwargs
            assert len(args['payload']['embeds']) == 1
            assert not args['payload'].get('flags',0) & 64
            assert not args['with_components']
        finally:
            async_context.reset(token)
    asyncio.run(run())


def test_malformed_personal_response_finishes_thinking_with_private_error(caplog):
    async def run():
        bot, interaction, _ = make_case({**DATA, 'clients':[{'runs':[]}]})
        adapter = SimpleNamespace(execute_webhook=AsyncMock(return_value={}))
        token = async_context.set(adapter)
        try:
            await respond(bot, interaction, 'mine')
            adapter.execute_webhook.assert_awaited_once()
            payload = adapter.execute_webhook.call_args.kwargs['payload']
            assert '오류가 발생' in payload['content']
            assert payload['flags'] & 64 and payload['allowed_mentions']['parse'] == []
            assert 'stage=format error=KeyError' in caplog.text
        finally:
            async_context.reset(token)
    asyncio.run(run())


def test_service_failure_finishes_thinking_with_private_message():
    async def run():
        bot, interaction, _ = make_case(DATA)
        bot.drop_stats.fetch.side_effect = RuntimeError('통계 서버 연결 실패')
        adapter = SimpleNamespace(execute_webhook=AsyncMock(return_value={}))
        token = async_context.set(adapter)
        try:
            await respond(bot, interaction, 'mine')
            adapter.execute_webhook.assert_awaited_once()
            payload = adapter.execute_webhook.call_args.kwargs['payload']
            assert payload['content'] == '통계 서버 연결 실패'
            assert payload['flags'] & 64
        finally:
            async_context.reset(token)
    asyncio.run(run())


def test_discord_send_rejection_reports_error_and_stops_unsent_view(monkeypatch, caplog):
    async def run():
        from discord_bot.commands import drop_stats
        created = []
        original = drop_stats.PersonalPages
        def view(*args):
            result = original(*args)
            created.append(result)
            return result
        monkeypatch.setattr(drop_stats, 'PersonalPages', view)
        bot, interaction, _ = make_case({**DATA, 'clients':[client_record(i,10,1) for i in range(3)]})
        rejected = discord.HTTPException(SimpleNamespace(status=400,reason='Bad Request'),
                                         {'code':50035,'message':'Synthetic invalid message'})
        adapter = SimpleNamespace(execute_webhook=AsyncMock(side_effect=[rejected,{}]))
        token = async_context.set(adapter)
        try:
            await respond(bot, interaction, 'mine')
            assert adapter.execute_webhook.await_count == 2
            payload = adapter.execute_webhook.call_args.kwargs['payload']
            assert '오류가 발생' in payload['content'] and payload['flags'] & 64
            assert created[0].is_finished()
            assert 'stage=send error=HTTPException' in caplog.text
        finally:
            for item in created:
                item.stop()
            async_context.reset(token)
    asyncio.run(run())
