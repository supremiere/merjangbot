import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from discord_bot.meetings_sync import (
    MeetingAccessError, MeetingDiscordSync, ResourceError, _marker, room_overwrites,
)
from storage.database import Database


async def records(*items):
    for item in items:
        yield item


def missing(code):
    return discord.NotFound(SimpleNamespace(status=404, reason="gone"), dict(code=code, message="not found"))


def role(role_id, name):
    obj = MagicMock(spec=discord.Role)
    obj.id, obj.name = role_id, name
    return obj


def text_channel(guild, channel_id):
    channel = MagicMock(spec=discord.TextChannel)
    channel.guild, channel.id = guild, channel_id
    return channel


class SyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "system.db")
        with self.db.connect() as conn:
            conn.execute("""CREATE TABLE meeting_discord_sends
                        (marker TEXT PRIMARY KEY,channel_id INTEGER,attempted_at INTEGER)""")
        self.store = MagicMock()
        self.store.db = self.db
        self.guild = MagicMock(spec=discord.Guild)
        self.guild.id, self.guild.owner_id = 10, 100
        self.guild.me.guild_permissions = discord.Permissions.all()
        self.channel_list = AsyncMock(return_value=[])
        self.guild._state = SimpleNamespace(http=SimpleNamespace(get_all_guild_channels=self.channel_list))
        everyone, self.admin_role = role(10, "@everyone"), role(20, "관리자")
        self.guild.default_role = everyone
        self.guild.roles = [everyone, self.admin_role, role(28, "길드원")]
        self.ui = MagicMock()
        self.ui.party_embed.side_effect = lambda party, surface: discord.Embed(title=surface)
        self.ui.party_view.return_value = None
        self.bot = SimpleNamespace(user=SimpleNamespace(id=99))
        self.sync = MeetingDiscordSync(self.bot, self.store, self.ui)
        self.party = dict(id=1, guild_id=10, nonce="unique", room_id=555, guide_id=None,
                          members=[11, 12], watchers=[13], destination="테스트", phase="ready")
        self.room = text_channel(self.guild, self.party["room_id"])
        self.room.topic = self.sync._room_marker(self.party)

    def message(self, channel, identity, embed):
        message = MagicMock(spec=discord.Message)
        message.author, message.channel, message.id = self.bot.user, channel, identity
        message.embeds, message.components, message.webhook_id = [embed], [], None
        message.edit.return_value = message
        return message

    async def test_joined_watcher_permissions_and_role_changes(self):
        initial = room_overwrites(self.guild, 99, [11, 12], [13])
        by_id = {target.id: perms for target, perms in initial.items()}
        self.assertFalse(by_id[10].view_channel)
        self.assertTrue(by_id[13].view_channel and by_id[13].read_message_history and by_id[13].send_messages)
        self.assertIsNone(by_id[13].manage_messages)
        self.assertTrue(by_id[20].pin_messages)
        self.assertTrue(by_id[99].manage_roles)
        self.assertNotIn(28, by_id)
        self.room.overwrites = initial
        self.guild.fetch_channel.return_value = self.room
        await self.sync.sync_access(self.guild, self.party)
        self.room.edit.assert_not_awaited()
        self.party["watchers"] = []
        self.admin_role.name = "이전 관리자"
        self.guild.roles.append(role(32, "머장"))
        await self.sync.sync_access(self.guild, self.party)
        new = {target.id: perms for target, perms in self.room.edit.await_args.kwargs["overwrites"].items()}
        self.assertEqual(set(new), {10, 11, 12, 32, 99})
        self.assertTrue(new[32].manage_messages)

    async def test_existing_restricted_room_retains_its_denies_after_bot_permissions_change(self):
        self.guild.me.guild_permissions = discord.Permissions(manage_channels=True, manage_roles=True)
        original = discord.PermissionOverwrite(manage_roles=False, manage_webhooks=False)
        old_bot = discord.PermissionOverwrite(manage_roles=True)
        self.room.overwrites_for.side_effect = lambda target: original if target is self.guild.default_role else old_bot
        grants = room_overwrites(self.guild, 99, [11], [13], room=self.room)
        self.assertIs(grants[self.guild.default_role].manage_roles, False)
        self.assertIs(grants[self.guild.default_role].manage_webhooks, False)
        self.assertIs(grants[discord.Object(99, type=discord.User)].manage_roles, True)

    async def test_discordpy_encodes_roles_and_uncached_watchers_on_creation_and_edit(self):
        grants = room_overwrites(self.guild, 99, [11], [13])
        raw = {"id": "555", "name": "party", "type": 0, "position": 0, "permission_overwrites": []}
        http = SimpleNamespace(create_channel=AsyncMock(return_value=raw), edit_channel=AsyncMock(return_value=raw))
        self.guild._state = SimpleNamespace(http=http)
        await discord.Guild._create_channel(self.guild, "party", channel_type=discord.ChannelType.text,
                                            overwrites=grants)
        real_channel = discord.TextChannel(state=self.guild._state, guild=self.guild, data=raw)
        await real_channel.edit(overwrites=grants)
        for send in (http.create_channel, http.edit_channel):
            rows = {row["id"]: row for row in send.await_args.kwargs["permission_overwrites"]}
            self.assertEqual(rows[10]["type"], 0)
            self.assertEqual(rows[20]["type"], 0)
            self.assertEqual(rows[13]["type"], 1)
            self.assertTrue(discord.Permissions(rows[10]["deny"]).view_channel)
            watcher = discord.Permissions(rows[13]["allow"])
            self.assertTrue(watcher.read_message_history and watcher.send_messages)
            self.assertFalse(watcher.manage_channels or watcher.manage_roles or watcher.manage_messages)

    async def test_known_room_with_wrong_marker_is_never_touched(self):
        self.guild.fetch_channel.return_value = self.room
        self.room.topic = "unrelated"
        with self.assertRaises(MeetingAccessError):
            await self.sync.sync_access(self.guild, self.party)
        self.room.edit.assert_not_awaited()
        self.room.delete.assert_not_awaited()
        with self.assertRaises(ResourceError):
            await self.sync.sync_access(SimpleNamespace(id=98), self.party)

    async def test_many_watchers_can_enter_until_actual_channel_overwrite_limit(self):
        many = range(10000, 10997)
        self.assertEqual(len(room_overwrites(self.guild, 99, [], many)), 1000)
        with self.assertRaisesRegex(ResourceError, "1,000"):
            room_overwrites(self.guild, 99, [2000], many)

    async def test_adopting_idless_room_requires_matching_creator_audit_record(self):
        self.party["room_id"] = None
        self.channel_list.return_value = [dict(id="555", type=0, topic=self.room.topic)]
        self.guild.fetch_channel.return_value = self.room
        self.guild.audit_logs.return_value = records()
        with self.assertRaises(MeetingAccessError):
            await self.sync._locate_room(self.guild, self.party)
        self.store.refs.assert_not_called()
        self.guild.audit_logs.return_value = records(SimpleNamespace(target=self.room, reason=self.room.topic))
        self.assertIs(await self.sync._locate_room(self.guild, self.party), self.room)
        self.assertEqual(self.guild.audit_logs.call_args.kwargs["user"].id, 99)
        self.store.refs.assert_called_once_with(10, 1, room_id=555, guide_id=None)

    async def test_audited_room_with_changed_topic_is_not_adopted(self):
        self.party["room_id"] = None
        marker = self.room.topic
        self.channel_list.return_value = [dict(id="555", type=0, topic=marker)]
        self.guild.audit_logs.return_value = records(SimpleNamespace(target=self.room, reason=marker))
        self.room.topic = "unrelated"
        self.guild.fetch_channel.return_value = self.room
        with self.assertRaises(MeetingAccessError):
            await self.sync._ensure_room(self.guild, self.party, create=True)
        self.store.refs.assert_not_called()
        self.guild.create_text_channel.assert_not_awaited()

    async def test_channel_inventory_failure_cannot_trigger_room_creation(self):
        self.party["room_id"] = None
        self.channel_list.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await self.sync._ensure_room(self.guild, self.party, create=True)
        self.guild.create_text_channel.assert_not_awaited()

    async def test_sdk_duplicate_create_blocks_publication_and_disband_until_reconciled(self):
        self.guild.fetch_channel.return_value = self.room
        extra = text_channel(self.guild, 556)
        extra.topic = self.room.topic
        self.channel_list.return_value = [dict(id=str(c.id), type=0, topic=c.topic) for c in (self.room, extra)]
        self.room.overwrites = room_overwrites(self.guild, 99, self.party["members"], self.party["watchers"])
        self.party.update(phase="creating", promo_token=None, ads=[])
        self.sync._public = AsyncMock()
        with self.assertRaisesRegex(MeetingAccessError, "여러 개"):
            await self.sync.sync(self.guild, self.party)
        self.sync._public.assert_not_awaited()
        self.party["phase"] = "disbanding"
        with self.assertRaisesRegex(MeetingAccessError, "여러 개"):
            await self.sync.sync(self.guild, self.party)
        self.room.delete.assert_not_awaited()
        extra.delete.assert_not_awaited()
        self.store.refs.assert_not_called()

    async def test_send_timeout_reconciles_post_at_original_time(self):
        channel = text_channel(self.guild, 33)
        marker = _marker(10, 1, "홍보", "T1")
        payload = self.sync._payload(self.party, "ad", marker)
        posted = self.message(channel, 77, payload["embed"])
        channel.send.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await self.sync._ensure_message(channel, None, marker, payload)
        recreated = MeetingDiscordSync(self.bot, self.store, self.ui)
        channel.history.return_value = records(posted)
        recovered = await recreated._ensure_message(channel, None, marker, payload)
        self.assertEqual(recovered.id, 77)
        self.assertEqual(channel.send.await_count, 1)
        sent = channel.send.await_args.kwargs
        self.assertIsInstance(sent["nonce"], int)
        self.assertFalse(sent["allowed_mentions"].everyone)
        recreated._confirmed(marker)
        self.assertIsNone(recreated._attempt(marker))

    async def test_full_uncertain_send_window_prevents_duplicate(self):
        marker = _marker(10, 1, "안내")
        payload = self.sync._payload(self.party, "room", marker)
        stranger = self.message(self.room, 44, discord.Embed(title="other"))
        self.sync._mark_sending(marker, self.room.id)
        self.room.history.return_value = records(*([stranger] * 1001))
        with self.assertRaisesRegex(ResourceError, "재발송"):
            await self.sync._ensure_message(self.room, None, marker, payload)
        self.room.send.assert_not_awaited()

    async def test_known_deleted_card_allocates_another_nonce(self):
        marker = _marker(10, 1, "모집")
        payload = self.sync._payload(self.party, "public", marker)
        await self.sync._ensure_message(self.room, None, marker, payload)
        initial = self.room.send.await_args.kwargs["nonce"]
        self.sync._confirmed(marker)
        self.room.fetch_message.side_effect = missing(10008)
        await self.sync._ensure_message(self.room, 5000, marker, payload)
        replaced = self.room.send.await_args.kwargs["nonce"]
        self.assertNotEqual(initial, replaced)

    async def test_unknown_guild_on_delete_preserves_room_reference_and_pending_disband(self):
        self.guild.fetch_channel.return_value = self.room
        self.room.delete.side_effect = missing(10004)
        self.party.update(phase="disbanding", promo_token=None, activity="ended", ads=[])
        self.sync._public = AsyncMock()
        with self.assertRaises(discord.NotFound):
            await self.sync.sync(self.guild, self.party)
        self.store.refs.assert_not_called()

    async def test_delete_404_with_channel_still_listed_preserves_pending_disband(self):
        self.guild.fetch_channel.return_value = self.room
        self.channel_list.return_value = [dict(id="555", type=0, topic=self.room.topic)]
        self.room.delete.side_effect = missing(10003)
        self.party.update(phase="disbanding", promo_token=None, activity="ended", ads=[])
        self.sync._public = AsyncMock()
        with self.assertRaisesRegex(ResourceError, "해산을 보류"):
            await self.sync.sync(self.guild, self.party)
        self.store.refs.assert_not_called()

    async def test_unknown_guild_on_lookup_cannot_trigger_replacement_or_ad_pruning(self):
        self.guild.fetch_channel.side_effect = missing(10004)
        self.party["ads"] = [dict(channel_id=39, message_id=91)]
        with self.assertRaises(discord.NotFound):
            await self.sync._ads(self.guild, self.party)
        with self.assertRaises(discord.NotFound):
            await self.sync._ensure_room(self.guild, self.party, create=True)
        self.store.refs.assert_not_called()
        self.guild.create_text_channel.assert_not_awaited()
        self.channel_list.assert_not_awaited()

    async def test_inconsistent_channel_lookup_does_not_retire_or_replace_a_room(self):
        self.guild.fetch_channel.side_effect = missing(10003)
        self.channel_list.return_value = [dict(id="555", type=20)]
        with self.assertRaisesRegex(ResourceError, "서버 목록"):
            await self.sync._ensure_room(self.guild, self.party, create=True)
        self.guild.create_text_channel.assert_not_awaited()
        self.store.refs.assert_not_called()

    async def test_message_lookup_rejects_non_message_404s(self):
        marker = _marker(10, 1, "모집")
        payload = self.sync._payload(self.party, "public", marker)
        for code in (0, 10003, 10004):
            self.room.fetch_message.side_effect = missing(code)
            with self.assertRaises(discord.NotFound):
                await self.sync._ensure_message(self.room, 67, marker, payload)
        self.room.send.assert_not_awaited()

    async def test_hidden_history_cannot_release_or_repeat_uncertain_post(self):
        marker = _marker(10, 1, "홍보", "T1")
        self.sync._mark_sending(marker, self.room.id)
        self.room.permissions_for.return_value = discord.Permissions(view_channel=True, read_message_history=False)
        self.room.history.return_value = records()
        with self.assertRaisesRegex(ResourceError, "기록 보기"):
            await self.sync._ensure_message(self.room, None, marker, self.sync._payload(self.party, "ad", marker))
        self.assertIsNotNone(self.sync._attempt(marker))
        self.room.send.assert_not_awaited()

    async def test_queued_post_after_ten_minutes_is_recovered_before_retry(self):
        marker = _marker(10, 1, "홍보", "T1")
        payload = self.sync._payload(self.party, "ad", marker)
        posted = self.message(self.room, 67, payload["embed"])
        self.sync._mark_sending(marker, self.room.id)
        sent = datetime.fromtimestamp(self.sync._attempt(marker)[1], timezone.utc) + timedelta(minutes=15)

        async def history(*, after, before=None, **kwargs):
            if sent > after and (before is None or sent < before):
                yield posted
        self.room.history.side_effect = history
        recovered = await self.sync._ensure_message(self.room, None, marker, payload)
        self.assertEqual(recovered.id, 67)
        self.room.send.assert_not_awaited()

    async def test_recovered_reservation_does_not_publish_after_last_seat_or_disable(self):
        self.party.update(promo_token="abc", promo_channel_id=22, activity="planned", closed=False,
                          capacity=2, ads=[])
        self.store.get.side_effect = lambda *args: dict(self.party, promo_token=None)
        self.store.settings.return_value = {"enabled": True}
        await self.sync.promote(self.guild, self.party)
        self.store.finish_promo.assert_called_once_with(10, 1, "abc")
        self.guild.fetch_channel.assert_not_awaited()
        self.party.update(capacity=5, promo_token="def")
        self.store.settings.return_value = {"enabled": False}
        await self.sync.promote(self.guild, self.party)
        self.store.finish_promo.assert_called_with(10, 1, "def")
        self.guild.fetch_channel.assert_not_awaited()

    async def test_ad_compaction_keeps_newest_and_refuses_foreign_messages(self):
        channel = text_channel(self.guild, 40)
        self.guild.fetch_channel.return_value = channel
        marker = _marker(10, 1, "홍보", "T")
        old, new = [self.message(channel, i, self.sync._payload(self.party, "ad", marker)["embed"])
                    for i in (45, 46)]
        refs = [{"channel_id":40, "message_id":i} for i in (45, 46)]
        self.party["ads"] = refs.copy()
        channel.fetch_message.side_effect = [new, old]
        await self.sync._ads(self.guild, self.party)
        old.delete.assert_awaited_once()
        new.delete.assert_not_awaited()
        self.assertEqual(self.party["ads"], refs[1:])
        new.author = SimpleNamespace(id=222)
        channel.fetch_message.side_effect = [new]
        with self.assertRaises(ResourceError):
            await self.sync._ads(self.guild, self.party)
        new.delete.assert_not_awaited()
        new.edit.assert_not_awaited()

    async def test_missing_newest_ad_keeps_the_remaining_verified_post(self):
        channel = text_channel(self.guild, 40)
        self.guild.fetch_channel.return_value = channel
        marker = _marker(10, 1, "홍보", "older")
        old = self.message(channel, 45, self.sync._payload(self.party, "ad", marker)["embed"])
        refs = [{"channel_id": 40, "message_id": i} for i in (45, 46)]
        self.party["ads"] = refs.copy()
        channel.fetch_message.side_effect = [missing(10008), old]
        await self.sync._ads(self.guild, self.party)
        self.assertEqual(self.party["ads"], refs[:1])
        old.delete.assert_not_awaited()

    async def test_disband_confirmed_ref_only_and_no_room_on_ended(self):
        self.guild.fetch_channel.return_value = self.room
        self.party.update(phase="disbanding", promo_token=None, activity="ended", ads=[])
        self.sync._public = AsyncMock()
        await self.sync.sync(self.guild, self.party)
        self.room.delete.assert_awaited_once()
        self.assertEqual(self.sync._public.await_args.args[1]["phase"], "disbanded")
        self.store.refs.assert_called_with(10, 1, phase="disbanded", room_id=None, guide_id=None,
                                           error="", dirty=False)
        self.party["phase"] = "disbanded"
        await self.sync.sync(self.guild, self.party)
        self.room.delete.assert_awaited_once()
        self.party.update(phase="ready")
        self.sync._ensure_room = AsyncMock(return_value=self.room)
        self.sync._guide = AsyncMock()
        await self.sync.sync(self.guild, self.party)
        self.room.delete.assert_awaited_once()
