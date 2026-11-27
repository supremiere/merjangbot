import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from discord_bot.meeting_jobs import MeetingJobs


class MeetingJobsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = SimpleNamespace(id=10, unavailable=False)
        self.store = Mock()
        self.store.settings.return_value = dict(
            panel_channel_id=45, panel_message_id=123, recruit_channel_id=46,
            promo_channel_id=47, category_id=48,
        )
        self.store.scan.return_value = []
        self.service = SimpleNamespace(store=self.store, recover=AsyncMock(), restore_panel=AsyncMock())
        self.jobs = MeetingJobs(SimpleNamespace(meetings=self.service, guilds=[self.guild]))

    async def test_first_and_following_pages_restore_healthy_and_interrupted(self):
        self.store.scan.side_effect = [
            [{"id": 51}], [{"id": i} for i in range(1, 5)], [], [{"id": 5}], [], []
        ]
        for _ in range(3):
            await self.jobs.tick_guild(self.guild)
        calls = [(c.args[1], c.kwargs["check_members"]) for c in self.service.recover.await_args_list]
        self.assertEqual(calls, [(51, False), (1, True), (2, True), (3, True), (4, True), (5, True)])
        self.assertIn(unittest.mock.call(10, after=4, limit=4), self.store.scan.call_args_list)
        self.service.restore_panel.assert_awaited_once_with(self.guild)

    async def test_deletion_is_coalesced_and_uses_guild_and_resource_ids(self):
        self.store.for_channel.return_value = 6
        self.store.for_messages.return_value = [6]
        self.jobs.resource_deleted(10, 60, (90, 91))
        self.jobs.resource_deleted(10, 60)
        self.store.for_messages.assert_called_once_with(10, 60, (90, 91))
        self.store.for_channel.assert_called_once_with(10, 60)
        await self.jobs.tick_guild(self.guild)
        self.service.recover.assert_awaited_once_with(self.guild, 6, check_members=False)

    async def test_no_repair_for_unconfigured_guild(self):
        self.store.settings.return_value.update(panel_channel_id=None, category_id=None)
        await self.jobs.tick_guild(self.guild)
        self.service.restore_panel.assert_not_called()
        self.store.scan.assert_not_called()

    async def test_failed_party_does_not_block_neighbor(self):
        self.store.scan.side_effect = [[{"id": 1}, {"id": 2}], []]
        self.service.recover.side_effect = [ValueError("unavailable"), None]
        with self.assertLogs("discord_bot.meeting_jobs", level="ERROR"):
            await self.jobs.tick_guild(self.guild)
        self.assertEqual(self.service.recover.await_count, 2)

    def test_role_changes_and_reconnect_reconcile_existing_rooms(self):
        state = self.jobs.state(10)
        state.review_due = state.panel_due = float("inf")
        self.jobs.role_changed(self.guild, after=SimpleNamespace(name="옛 역할"))
        self.assertNotIn(10, self.jobs.acl_cursors)
        self.jobs.role_changed(self.guild, before=SimpleNamespace(name="관리자"), after=SimpleNamespace(name="평회원"))
        self.assertIn(10, self.jobs.acl_cursors)
        self.jobs.reconnect()
        self.assertEqual(state.panel_due, 0)

    async def test_role_access_bypasses_public_recovery_errors(self):
        self.service.refresh_access = AsyncMock(side_effect=[ValueError("lookup"), None])
        self.store.scan.return_value = [
            {"id": 1, "room_id": None}, {"id": 2, "room_id": 81, "retry_count": 5},
            {"id": 3, "room_id": 82},
        ]
        with self.assertLogs("discord_bot.meeting_jobs", level="ERROR"):
            self.assertIsNone(await self.jobs.update_acls(self.guild, 0))
        self.assertEqual(self.service.refresh_access.await_count, 2)
        self.service.refresh_access.assert_any_await(self.guild, 2)
        self.service.refresh_access.assert_any_await(self.guild, 3)

    async def test_channel_overwrite_drift_coalesces_and_ignores_unchanged_updates(self):
        before = SimpleNamespace(overwrites={"everyone": False})
        after = SimpleNamespace(id=56, guild=self.guild, overwrites={"everyone": True})
        self.jobs.room_changed(after, after)
        self.store.for_channel.assert_not_called()
        self.store.for_channel.return_value = 19
        self.jobs.room_changed(before, after)
        self.jobs.room_changed(before, after)
        self.assertEqual(list(self.jobs.acl_targets), [(10, 19)])
        self.service.refresh_access = AsyncMock()
        self.jobs.bot.is_ready = lambda: True
        self.jobs.bot.get_guild = lambda gid: self.guild
        await self.jobs.refresh()
        self.service.refresh_access.assert_awaited_once_with(self.guild, 19)
        self.assertFalse(self.jobs.acl_targets)

    def test_activity_writes_only_for_human_in_matching_room_and_throttled(self):
        msg = SimpleNamespace(guild=self.guild, author=SimpleNamespace(bot=False),
                              channel=SimpleNamespace(id=777, topic=None))
        self.jobs.message_seen(msg)
        msg.channel.topic = "계획된 만남 · 10/7/방/abc"
        msg.author.bot = True
        self.jobs.message_seen(msg)
        msg.author.bot = False
        self.jobs.message_seen(msg)
        self.jobs.message_seen(msg)
        self.store.touch.assert_called_once_with(10, 777)
