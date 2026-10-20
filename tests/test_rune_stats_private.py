"""The /룬통계 command only responds to the invoking user."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from discord_bot.commands import rune_stats as rune_stats_command


class RecordingTree:
    def __init__(self):
        self.registered = None

    def command(self, **kwargs):
        def decorate(callback):
            self.registered = discord.app_commands.command(**kwargs)(callback)
            return self.registered
        return decorate


def setup_command(*, data=None):
    tree = RecordingTree()
    service = SimpleNamespace(get_class=AsyncMock(return_value=data), cache={})
    bot = SimpleNamespace(tree=tree, rune_stats=service)
    rune_stats_command.register(bot)
    interaction = SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    return tree.registered.callback, service, interaction


def assert_private(interaction):
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    interaction.followup.send.assert_awaited_once()
    assert interaction.followup.send.await_args.kwargs["ephemeral"] is True


def test_successful_rune_stats_are_private(monkeypatch):
    expected = discord.Embed(title="테스트")
    monkeypatch.setattr(rune_stats_command, "build_rune_stats_embed", lambda data: expected)
    callback, service, interaction = setup_command(data={"class_name": "기사"})

    asyncio.run(callback(interaction, "기사"))

    service.get_class.assert_awaited_once_with("기사")
    assert_private(interaction)
    assert interaction.followup.send.await_args.kwargs["embed"] is expected


def test_unknown_class_error_is_private():
    callback, service, interaction = setup_command()

    asyncio.run(callback(interaction, "없는 직업"))

    service.get_class.assert_not_awaited()
    assert_private(interaction)


def test_data_unavailable_error_is_private():
    callback, service, interaction = setup_command()

    asyncio.run(callback(interaction, "기사"))

    assert_private(interaction)
