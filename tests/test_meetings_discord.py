import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from bootstrap import create_bot
from config import Settings
from discord_bot.commands.meeting_party import MeetingButton
from discord_bot.meetings_sync import MeetingDiscordSync
from services.meetings import MeetingService, RETRY_DELAYS
from storage.database import Database
from storage.meetings import MeetingError, MeetingStore


def http_error(status, code=50013):
    response = SimpleNamespace(status=status, reason="test")
    error = discord.NotFound if status == 404 else discord.Forbidden
    return error(response, {"message": "test", "code": code})


class LocalDiscord:
    """In-process Discord transport. Uses discord.py channel specs and real overwrites."""

    def __init__(self):
        self.bot_id = 1234
        self.default_role = Mock(spec=discord.Role, id=10)
        self.default_role.name = "@everyone"
        self.admin_role = Mock(spec=discord.Role, id=11)
        self.admin_role.name = "관리자"
        self.channels = {}
        self.messages = {}
        self.next_message = 5000
        self.create_log = []
        self.members = {}
        self.guild = SimpleNamespace(
            id=10, owner_id=123, default_role=self.default_role,
            me=SimpleNamespace(id=self.bot_id),
            roles=[self.default_role, self.admin_role], create_text_channel=AsyncMock(side_effect=self.create),
            fetch_channel=AsyncMock(side_effect=self.channel), fetch_channels=AsyncMock(side_effect=lambda: list(self.channels.values())),
            fetch_member=AsyncMock(side_effect=self.member),
            audit_logs=self.audit_logs,
        )
        self.category = Mock(spec=discord.CategoryChannel, id=500, guild=self.guild)
        self.channels[self.category.id] = self.category
        for cid in (501, 502, 503):
            self.add_text(cid)

    async def audit_logs(self, **options):
        for entry in self.create_log:
            yield entry

    def channel(self, cid):
        if cid not in self.channels:
            raise http_error(404, 10003)
        return self.channels[cid]

    def member(self, uid):
        if uid not in self.members:
            raise http_error(404, 10007)
        return self.members[uid]

    def add_text(self, cid, topic=None, overwrites=None):
        channel = Mock(spec=discord.TextChannel, id=cid, guild=self.guild, topic=topic, overwrites=overwrites or {})
        channel.permissions_for.return_value = discord.Permissions(view_channel=True, read_message_history=True)
        self.channels[cid] = channel
        self.messages[cid] = messages = {}

        async def send(*, embed, view, allowed_mentions, **options):
            assert allowed_mentions.to_dict().get("parse") == []
            self.next_message += 1
            message = SimpleNamespace(
                id=self.next_message, channel=channel, author=SimpleNamespace(id=self.bot_id), webhook_id=None,
                embeds=[embed], components=[], pinned=False,
            )

            async def edit(**updates):
                message.embeds = [updates["embed"]]
                return message

            message.edit = AsyncMock(side_effect=edit)
            message.pin = AsyncMock(side_effect=lambda **opts: setattr(message, "pinned", True))
            message.delete = AsyncMock(side_effect=lambda **opts: messages.pop(message.id, None))
            messages[message.id] = message
            return message

        async def edit(**updates):
            if "overwrites" in updates:
                channel.overwrites = updates["overwrites"]
            return channel

        async def history(**kwargs):
            for message in list(messages.values()):
                yield message

        def fetch(mid):
            if mid not in messages:
                raise http_error(404, 10008)
            return messages[mid]

        channel.send = AsyncMock(side_effect=send)
        channel.delete = AsyncMock(side_effect=lambda **opts: self.channels.pop(cid, None))
        channel.fetch_message = AsyncMock(side_effect=fetch)
        channel.edit = AsyncMock(side_effect=edit)
        channel.history = history
        return channel

    async def create(self, name, **options):
        channel = self.add_text(max(self.channels) + 1, options["topic"], options["overwrites"])
        self.create_log.append(SimpleNamespace(target=channel, reason=options["reason"]))
        return channel


@pytest.fixture
def environment(tmp_path):
    db = Database(tmp_path / "meetings.db")
    db.initialize()
    store = MeetingStore(db)
    network = LocalDiscord()
    store.configure(10, 1, enabled=True, category_id=500, panel_channel_id=501, recruit_channel_id=502, promo_channel_id=503)
    ui = SimpleNamespace(
        party_embed=lambda party, surface: discord.Embed(title=party["destination"], description=party["phase"]),
        party_view=lambda party, surface: None,
        panel_embed=lambda settings: discord.Embed(title="계획된 만남"), panel_view=lambda: None,
    )
    bot = SimpleNamespace(user=SimpleNamespace(id=network.bot_id))
    adapter = MeetingDiscordSync(bot, store, ui)
    pid = store.create(10, 100, destination="고정 공대", capacity=2)["id"]
    return store, network, adapter, pid


def test_room_permissions_changes_and_repair_never_duplicate_resources(environment):
    async def exercise():
        store, network, adapter, pid = environment
        guild = network.guild

        async def sync():
            await adapter.sync(guild, store.get(10, pid))
            return store.get(10, pid)

        state = await sync()
        room = network.channels[state["room_id"]]
        assert network.messages[room.id][state["guide_id"]].pinned
        await sync()
        guild.create_text_channel.assert_awaited_once()
        assert len(network.messages[502]) == len(network.messages[room.id]) == 1
        assert state["phase"] == "ready"
        store.mutate(10, pid, 101, False, "join")
        store.mutate(10, pid, 102, False, "watch")
        await adapter.sync_access(guild, store.get(10, pid))
        by_id = {target.id: value for target, value in room.overwrites.items()}
        assert by_id[10].view_channel is False
        assert by_id[11].manage_messages and by_id[11].pin_messages
        assert by_id[101].view_channel and by_id[101].send_messages
        assert by_id[102].read_message_history and by_id[102].send_messages
        assert not by_id[102].manage_messages
        store.mutate(10, pid, 100, False, "close_watch")
        network.admin_role.name = "이전 관리자"
        await sync()
        assert {target.id for target in room.overwrites} == {10, network.bot_id, 100, 101}
        recruit = network.messages[502].pop(state["recruit_id"])
        assert (await sync())["recruit_id"] != recruit.id
        del network.channels[room.id]
        recovered = await sync()
        assert len(network.messages[502]) == 1
        assert network.messages[recovered["room_id"]][recovered["guide_id"]].pinned
        await adapter.sync_panel(guild, store.settings(10))
        await adapter.sync_panel(guild, store.settings(10))
        assert len(network.messages[501]) == 1

    asyncio.run(exercise())


def test_unknown_send_recovers_original_advertisement_and_preserves_cooldown(environment):
    async def exercise():
        store, network, adapter, pid = environment
        await adapter.sync(network.guild, store.get(10, pid))
        state = store.mutate(10, pid, 100, False, "promote")
        channel = network.channels[503]
        original_send = channel.send.side_effect

        async def lost_response(**options):
            await original_send(**options)
            raise TimeoutError("after the Discord server accepted the request")

        channel.send.side_effect = lost_response
        with pytest.raises((TimeoutError, ValueError)):
            await adapter.promote(network.guild, state)
        assert len(network.messages[503]) == 1
        assert store.get(10, pid)["last_promo"] is None
        channel.send.side_effect = original_send
        await adapter.promote(network.guild, store.get(10, pid))
        assert len(network.messages[503]) == 1 and channel.send.await_count == 1
        after = store.get(10, pid)
        assert after["last_promo"] and not after["promo_token"]
        assert after["ads"][0]["message_id"] in network.messages[503]

    asyncio.run(exercise())


def test_explicit_disband_only_deletes_owned_room_and_is_retryable(environment):
    async def exercise():
        store, network, adapter, pid = environment
        await adapter.sync(network.guild, store.get(10, pid))
        state = store.get(10, pid)
        room = network.channels[state["room_id"]]
        marker = room.topic
        room.topic = "different party"
        store.mutate(10, pid, 100, False, "disband")
        with pytest.raises(ValueError):
            await adapter.sync(network.guild, store.get(10, pid))
        assert room.id in network.channels and not room.delete.called
        assert store.get(10, pid)["phase"] == "disbanding"
        room.topic = marker
        await adapter.sync(network.guild, store.get(10, pid))
        await adapter.sync(network.guild, store.get(10, pid))
        room.delete.assert_awaited_once()
        assert store.get(10, pid)["phase"] == "disbanded"

    asyncio.run(exercise())


def interaction(network, uid, *, admin=False, request_id=10000):
    member = SimpleNamespace(id=uid, bot=False, guild=network.guild,
                             guild_permissions=discord.Permissions.none(),
                             roles=[network.admin_role] if admin else [])
    network.members[uid] = member
    return SimpleNamespace(id=request_id, user=member, guild=network.guild)


def test_recovery_only_removes_a_roster_after_discord_confirms_unknown_member(environment):
    async def exercise():
        store, network, adapter, pid = environment
        adapter.bot.intents = discord.Intents.none()
        service = MeetingService(adapter.bot, store, adapter)
        await adapter.sync(network.guild, store.get(10, pid))
        store.mutate(10, pid, 100, False, "roster", target=100, character="일곱")
        response = SimpleNamespace(status=404, reason="test")
        network.guild.fetch_member.side_effect = discord.NotFound(response, {"code": 10004, "message": "Unknown Guild"})
        with pytest.raises(MeetingError):
            await service.recover(network.guild, pid, manual=True, check_members=True)
        preserved = store.get(10, pid)
        assert preserved["leader_id"] == 100 and preserved["roster"][100]["character"] == "일곱"
        network.guild.fetch_member.side_effect = discord.NotFound(response, {"code": 10007, "message": "Unknown Member"})
        recovered = await service.recover(network.guild, pid, manual=True, check_members=True)
        assert recovered["members"] == [] and recovered["roster"] == {}
        assert recovered["leader_id"] is None and recovered["closed"]
        assert recovered["room_id"] == preserved["room_id"]

    asyncio.run(exercise())


def test_membership_retry_survives_updates_restart_and_interrupted_recheck(environment):
    async def exercise():
        store, network, adapter, pid = environment
        adapter.bot.intents = discord.Intents.none()
        service = MeetingService(adapter.bot, store, adapter)
        now = [1_800_000_000]
        store.clock = lambda: now[0]
        leader = interaction(network, 100)
        await adapter.sync(network.guild, store.get(10, pid))
        await service.act(interaction(network, 101), pid, "join")
        await service.act(interaction(network, 102), pid, "watch")
        old = store.get(10, pid)
        network.messages[502].pop(old["recruit_id"])
        del network.members[101]

        def verify(uid):
            if uid == 102:
                raise TimeoutError()
            return network.member(uid)

        network.guild.fetch_member.side_effect = verify
        with pytest.raises(MeetingError, match="구성원 확인"):
            await service.recover(network.guild, pid, check_members=True)
        partial = store.get(10, pid)
        assert partial["members"] == [100] and partial["watchers"] == [102]
        assert 101 not in {target.id for target in network.channel(old["room_id"]).overwrites}
        assert partial["recruit_id"] != old["recruit_id"] and partial["recruit_id"] in network.messages[502]
        assert partial["member_check"]["failures"] == 1
        assert "구성원 확인" in partial["error"]
        assert store.stats(10)["errors"] == store.page(10, "errors")[1] == store.page(10, "recovery")[1] == 1
        assert not store.scan(10, dirty_only=True)  # Resource repair succeeded; membership retry is not due.
        updated = await service.act(leader, pid, "edit", destination="토요일")
        assert updated["member_check"] == partial["member_check"] and updated["error"]

        now[0] += RETRY_DELAYS[0]
        assert [p["id"] for p in store.scan(10, dirty_only=True)] == [pid]
        store.db.initialize()
        restored = MeetingStore(store.db, clock=store.clock)
        service = MeetingService(adapter.bot, restored, MeetingDiscordSync(adapter.bot, restored, adapter.ui))
        network.guild.fetch_member.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await service.recover(network.guild, pid, check_members=False)
        assert restored.get(10, pid)["member_check"] == partial["member_check"]
        network.guild.fetch_member.side_effect = network.member
        final = await service.recover(network.guild, pid, check_members=False)
        assert final["member_check"] is None and not final["error"]
        assert final["watchers"] == [102] and final["members"] == [100]
        assert not restored.scan(10, dirty_only=True) and restored.stats(10)["errors"] == 0

    asyncio.run(exercise())


def test_member_verification_stops_after_retry_budget_and_manual_repair_can_resume(environment):
    async def exercise():
        store, network, adapter, pid = environment
        adapter.bot.intents = discord.Intents.none()
        service = MeetingService(adapter.bot, store, adapter)
        now = [1_800_000_000]
        store.clock = lambda: now[0]
        leader = interaction(network, 100)
        await adapter.sync(network.guild, store.get(10, pid))
        network.guild.fetch_member.side_effect = TimeoutError()
        for delay in (*RETRY_DELAYS, None):
            with pytest.raises(MeetingError, match="구성원 확인"):
                await service.recover(network.guild, pid, check_members=True)
            if delay is not None:
                now[0] += delay
        before = network.guild.fetch_member.await_count
        assert store.get(10, pid)["member_check"]["retry_at"] is None
        now[0] += 10 * 86_400
        await service.act(leader, pid, "edit", destination="여전히 유지되는 파티")
        await service.recover(network.guild, pid, check_members=True)
        assert network.guild.fetch_member.await_count == before and not store.scan(10, dirty_only=True)
        network.guild.fetch_member.side_effect = network.member
        final = await service.recover(network.guild, pid, manual=True)
        assert final["member_check"] is None and final["members"] == [100]

    asyncio.run(exercise())


def test_failed_acl_admission_rolls_back_but_failed_public_card_preserves_real_access(environment):
    async def exercise():
        store, network, adapter, pid = environment
        service = MeetingService(adapter.bot, store, adapter)
        await adapter.sync(network.guild, store.get(10, pid))
        before = store.get(10, pid)
        room = network.channel(before["room_id"])
        actual_edit = room.edit.side_effect
        room.edit.side_effect = http_error(403)
        watcher = interaction(network, 102)
        for action in ("watch", "join"):
            with pytest.raises(MeetingError):
                await service.act(watcher, pid, action)
            state = store.get(10, pid)
            assert state["members"] == [100] and state["watchers"] == []
            assert state["dirty"] and state["error"]
        room.edit.side_effect = actual_edit
        await service.act(watcher, pid, "watch")
        # A failed watcher-to-player conversion must restore the original role.
        # Existing chat permissions on both sides are equal, so simulate a failed
        # authoritative channel lookup, rather than an overwrite edit.
        real_fetch = network.guild.fetch_channel.side_effect
        network.guild.fetch_channel.side_effect = http_error(403)
        with pytest.raises(MeetingError):
            await service.act(watcher, pid, "join")
        assert store.get(10, pid)["watchers"] == [102]
        network.guild.fetch_channel.side_effect = real_fetch
        display = network.channel(502)
        fetch = display.fetch_message.side_effect
        display.fetch_message.side_effect = http_error(403)
        actual = await service.act(watcher, pid, "join")
        assert actual["members"] == [100, 102] and actual["watchers"] == []
        assert actual["sync_warning"] and actual["dirty"]
        assert any(target.id == 102 and overwrites.send_messages for target, overwrites in room.overwrites.items())
        display.fetch_message.side_effect = fetch
        repaired = await service.recover(network.guild, pid, manual=True)
        assert repaired["members"] == [100, 102] and not repaired["dirty"]
        assert not repaired["error"]

    asyncio.run(exercise())


def test_live_authorization_admin_without_seat_and_banned_members(environment):
    async def exercise():
        store, network, adapter, pid = environment
        service = MeetingService(adapter.bot, store, adapter)
        await adapter.sync(network.guild, store.get(10, pid))
        admin = interaction(network, 200, admin=True)
        user = interaction(network, 101)
        assert service.require_access(admin, pid)["members"] == [100]
        with pytest.raises(MeetingError):
            service.require_access(user, pid)
        await service.act(user, pid, "watch")
        assert service.require_access(user, pid)["watchers"] == [101]
        with pytest.raises(MeetingError):
            service.require_member(user, pid)
        await service.act(admin, pid, "kick", target=101)
        for role in ("watch", "join"):
            with pytest.raises(MeetingError):
                await service.act(user, pid, role)
        await service.act(admin, pid, "allow", target=101)
        await service.act(admin, pid, "join")
        await service.act(admin, pid, "leave")
        assert service.require_access(admin, pid)["members"] == [100]
        admin.user.roles.clear()
        with pytest.raises(MeetingError):
            service.require_access(admin, pid)
        with pytest.raises(MeetingError):
            await service.act(admin, pid, "disband")
        outsider = interaction(network, 100)
        outsider.guild = SimpleNamespace(id=999)
        with pytest.raises(MeetingError):
            await service.act(outsider, pid, "close_watch")
        assert store.get(10, pid)["phase"] == "ready"

    asyncio.run(exercise())


def test_interrupted_creation_and_duplicate_create_after_restart(environment):
    async def exercise():
        store, network, adapter, _ = environment
        service = MeetingService(adapter.bot, store, adapter)
        creator = interaction(network, 301)
        recruit = network.channel(502)
        send = recruit.send.side_effect
        recruit.send.side_effect = http_error(403)
        with pytest.raises(MeetingError):
            await service.create(creator, destination="며칠 뒤 출발", capacity=4)
        targets, total = store.page(10, "mine", user_id=301)
        assert total == 1
        unfinished, = targets
        assert unfinished["phase"] == "creating" and unfinished["room_id"]
        assert unfinished["dirty"] and unfinished["error"]
        recruit.send.side_effect = send
        restarted = MeetingService(adapter.bot, store, MeetingDiscordSync(adapter.bot, store, adapter.ui))
        state = await restarted.recover(network.guild, unfinished["id"], manual=True)
        assert state["phase"] == "ready" and not state["error"]
        retried = await restarted.create(creator, destination="며칠 뒤 출발", capacity=4)
        assert retried["id"] == state["id"] and state["room_id"] == unfinished["room_id"]
        assert len(network.messages[502]) == 1
        network.guild.create_text_channel.assert_awaited_once()

    asyncio.run(exercise())


def test_join_and_close_on_same_party_serialize_while_another_party_remains_usable(environment):
    async def exercise():
        store, network, adapter, pid = environment
        service = MeetingService(adapter.bot, store, adapter)
        await adapter.sync(network.guild, store.get(10, pid))
        second = await service.create(interaction(network, 333), destination="다른 파티", capacity=3)
        admitted = asyncio.Event()
        resume = asyncio.Event()
        original_access = adapter.sync_access

        async def pause_first(guild, party):
            if party["id"] == pid:
                admitted.set()
                await resume.wait()
            return await original_access(guild, party)

        adapter.sync_access = pause_first
        joined = asyncio.create_task(service.act(interaction(network, 101), pid, "join"))
        await admitted.wait()
        close = asyncio.create_task(service.act(interaction(network, 100), pid, "close"))
        another = await service.act(interaction(network, 102), second["id"], "join")
        assert another["members"] == [333, 102] and not close.done()
        resume.set()
        await asyncio.gather(joined, close)
        final = store.get(10, pid)
        assert final["members"] == [100, 101] and final["closed"]
        for uid in (101, 103):
            with pytest.raises(MeetingError):
                await service.act(interaction(network, uid), pid, "join")

    asyncio.run(exercise())


@pytest.mark.parametrize("left_server", [False, True])
def test_waiting_admin_is_rechecked_after_losing_role_or_leaving_guild(environment, left_server):
    async def exercise():
        store, network, adapter, pid = environment
        service = MeetingService(adapter.bot, store, adapter)
        await adapter.sync(network.guild, store.get(10, pid))
        stale = interaction(network, 200, admin=True)
        # Model an already-issued interaction object that retains its old role list.
        async with service._lock(10, pid):
            queued = asyncio.create_task(service.act(stale, pid, "disband"))
            await asyncio.sleep(0)
            if left_server:
                del network.members[200]
            else:
                interaction(network, 200, admin=False)
            assert stale.user.roles == [network.admin_role]
        with pytest.raises(MeetingError):
            await queued
        network.guild.fetch_member.assert_awaited_once_with(200)
        assert store.get(10, pid)["phase"] == "ready"

    asyncio.run(exercise())


@pytest.mark.parametrize(("code", "feedback"), [(10007, "참가한 사용자"), (10004, "권한을 확인하지 못했어요")])
def test_queued_member_lookup_distinguishes_unknown_guild_from_unknown_member(code, feedback):
    guild = SimpleNamespace(fetch_member=AsyncMock(side_effect=http_error(404, code)))
    with pytest.raises(MeetingError, match=feedback):
        asyncio.run(MeetingService._queued_actor(guild, SimpleNamespace(id=100), True))


def test_admin_click_arriving_during_fifo_lock_handoff_uses_current_role(environment):
    async def exercise():
        store, network, adapter, pid = environment
        service = MeetingService(adapter.bot, store, adapter)
        await adapter.sync(network.guild, store.get(10, pid))
        stale = interaction(network, 200, admin=True)

        async def earlier_waiter():
            async with service._lock(10, pid):
                interaction(network, 200, admin=False)

        async with service._lock(10, pid):
            previous = asyncio.create_task(earlier_waiter())
            await asyncio.sleep(0)
            assert not previous.done()
        # The releasing task continues immediately, before the earlier waiter runs.
        # asyncio.Lock is momentarily unlocked, but FIFO still queues act behind it.
        with pytest.raises(MeetingError):
            await service.act(stale, pid, "disband")
        await previous
        network.guild.fetch_member.assert_awaited_once_with(200)
        assert store.get(10, pid)["phase"] == "ready"

    asyncio.run(exercise())


def test_existing_bot_startup_restores_train_and_party_routes_together_without_login(tmp_path):
    async def exercise():
        settings = Settings(token="", notice_channel_id=1, abyss_channel_id=2, server_status_channel_id=3,
                            db_file=tmp_path / "integrated.db")
        pid = None
        for startup in range(2):
            bot = create_bot(settings)
            # Keep real setup_hook, SQLite, command tree and Discord view dispatch.
            # Mock the unrelated network clients and background job starts only.
            bot.abyss.refresh = AsyncMock()
            bot.http_client.start = AsyncMock()
            bot.add_dynamic_items = Mock(wraps=bot.add_dynamic_items)
            for job in bot.jobs:
                job.start = Mock()
            try:
                await bot.setup_hook()
                names = {cmd.name for cmd in bot.tree.get_commands()}
                assert {"계획된만남", "열차출발", "어비스"} <= names
                buttons = [item.custom_id for view in bot.persistent_views for item in view.children]
                assert any(s.startswith("train:") for s in buttons)
                assert "meet:main:mine" in buttons
                bot.add_dynamic_items.assert_any_call(MeetingButton)
                store = bot.meetings.store
                if startup == 0:
                    store.configure(10, 100, enabled=True)
                    pid = store.create(10, 100, destination="유지되는 파티", capacity=2)["id"]
                    store.refs(10, pid, phase="ready", room_id=600)
                    store.mutate(10, pid, 101, False, "watch")
                else:
                    assert store.get(10, pid)["watchers"] == [101]
            finally:
                await bot.close()

    asyncio.run(exercise())
