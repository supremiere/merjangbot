"""Repair Discord resources against durable meeting state, never by party age."""
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from discord.ext import tasks

from discord_bot.commands.trains import TRAIN_ADMIN_ROLES

logger = logging.getLogger(__name__)


@dataclass
class GuildSweep:
    dirty_cursor: int = 0
    review_cursor: int = 0
    review_due: float = 0
    panel_due: float = 0
    panel_failures: int = 0
    targets: dict = field(default_factory=dict)

    def request_review(self):
        self.review_cursor = 0
        self.review_due = 0


class MeetingJobs:
    def __init__(self, bot):
        self.bot = bot
        self.service = bot.meetings
        self.states = {}
        self.activity_writes = OrderedDict()
        self.guild_cursor = 0
        self.acl_cursors = OrderedDict()
        self.repeat_acl = set()
        self.acl_targets = {}

    def state(self, gid):
        return self.states.setdefault(gid, GuildSweep())

    def reconnect(self):
        for state in self.states.values():
            state.request_review()
            state.panel_due = state.panel_failures = 0
        for guild in self.bot.guilds:
            self.request_acl(guild.id)

    def forget(self, gid):
        self.states.pop(gid, None)
        self.acl_cursors.pop(gid, None)
        self.repeat_acl.discard(gid)

    def request_acl(self, gid):
        if gid in self.acl_cursors:
            # Finish the current sweep fairly, then cover already-visited rooms.
            self.repeat_acl.add(gid)
        else:
            self.acl_cursors[gid] = 0

    def role_changed(self, guild, before=None, after=None):
        had_access = bool(before and before.name in TRAIN_ADMIN_ROLES)
        has_access = bool(after and after.name in TRAIN_ADMIN_ROLES)
        if had_access != has_access:
            self.request_acl(guild.id)

    def room_changed(self, before, after):
        if before.overwrites == after.overwrites:
            return
        gid = after.guild.id
        pid = self.service.store.for_channel(gid, after.id)
        if pid is not None:
            if len(self.acl_targets) < 128:
                self.acl_targets[gid, pid] = None
            else:
                self.request_acl(gid)

    async def _refresh_access(self, guild, pid):
        try:
            await self.service.refresh_access(guild, pid)
        except Exception:
            logger.exception("계획된 만남 파티방 권한 동기화 실패 guild=%s party=%s", guild.id, pid)

    async def update_acls(self, guild, cursor):
        parties = self.service.store.scan(guild.id, after=cursor, limit=12)
        for party in parties:
            if party["room_id"]:
                await self._refresh_access(guild, party["id"])
        return parties[-1]["id"] if len(parties) == 12 else None

    @tasks.loop(seconds=2)
    async def refresh(self):
        if not self.bot.is_ready():
            return
        for gid, pid in list(self.acl_targets)[:4]:
            self.acl_targets.pop((gid, pid), None)
            guild = self.bot.get_guild(gid)
            if guild:
                if guild.unavailable:
                    self.request_acl(gid)
                else:
                    await self._refresh_access(guild, pid)
        if not self.acl_cursors:
            return
        gid = next(iter(self.acl_cursors))
        cursor = self.acl_cursors[gid]
        guild = self.bot.get_guild(gid)
        if not guild:
            self.forget(gid)
            return
        self.acl_cursors.move_to_end(gid)
        if guild.unavailable:
            return
        try:
            cursor = await self.update_acls(guild, cursor)
        except Exception:
            logger.exception("계획된 만남 관리자 권한 점검 실패 guild=%s", gid)
            # Recovery and the admin panel still expose failed resources.
            cursor = None
        if gid not in self.acl_cursors:
            return
        if cursor is not None:
            self.acl_cursors[gid] = cursor
        elif gid in self.repeat_acl:
            self.repeat_acl.discard(gid)
            self.acl_cursors[gid] = 0
        else:
            self.acl_cursors.pop(gid)

    def resource_deleted(self, gid, cid, message_ids=None):
        if not gid:
            return
        store = self.service.store
        settings = store.settings(gid)
        if not settings["panel_channel_id"] and not settings["category_id"]:
            return
        state = self.state(gid)
        if cid == settings["panel_channel_id"] and (
            message_ids is None or settings["panel_message_id"] in message_ids
        ):
            state.panel_due = state.panel_failures = 0
        if message_ids is None and cid in (
            settings["category_id"], settings["recruit_channel_id"], settings["promo_channel_id"]
        ):
            state.request_review()
        if message_ids is None:
            pid = store.for_channel(gid, cid)
            parties = [] if pid is None else [pid]
        else:
            parties = store.for_messages(gid, cid, message_ids)
        for pid in parties:
            if len(state.targets) >= 128:
                state.request_review()
                break
            state.targets[pid] = None

    def message_seen(self, message):
        guild = message.guild
        if not guild or message.author.bot:
            return
        if not (getattr(message.channel, "topic", None) or "").startswith(f"계획된 만남 · {guild.id}/"):
            return
        key = (guild.id, message.channel.id)
        now = time.monotonic()
        if now < self.activity_writes.get(key, 0):
            return
        self.service.store.touch(*key)
        self.activity_writes[key] = now + 60
        self.activity_writes.move_to_end(key)
        if len(self.activity_writes) > 1024:
            self.activity_writes.popitem(last=False)

    async def _recover(self, guild, pid, *, check_members):
        try:
            await self.service.recover(guild, pid, check_members=check_members)
        except Exception:
            logger.exception("계획된 만남 복구 실패 guild=%s party=%s", guild.id, pid)

    async def tick_guild(self, guild):
        if guild.unavailable:
            return
        store = self.service.store
        settings = store.settings(guild.id)
        if not settings["panel_channel_id"] and not settings["category_id"]:
            return
        state = self.state(guild.id)
        now = time.monotonic()
        if settings["panel_channel_id"] and now >= state.panel_due:
            try:
                await self.service.restore_panel(guild)
            except Exception:
                logger.exception("계획된 만남 메인 패널 복구 실패 guild=%s", guild.id)
                state.panel_failures += 1
                # A channel deletion, /계획된만남 settings edit, or a reconnect can
                # start another attempt once the configuration is repaired.
                state.panel_due = (now + 30 * 2**state.panel_failures
                                   if state.panel_failures < 4 else float("inf"))
            else:
                state.panel_failures = 0
                state.panel_due = now + 3600

        pending = {}
        for pid in list(state.targets)[:4]:
            state.targets.pop(pid)
            pending[pid] = False
        dirty = store.scan(guild.id, after=state.dirty_cursor, limit=4, dirty_only=True)
        state.dirty_cursor = dirty[-1]["id"] if len(dirty) == 4 else 0
        for party in dirty:
            pending[party["id"]] = False
        if now >= state.review_due:
            # Fair keyset traversal also finds missed removals while disconnected.
            page = store.scan(guild.id, after=state.review_cursor, limit=4)
            state.review_cursor = page[-1]["id"] if len(page) == 4 else 0
            state.review_due = 0 if len(page) == 4 else now + 3600
            for party in page:
                pending[party["id"]] = True
        for pid, check_members in pending.items():
            await self._recover(guild, pid, check_members=check_members)

    @tasks.loop(seconds=15)
    async def poll(self):
        if not self.bot.is_ready():
            return
        guilds = self.bot.guilds
        if not guilds:
            return
        count = min(4, len(guilds))
        for offset in range(count):
            guild = guilds[(self.guild_cursor + offset) % len(guilds)]
            try:
                await self.tick_guild(guild)
            except Exception:
                logger.exception("계획된 만남 서버 상태 점검 실패 guild=%s", guild.id)
        self.guild_cursor = (self.guild_cursor + count) % len(guilds)

    @poll.before_loop
    @refresh.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    def start(self):
        if not self.poll.is_running():
            self.poll.start()
        if not self.refresh.is_running():
            self.refresh.start()

    def stop(self):
        self.poll.cancel()
        self.refresh.cancel()
