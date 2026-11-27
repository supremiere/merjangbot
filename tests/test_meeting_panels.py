import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from discord_bot.commands import meetings
from services.meetings import MeetingService


def interaction(*, admin=False, uid=7, gid=20, service=None):
    guild = SimpleNamespace(id=gid, owner_id=1)
    actor = SimpleNamespace(id=uid, guild=guild, roles=[], bot=False,
                            guild_permissions=SimpleNamespace(administrator=admin))
    svc = service or MeetingService(None, Mock(), Mock())
    return SimpleNamespace(user=actor, guild=guild, guild_id=gid, channel_id=11,
        response=SimpleNamespace(is_done=lambda: False, send_message=AsyncMock(), edit_message=AsyncMock()),
        message=None, client=SimpleNamespace(meetings=svc))


class PanelTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_namespace_and_old_panel_guard(self):
        client = discord.Client(intents=discord.Intents.none())
        meetings.setup_views(client)
        main = next(v for v in client.persistent_views if isinstance(v, meetings.MainPanel))
        self.assertEqual(len(main.children), 6)
        self.assertTrue(all(c.custom_id.startswith("meet:main:") for c in main.children))
        i = interaction()
        i.client.meetings.store.settings.return_value = {"panel_channel_id": 11, "panel_message_id": 99}
        i.message = SimpleNamespace(id=5, flags=SimpleNamespace(ephemeral=False))
        self.assertFalse(await main.interaction_check(i))
        i.message.id = 99
        self.assertTrue(await main.interaction_check(i))
        i.message.flags.ephemeral = True
        i.message.id = 5
        self.assertTrue(await main.interaction_check(i))
        await client.close()

    async def test_primary_and_private_creation_open_different_forms(self):
        i = interaction()
        main = meetings.MainPanel()
        with patch.object(meetings.meeting_party, "open_create", new_callable=AsyncMock) as create:
            await main.create.callback(i)
            create.assert_awaited_once_with(i, i.client.meetings)
            await main.create_private.callback(i)
            create.assert_awaited_with(i, i.client.meetings, spectating=False)
        self.assertIn("기본으로 관전이 열려요", meetings.panel_embed({"enabled": True}).fields[0].value)
        self.assertIn("비활성화", meetings.panel_embed({"enabled": False}).description)

    async def test_old_admin_controls_recheck_guild_actor_and_permissions(self):
        initial = interaction(admin=True)
        menu = meetings.AdminPanel(initial)
        modal = meetings.PartySearch(initial)
        for control in (menu, modal):
            self.assertTrue(await control.interaction_check(initial))
            for i in (interaction(), interaction(admin=True, uid=8), interaction(admin=True, gid=21)):
                self.assertFalse(await control.interaction_check(i))
                self.assertTrue(i.response.send_message.await_args.kwargs["ephemeral"])

    async def test_watch_browser_shows_full_parties_without_roster(self):
        i = interaction()
        store = i.client.meetings.store
        store.settings.return_value = {"enabled": True}
        store.page.return_value = ([{"id": 89, "destination": "토요 레이드", "members": [9, 10],
            "watchers": [], "capacity": 2, "departure": 1, "strategy": "private"}], 1)
        await meetings.show_list(i, "watch")
        store.page.assert_called_once_with(20, "watch", user_id=7, query="", offset=0, limit=8)
        reply = i.response.send_message.await_args.kwargs
        self.assertTrue(reply["ephemeral"])
        self.assertIn("빈자리 0", reply["embed"].fields[0].value)
        self.assertNotIn("private", str(reply["embed"].to_dict()))
        self.assertTrue(reply["view"].next.disabled)

    async def test_list_shrink_and_admin_filter_denial(self):
        i = interaction()
        store = i.client.meetings.store
        store.settings.return_value = {"enabled": True}
        store.page.return_value = ([], 0)
        await meetings.show_list(i, "errors")
        store.page.assert_not_called()
        await meetings.show_list(i, "open", 3, edit=True)
        response = i.response.edit_message.await_args.kwargs
        self.assertIn("1/1", response["embed"].footer.text)
        self.assertTrue(response["view"].previous.disabled)
        self.assertTrue(response["view"].next.disabled)

    async def test_emoji_destination_fits_discord_selector_and_far_date_renders(self):
        i = interaction()
        store = i.client.meetings.store
        store.settings.return_value = {"enabled": True}
        departure = meetings.meeting_party.parse_departure("9999-12-31 23:59")
        store.page.return_value = ([{"id": 1, "destination": "🚆" * 100, "members": [9],
                                    "watchers": [], "capacity": 2, "departure": departure}], 1)
        await meetings.show_list(i, "open")
        reply = i.response.send_message.await_args.kwargs
        select = next(item for item in reply["view"].children if item.row == 0)
        self.assertLessEqual(len(select.options[0].label.encode("utf-16-le")), 200)
        self.assertIn("9999.12.31 23:59 KST", reply["embed"].fields[0].value)

    async def test_modal_channel_filter_and_unlimited_creation_label(self):
        i = interaction(admin=True)
        settings = dict(enabled=False, cooldown=60, max_capacity=90, max_created=0, inactive_days=30)
        settings.update({k: None for k in meetings.CHANNEL_LABELS})
        i.client.meetings.store.settings.return_value = settings
        await meetings.show_settings(i)
        reply = i.response.send_message.await_args.kwargs
        self.assertIn("무제한", reply["embed"].fields[1].value)
        self.assertIn("모집 채널 위치 사용", reply["embed"].fields[0].value)
        picker = meetings.ChannelPicker(i, "category_id")
        self.assertEqual(picker.children[0].channel_types, [discord.ChannelType.category])
        self.assertEqual(picker.children[1].label, "모집 채널 위치 사용")
        self.assertEqual(len(meetings.LimitsModal(i, settings).children), 4)
