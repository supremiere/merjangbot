import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from discord_bot.commands.trains import CHANNEL_ID, PANEL_TITLE, TrainController


def controller():
    value = object.__new__(TrainController)
    value.bot = SimpleNamespace(user=SimpleNamespace(id=123))
    value.repository = SimpleNamespace(panel_location=Mock(return_value=None),
                                       save_panel_location=Mock())
    value.panel_text = AsyncMock(return_value=PANEL_TITLE)
    return value


def test_renamed_panel_channel_is_selected_by_id_over_same_named_channel():
    target = SimpleNamespace(id=CHANNEL_ID, name='🚉변경된-좌석도')
    decoy = SimpleNamespace(id=999, name='우만열차좌석도')
    guild = SimpleNamespace(name='테스트', text_channels=[decoy, target],
                            create_text_channel=AsyncMock())
    assert asyncio.run(controller()._find_channel(guild)) is target
    guild.create_text_channel.assert_not_awaited()


def test_missing_configured_channel_never_uses_name_or_creates_a_replacement():
    guild = SimpleNamespace(id=10, name='테스트',
                            text_channels=[SimpleNamespace(id=999, name='우만열차좌석도')],
                            create_text_channel=AsyncMock())
    value = controller()
    assert asyncio.run(value.ensure_panel(guild)) is False
    guild.create_text_channel.assert_not_awaited()
    value.repository.save_panel_location.assert_not_called()


def test_stale_saved_location_cannot_redirect_panel_and_existing_target_panel_is_reused():
    message = SimpleNamespace(id=456, author=SimpleNamespace(id=123),
                              content=PANEL_TITLE, pinned=True, edit=AsyncMock())

    async def history(*, limit):
        yield message

    target = SimpleNamespace(id=CHANNEL_ID, name='새-이름', history=history,
                             fetch_message=AsyncMock(), send=AsyncMock())
    old = SimpleNamespace(id=999, name='우만열차좌석도', fetch_message=AsyncMock(),
                          send=AsyncMock())
    guild = SimpleNamespace(id=10, name='테스트', text_channels=[old, target],
                            get_channel=Mock(return_value=old))
    value = controller()
    value.repository.panel_location.return_value = (old.id, 111)
    assert asyncio.run(value.ensure_panel(guild)) is True
    old.fetch_message.assert_not_awaited()
    old.send.assert_not_awaited()
    target.fetch_message.assert_not_awaited()
    target.send.assert_not_awaited()
    message.edit.assert_awaited_once()
    value.repository.save_panel_location.assert_called_once_with(10, CHANNEL_ID, message.id)


def test_saved_message_in_configured_channel_is_reused_directly():
    message = SimpleNamespace(id=456, pinned=True, edit=AsyncMock())
    target = SimpleNamespace(id=CHANNEL_ID, name='새-이름',
                             fetch_message=AsyncMock(return_value=message), send=AsyncMock())
    guild = SimpleNamespace(id=10, name='테스트', text_channels=[target])
    value = controller()
    value.repository.panel_location.return_value = (CHANNEL_ID, message.id)
    assert asyncio.run(value.ensure_panel(guild)) is True
    target.fetch_message.assert_awaited_once_with(message.id)
    target.send.assert_not_awaited()
    message.edit.assert_awaited_once()
    value.repository.save_panel_location.assert_called_once_with(10, CHANNEL_ID, message.id)
