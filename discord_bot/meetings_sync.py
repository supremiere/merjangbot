"""Discord resources for planned meetings. SQLite owns desired state and resource IDs."""
import hashlib
import re
import time
from datetime import datetime, timezone

import discord

from discord_bot.commands.trains import TRAIN_ADMIN_ROLES

UNKNOWN_CHANNEL = 10003
UNKNOWN_MESSAGE = 10008


class ResourceError(ValueError):
    """Repair requires a configuration or permission change by a server manager."""


class MeetingAccessError(ResourceError):
    """The requested private room or its overwrites could not be confirmed."""


def room_name(destination):
    text = re.sub(r"[^\w가-힣-]+", "-", str(destination).strip().lower()).strip("-_")
    return "🔒・" + (text[:90] or "파티방")


def room_overwrites(guild, bot_id, members, watchers):
    """Specify all grants at channel creation so it is never temporarily public."""
    chat = dict(view_channel=True, read_message_history=True, send_messages=True,
                attach_files=True, embed_links=True)
    mod = dict(chat, manage_messages=True, pin_messages=True)
    private = discord.PermissionOverwrite(view_channel=False, manage_channels=False,
                                          manage_roles=False, manage_webhooks=False,
                                          create_public_threads=False, create_private_threads=False,
                                          manage_messages=False, pin_messages=False)
    result = {guild.default_role: private}
    for role in guild.roles:
        if role != guild.default_role and role.name in TRAIN_ADMIN_ROLES:
            result[role] = discord.PermissionOverwrite(**mod)
    for user_id in set(members) | set(watchers):
        result[discord.Object(int(user_id), type=discord.User)] = discord.PermissionOverwrite(**chat)
    result[discord.Object(int(bot_id), type=discord.User)] = discord.PermissionOverwrite(
        **mod, manage_channels=True, manage_roles=True)
    if len(result) > 1000:  # Discord API error 30060
        raise ResourceError("파티방 권한 대상이 Discord 채널당 최대 1,000개를 초과합니다.")
    return result


def _permission_values(overwrites):
    return {target.id: tuple(p.value for p in overwrite.pair()) for target, overwrite in overwrites.items()}


def _marker(guild_id, party_id, surface, token=None):
    base = f"계획된 만남 · {guild_id}/{party_id}/{surface}"
    return base if token is None else f"{base}/{token}"


def _nonce(marker, channel_id):
    return int.from_bytes(hashlib.blake2b(f"{channel_id}/{marker}".encode(), digest_size=8).digest(), "big")


def _owned(message, channel, bot_id, marker):
    return (message.channel.id == channel.id and message.author.id == bot_id
            and not getattr(message, 'webhook_id', None)
            and bool(message.embeds) and message.embeds[0].footer.text == marker)


async def _guild_channel_topics(guild) -> dict[int, str | None]:
    """Keep every live channel ID, but only text channels can own a party topic."""
    # Guild.fetch_channels() rejects the entire response when discord.py doesn't
    # recognize one channel type. Use the same HTTP client without building models.
    channels = await guild._state.http.get_all_guild_channels(guild.id)
    text_types = (discord.ChannelType.text.value, discord.ChannelType.news.value)
    return {int(channel["id"]): channel.get("topic") if channel["type"] in text_types else None
            for channel in channels}


async def _get_channel(guild, channel_id, channel_type):
    if not channel_id:
        return None
    try:
        channel = await guild.fetch_channel(channel_id)
    except discord.NotFound as exc:
        if exc.code != UNKNOWN_CHANNEL:
            raise
        if channel_id in await _guild_channel_topics(guild):
            raise ResourceError("서버 목록에는 채널이 남아 있습니다. 봇의 채널 접근 권한을 확인해주세요.") from exc
        return None
    if not isinstance(channel, channel_type) or channel.guild.id != guild.id or channel.id != channel_id:
        raise ResourceError("설정한 채널의 서버 또는 종류를 확인해주세요.")
    return channel


def _require_read_history(channel):
    me = channel.guild.me
    if me is None:
        raise ResourceError("이 서버의 봇 권한을 확인할 수 없습니다. 연결을 확인한 뒤 복구해주세요.")
    permissions = channel.permissions_for(me)
    if not (permissions.view_channel and permissions.read_message_history):
        raise ResourceError("이전 게시 여부를 확인할 수 없습니다. 봇의 채널 보기·메시지 기록 보기 권한을 복구해주세요.")


async def _get_message(channel, message_id, bot_id, marker):
    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound as exc:
        if exc.code != UNKNOWN_MESSAGE:
            raise
        _require_read_history(channel)
        return None
    if not _owned(message, channel, bot_id, marker):
        raise ResourceError("저장된 메시지의 작성자 또는 파티 식별자가 일치하지 않습니다. 설정을 확인해주세요.")
    return message


def _components(value):
    """Ignore server-assigned component IDs and empty fields in Discord responses."""
    if isinstance(value, dict):
        return {k: _components(v) for k, v in value.items() if k != "id" and v not in (None, False, "")}
    if isinstance(value, list):
        return [_components(item) for item in value]
    return value


async def _update(message, payload):
    old_components = [component.to_dict() for component in message.components]
    new_components = payload["view"].to_components() if payload["view"] else []
    if (len(message.embeds) != 1 or message.embeds[0].to_dict() != payload["embed"].to_dict()
            or _components(old_components) != _components(new_components)):
        message = await message.edit(**payload, allowed_mentions=discord.AllowedMentions.none())
    return message


class MeetingDiscordSync:
    def __init__(self, bot, store, ui):
        self.bot = bot
        self.store = store
        self.ui = ui

    @property
    def bot_id(self):
        return self.bot.user.id

    def _refs(self, party, **fields):
        self.store.refs(party["guild_id"], party["id"], **fields)
        party.update(fields)

    def _scope(self, guild, party):
        if guild.id != party["guild_id"]:
            raise ResourceError("다른 서버의 파티입니다.")

    def _room_marker(self, party):
        return _marker(party["guild_id"], party["id"], "방", party["nonce"])

    async def _verify_single_room(self, guild, party, room):
        # Create Channel has no idempotency key; discord.py can internally retry
        # this POST after a lost reply. Do not publish or finish a disband while
        # an additional room still carries the same creation marker.
        marker = self._room_marker(party)
        topics = await _guild_channel_topics(guild)
        for channel_id, topic in topics.items():
            if channel_id != room.id and topic == marker:
                raise MeetingAccessError("같은 파티 식별자의 방이 여러 개입니다. 관리자에게 생성 기록 확인을 요청해주세요.")

    async def _locate_room(self, guild, party):
        marker = self._room_marker(party)
        room = await _get_channel(guild, party["room_id"], discord.TextChannel)
        if room:
            if room.topic != marker:
                raise MeetingAccessError("파티방 식별자가 변경되어 권한 수정과 삭제를 중지했습니다.")
            return room

        # An interrupted Create Channel may have succeeded before its ID was stored.
        # A copied topic alone does not prove ownership of an untracked channel.
        topics = await _guild_channel_topics(guild)
        matches = [channel_id for channel_id, topic in topics.items() if topic == marker]
        if not matches:
            return None
        if len(matches) == 1:
            candidate_id = matches[0]
            try:
                async for entry in guild.audit_logs(limit=100, action=discord.AuditLogAction.channel_create,
                                                    user=discord.Object(self.bot_id)):
                    if getattr(entry.target, "id", None) == candidate_id and entry.reason == marker:
                        candidate = await _get_channel(guild, candidate_id, discord.TextChannel)
                        if candidate is not None and candidate.topic == marker:
                            self._refs(party, room_id=candidate_id, guide_id=None)
                            return candidate
                        break
            except discord.Forbidden:
                pass
        raise MeetingAccessError("파티방 후보의 생성 기록을 확인할 수 없습니다. 관리자에게 알려주세요.")

    async def _recruit_channel(self, guild, party, *, final=False):
        original = party["recruit_channel_id"]
        if original:
            channel = await _get_channel(guild, original, discord.TextChannel)
            if channel is not None or final:
                return channel
        current = self.store.settings(guild.id)["recruit_channel_id"]
        if current == original:
            return None
        return await _get_channel(guild, current, discord.TextChannel)

    async def _room_category(self, guild, party):
        override = self.store.settings(guild.id)["category_id"]
        if override:
            category = await _get_channel(guild, override, discord.CategoryChannel)
            if category is None:
                raise MeetingAccessError("지정한 파티방 카테고리가 없어졌어요. 서버 설정에서 모집 채널 위치 사용을 선택해주세요.")
            return category
        recruit = await self._recruit_channel(guild, party)
        if recruit is None:
            raise MeetingAccessError("파티방을 만들 위치를 찾을 수 없어요. 모집 채널 설정을 확인해주세요.")
        if recruit.category_id is None:
            return None
        category = await _get_channel(guild, recruit.category_id, discord.CategoryChannel)
        if category is None:
            raise MeetingAccessError("모집 채널의 카테고리가 변경됐어요. 위치를 확인한 뒤 다시 시도해주세요.")
        return category

    async def _ensure_room(self, guild, party, *, create):
        room = await self._locate_room(guild, party)
        permissions = room_overwrites(guild, self.bot_id, party["members"], party["watchers"])
        if room is None:
            if not create:
                raise MeetingAccessError("파티방이 없어 입장할 수 없습니다. 파티 관리에서 복구해주세요.")
            category = await self._room_category(guild, party)
            marker = self._room_marker(party)
            try:
                room = await guild.create_text_channel(
                    room_name(party["destination"]), category=category, topic=marker,
                    overwrites=permissions, reason=marker)
            except discord.HTTPException as exc:
                if exc.code in (30013, 50035):
                    raise MeetingAccessError("파티방 생성에 실패했습니다. 서버·카테고리 채널 한도와 권한을 확인해주세요.") from exc
                raise
            self._refs(party, room_id=room.id, guide_id=None)
        elif _permission_values(permissions) != _permission_values(room.overwrites):
            room = await room.edit(overwrites=permissions, reason="계획된 만남 파티방 권한 동기화")
        return room

    async def sync_access(self, guild, party):
        """Admission gate. No success response before the Discord write is confirmed."""
        self._scope(guild, party)
        try:
            return await self._ensure_room(guild, party, create=False)
        except discord.HTTPException as exc:
            raise MeetingAccessError("파티방 접근 권한을 반영하지 못했습니다. 봇 권한을 확인해주세요.") from exc

    def _payload(self, party, surface, marker):
        embed = self.ui.party_embed(party, surface)
        embed.set_footer(text=marker)
        return dict(embed=embed, view=self.ui.party_view(party, surface))

    def _attempt(self, marker):
        with self.store.db.connect() as conn:
            return conn.execute("SELECT channel_id,attempted_at FROM meeting_discord_sends WHERE marker=?",
                                (marker,)).fetchone()

    def _mark_sending(self, marker, channel_id):
        with self.store.db.connect() as conn:
            conn.execute("""INSERT INTO meeting_discord_sends(marker,channel_id,attempted_at) VALUES(?,?,?)
                ON CONFLICT(marker) DO UPDATE SET channel_id=excluded.channel_id, attempted_at=excluded.attempted_at""",
                         (marker, channel_id, int(time.time())))

    def _confirmed(self, marker):
        with self.store.db.connect() as conn:
            conn.execute("DELETE FROM meeting_discord_sends WHERE marker=?", (marker,))

    async def _old_attempt(self, channel, marker):
        attempt = self._attempt(marker)
        if not attempt:
            return None
        if attempt[0] != channel.id:
            old_channel = await _get_channel(channel.guild, attempt[0], discord.TextChannel)
            if old_channel is not None:
                raise ResourceError("이전 발송 결과를 확인한 뒤 게시 채널을 변경해주세요.")
            self._confirmed(marker)
            return None
        return await self._replay(channel, marker, attempt[1])

    async def _replay(self, channel, marker, attempted_at):
        # Discord's HTTP client may queue a send for rate limits. The message
        # can appear long after the local attempt started, even on a quiet channel.
        after = datetime.fromtimestamp(attempted_at - 120, timezone.utc)
        scanned = 0
        async for message in channel.history(limit=1001, after=after, oldest_first=True):
            if _owned(message, channel, self.bot_id, marker):
                return message
            scanned += 1
        if scanned > 1000:
            raise ResourceError("메시지 발송 시각의 기록이 너무 많아 재발송을 보류했습니다. 관리자 확인이 필요합니다.")
        # Get Channel Messages also returns an empty array when the caller
        # lacks Read Message History. Such an answer cannot prove absence.
        _require_read_history(channel)
        return None

    async def _ensure_message(self, channel, message_id, marker, payload):
        if message_id:
            message = await _get_message(channel, message_id, self.bot_id, marker)
            if message:
                return await _update(message, payload)
        recovered = await self._old_attempt(channel, marker)
        if recovered:
            return await _update(recovered, payload)
        self._mark_sending(marker, channel.id)
        try:
            # A confirmed deletion is a distinct send (even in Discord's nonce
            # retention window); retries of that same replacement reuse its nonce.
            identity = f"{marker}/{message_id or 0}"
            return await channel.send(**payload, nonce=_nonce(identity, channel.id),
                                      allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            if 400 <= exc.status < 500 and exc.status not in (408, 429):
                self._confirmed(marker)
            raise

    async def _public(self, guild, party, *, final=False):
        channel = await self._recruit_channel(guild, party, final=final)
        if channel is None:
            if final:
                return
            raise ResourceError("모집 메시지를 게시할 텍스트 채널을 설정해주세요.")
        if party["recruit_channel_id"] != channel.id:
            self._refs(party, recruit_channel_id=channel.id, recruit_id=None)
        marker = _marker(guild.id, party["id"], "모집")
        payload = self._payload(party, "public", marker)
        if final:
            old = (await _get_message(channel, party["recruit_id"], self.bot_id, marker)
                   if party["recruit_id"] else None)
            if old is None:
                old = await self._old_attempt(channel, marker)
            if old:
                await _update(old, payload)
                if party["recruit_id"] != old.id:
                    self._refs(party, recruit_id=old.id)
            self._confirmed(marker)
            return
        message = await self._ensure_message(channel, party["recruit_id"], marker, payload)
        if party["recruit_id"] != message.id:
            self._refs(party, recruit_id=message.id)
        self._confirmed(marker)

    async def _guide(self, guild, party, room):
        marker = _marker(guild.id, party["id"], "안내")
        message = await self._ensure_message(room, party["guide_id"], marker,
                                             self._payload(party, "room", marker))
        if party["guide_id"] != message.id:
            self._refs(party, guide_id=message.id)
        self._confirmed(marker)
        if not message.pinned:
            await message.pin(reason="계획된 만남 파티방 안내")

    async def _ads(self, guild, party):
        # Keep one active advertisement per party. A failed deletion keeps its ID
        # in SQLite so a later repair can retire it without losing track of it.
        ads = list(party["ads"])
        prefix = _marker(guild.id, party["id"], "홍보") + "/"
        # Verify a surviving replacement before deleting any older post. A
        # moderator may have removed the latest one before acknowledgement.
        kept = False
        for ref in reversed(ads.copy()):
            channel = await _get_channel(guild, ref["channel_id"], discord.TextChannel)
            message = None
            if channel:
                try:
                    message = await channel.fetch_message(ref["message_id"])
                except discord.NotFound as exc:
                    if exc.code != UNKNOWN_MESSAGE:
                        raise
                    _require_read_history(channel)
            if message:
                marker = message.embeds[0].footer.text if message.embeds else None
                if not marker or not marker.startswith(prefix) or not _owned(message, channel, self.bot_id, marker):
                    raise ResourceError("홍보글의 작성자 또는 파티 식별자가 달라 수정을 중지했습니다.")
                if not kept:
                    await _update(message, self._payload(party, "ad", marker))
                    self._confirmed(marker)
                    kept = True
                    continue
                try:
                    await message.delete()
                except discord.NotFound as exc:
                    if exc.code != UNKNOWN_MESSAGE:
                        raise
                    _require_read_history(channel)
            ads.remove(ref)
            self._refs(party, ads=list(ads))

    async def promote(self, guild, party):
        self._scope(guild, party)
        token = party["promo_token"]
        if not token:
            return
        marker = _marker(guild.id, party["id"], "홍보", token)
        if (party["phase"] != "ready" or party["activity"] == "ended" or party["closed"]
                or len(party["members"]) >= party["capacity"] or not self.store.settings(guild.id)["enabled"]):
            await self._settle_promotion(guild, party)
            await self._ads(guild, party)
            return
        try:
            channel = await _get_channel(guild, party["promo_channel_id"], discord.TextChannel)
            if channel is None:
                self._confirmed(marker)
                raise ResourceError("홍보 채널을 찾을 수 없습니다. 자유-채팅 채널 설정을 확인해주세요.")
            message = await self._ensure_message(channel, None, marker, self._payload(party, "ad", marker))
        except ResourceError:
            if not self._attempt(marker):
                self.store.finish_promo(guild.id, party["id"], token)
            raise
        except discord.HTTPException as exc:
            if 400 <= exc.status < 500 and exc.status not in (408, 429) and not self._attempt(marker):
                self.store.finish_promo(guild.id, party["id"], token)
            raise
        self._record_ad(party, token, message)
        self._confirmed(marker)
        await self._ads(guild, self.store.get(guild.id, party["id"]))

    def _record_ad(self, party, token, message):
        posted_at = getattr(message, "created_at", None)
        self.store.finish_promo(party["guild_id"], party["id"], token, channel_id=message.channel.id,
                                message_id=message.id,
                                posted_at=posted_at.timestamp() if isinstance(posted_at, datetime) else None)

    async def _settle_promotion(self, guild, party):
        """A cancelled/ended recruitment may acknowledge an earlier send but cannot start one."""
        token = party["promo_token"]
        if not token:
            return
        marker = _marker(guild.id, party["id"], "홍보", token)
        previous = None
        if self._attempt(marker):
            channel = await _get_channel(guild, party["promo_channel_id"], discord.TextChannel)
            if channel:
                previous = await self._old_attempt(channel, marker)
        if previous:
            self._record_ad(party, token, previous)
        else:
            self.store.finish_promo(guild.id, party["id"], token)
        self._confirmed(marker)
        party.update(self.store.get(guild.id, party["id"]))

    async def sync(self, guild, party):
        self._scope(guild, party)
        if party["phase"] == "disbanded":
            return
        if party["phase"] == "disbanding":
            await self._settle_promotion(guild, party)
            # The decision is irrevocable even while the room deletion awaits
            # acknowledgement. Final cards must never leave a stale progress label.
            party = dict(party, phase="disbanded")
            await self._public(guild, party, final=True)
            await self._ads(guild, party)
            room = await self._locate_room(guild, party)
            if room:
                await self._verify_single_room(guild, party, room)
                try:
                    await room.delete(reason="계획된 만남 파티장·관리자 해산 확인")
                except discord.NotFound as exc:
                    if exc.code != UNKNOWN_CHANNEL:
                        raise
                    if room.id in await _guild_channel_topics(guild):
                        raise ResourceError("삭제 응답과 서버 채널 목록이 달라 해산을 보류했습니다. 파티방을 확인해주세요.") from exc
            self._refs(party, phase="disbanded", room_id=None, guide_id=None, error="", dirty=False)
            return
        room = await self._ensure_room(guild, party, create=True)
        # Render the ready state for both cards, then commit the final phase
        # after Discord has acknowledged the recruit, guide and pin.
        if party["phase"] == "creating":
            await self._verify_single_room(guild, party, room)
            party["phase"] = "ready"
        await self._public(guild, party)
        await self._guide(guild, party, room)
        await self._ads(guild, party)
        self._refs(party, phase="ready", dirty=False, error="")

    async def sync_panel(self, guild, settings):
        channel = await _get_channel(guild, settings["panel_channel_id"], discord.TextChannel)
        if channel is None:
            if settings["panel_channel_id"]:
                raise ResourceError("메인 패널 채널을 찾을 수 없습니다.")
            return None
        marker = _marker(guild.id, 0, f"패널/{channel.id}")
        embed = self.ui.panel_embed(settings)
        embed.set_footer(text=marker)
        message = await self._ensure_message(channel, settings["panel_message_id"], marker,
                                             dict(embed=embed, view=self.ui.panel_view()))
        if message.id != settings["panel_message_id"]:
            self.store.configure(guild.id, 0, panel_message_id=message.id)
        self._confirmed(marker)
        if not message.pinned:
            await message.pin(reason="계획된 만남 메인 패널")
        return message
