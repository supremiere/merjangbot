import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from discord import app_commands
import pytest

from discord_bot.commands.special_users import SpecialUsersGroup, register, respond
from services.special_users import SpecialUsersService

UID = '123456789012345678'  # Synthetic fixture, never provisioned remotely.


def bot():
    client = discord.Client(intents=discord.Intents.none())
    return SimpleNamespace(settings=SimpleNamespace(drop_stats_guild_id=222,
        drop_stats_origin='https://auth.example.test', special_users_admin_key='x'*43),
        tree=app_commands.CommandTree(client), special_users=SimpleNamespace(request=AsyncMock()))


def interaction(guild=222, admin=True, manage=False):
    return SimpleNamespace(guild_id=guild,
        permissions=discord.Permissions(administrator=admin, manage_guild=manage),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()))


@pytest.mark.parametrize('guild,admin,manage', [(222, False, False), (222, False, True),
                                              (333, True, True), (None, True, True)])
def test_unauthorized_cannot_read_or_mutate_even_if_discord_permissions_overridden(guild, admin, manage):
    b = bot()
    for action in ('add', 'remove', None):
        i = interaction(guild, admin, manage)
        asyncio.run(respond(b, i, action, UID if action else None))
        b.special_users.request.assert_not_awaited()
        i.response.defer.assert_not_awaited()
        assert i.response.send_message.await_args.kwargs['ephemeral'] is True
    i = interaction(guild, admin, manage)
    assert asyncio.run(SpecialUsersGroup(b).interaction_check(i)) is False


def test_metadata_hides_from_nonadmins_and_command_stays_in_target_guild_after_sync_copy():
    b = bot()
    register(b)
    for guild in (222, 333):
        b.tree.copy_global_to(guild=discord.Object(id=guild))
    assert b.tree.get_command('특별허용') is None
    assert b.tree.get_command('특별허용', guild=discord.Object(id=333)) is None
    group = b.tree.get_command('특별허용', guild=discord.Object(id=222))
    metadata = group.to_dict(b.tree)
    assert metadata['default_member_permissions'] == discord.Permissions(administrator=True).value
    assert metadata['dm_permission'] is False
    assert {option['name'] for option in metadata['options']} == {'추가', '삭제', '목록'}
    assert metadata['options'][0]['options'][0]['type'] == 3  # String, never a lossy numeric snowflake.


@pytest.mark.parametrize('field,value', [('special_users_admin_key', ''), ('special_users_admin_key', 'short'),
                                         ('drop_stats_origin', ''), ('drop_stats_guild_id', 0)])
def test_missing_optional_configuration_does_not_register_commands(field, value):
    b = bot()
    setattr(b.settings, field, value)
    register(b)
    assert b.tree.get_command('특별허용', guild=discord.Object(id=222)) is None


@pytest.mark.parametrize('action,changed', [('add', True), ('add', False), ('remove', True), ('remove', False)])
def test_admin_changes_are_private_and_use_exact_user_id(action, changed):
    b, i = bot(), interaction()
    b.special_users.request.return_value = {'ok': True, 'user_id': UID, 'changed': changed}
    asyncio.run(respond(b, i, action, UID))
    b.special_users.request.assert_awaited_once_with(action=action, user_id=UID, after=None)
    assert i.response.defer.await_args.kwargs['ephemeral'] is True
    sent = i.followup.send.await_args
    assert UID in sent.args[0]
    assert sent.kwargs['ephemeral'] is True
    assert sent.kwargs['allowed_mentions'].to_dict()['parse'] == []


def test_list_pagination_and_errors_only_visible_to_requesting_admin():
    b, i = bot(), interaction()
    b.special_users.request.return_value = {'users': [{'discord_user_id': UID}], 'next_cursor': UID}
    asyncio.run(respond(b, i))
    sent = i.followup.send.await_args
    assert '다음아이디:' in sent.args[0] and sent.kwargs['ephemeral'] is True
    b.special_users.request.side_effect = RuntimeError('연결 실패')
    asyncio.run(respond(b, i, 'add', UID))
    assert i.followup.send.await_args.kwargs['ephemeral'] is True


def test_invalid_id_never_contacts_management_api():
    b, i = bot(), interaction()
    asyncio.run(respond(b, i, 'add', 'not-an-id'))
    b.special_users.request.assert_not_awaited()
    assert i.response.send_message.await_args.kwargs['ephemeral'] is True


class Context:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *args):
        pass


class Content:
    def __init__(self, raw):
        self.raw = raw

    async def iter_chunked(self, size):
        for start in range(0, len(self.raw), size):
            yield self.raw[start:start+size]


def network_fixture(body, status=200):
    calls = []
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    response = SimpleNamespace(status=status, content=Content(raw))

    def request(method, url, **options):
        calls.append((method, url, options))
        return Context(response)

    return Context(SimpleNamespace(request=request)), calls


def test_service_uses_dedicated_bearer_post_with_redirects_disabled():
    session, calls = network_fixture({'ok': True, 'user_id': UID, 'changed': True})
    with patch('services.special_users.aiohttp.ClientSession', return_value=session):
        result = asyncio.run(SpecialUsersService('https://auth.example.test', 'secret'*8).request(action='add', user_id=UID))
    assert result['changed'] is True
    method, url, options = calls[0]
    assert method == 'POST' and url.endswith('/admin/special-users')
    assert options['headers']['Authorization'] == 'Bearer '+'secret'*8
    assert options['allow_redirects'] is False
    assert options['json'] == {'action': 'add', 'user_id': UID}


@pytest.mark.parametrize('body,status', [(b'x'*32769, 200), ({'ok': True, 'user_id': 'other', 'changed': True}, 200),
                                        ({}, 403), ({}, 302), (b'not json', 200)],
                         ids=['oversized', 'wrong-id', 'forbidden', 'redirect', 'malformed'])
def test_service_rejects_failed_oversized_or_invalid_responses_without_leaking_key(body, status):
    session, _ = network_fixture(body, status)
    key = 'private-key'*5
    with patch('services.special_users.aiohttp.ClientSession', return_value=session):
        with pytest.raises(RuntimeError) as error:
            asyncio.run(SpecialUsersService('https://auth.example.test', key).request(action='remove', user_id=UID))
    assert key not in str(error.value)
