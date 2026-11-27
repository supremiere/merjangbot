"""Coordinate durable party decisions with the Discord resources they describe."""
import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager
from weakref import WeakValueDictionary

import discord

from discord_bot.commands.trains import NO_MENTIONS, can_manage_trains
from storage.meetings import MeetingError


logger = logging.getLogger(__name__)
RETRY_DELAYS = (15, 60, 300, 900, 3600)
MANAGEMENT_ACTIONS = {
    "edit", "open_watch", "close_watch", "kick", "allow", "transfer", "strategy",
    "close", "open", "start", "end", "resume", "disband", "promote",
}
MEMBER_ACTIONS = {"join", "leave", "watch", "unwatch", "roster"}
ACTION_LABELS = {
    "create": "파티 생성", "transfer": "파티장 위임", "kick": "추방", "allow": "재입장 허용",
    "end": "활동 종료", "resume": "활동 재개", "disband": "파티 해산", "repair": "동기화 복구",
    "configure": "서버 설정 변경", "close_watch": "관전 닫기", "open_watch": "관전 열기",
    "edit": "모집 정보 수정", "roster": "로스터 수정", "strategy": "작전 수정",
    "open": "모집 재개", "close": "모집 마감", "start": "활동 시작", "promote": "홍보",
}


class MeetingService:
    def __init__(self, bot, store, sync):
        self.bot = bot
        self.store = store
        self.sync = sync
        # A lock is retained by its holder and all waiters. Inactive parties cost no memory.
        self._locks = WeakValueDictionary()

    def _lock(self, guild_id, key):
        return self._locks.setdefault((guild_id, key), asyncio.Lock())

    @asynccontextmanager
    async def _actor_turn(self, guild, actor, key):
        # An existing lock may be handing off to an earlier waiter. asyncio
        # reports locked() == False in that interval even though we must queue.
        shared = (guild.id, key) in self._locks
        async with self._lock(guild.id, key):
            yield await self._queued_actor(guild, actor, shared)

    @staticmethod
    async def _queued_actor(guild, actor, waited):
        # Discord supplies the member on each click. A click queued behind another
        # network operation can outlive the member's roles or guild membership.
        if not waited:
            return actor
        try:
            return await guild.fetch_member(actor.id)
        except (discord.HTTPException, OSError, TimeoutError) as exc:
            if isinstance(exc, discord.NotFound) and exc.code == 10007:
                raise MeetingError("현재 이 서버에 참가한 사용자만 이용할 수 있어요.") from None
            raise MeetingError("현재 서버 권한을 확인하지 못했어요. 잠시 뒤 다시 시도해 주세요.") from None

    @staticmethod
    def _actor(interaction):
        guild = interaction.guild
        member = interaction.user
        if guild is None or getattr(getattr(member, "guild", None), "id", None) != guild.id:
            raise MeetingError("이 서버 안에서 이용해 주세요.")
        if getattr(member, "bot", False):
            raise MeetingError("서버 구성원만 이용할 수 있어요.")
        return guild, member

    def require_admin(self, interaction):
        _, member = self._actor(interaction)
        if not can_manage_trains(member):
            raise MeetingError("머장봇 관리자만 사용할 수 있어요.")

    def require_manager(self, interaction, pid):
        guild, member = self._actor(interaction)
        party = self.store.get(guild.id, pid)
        if party["leader_id"] != member.id and not can_manage_trains(member):
            raise MeetingError("파티장 또는 관리자만 사용할 수 있어요.")
        return party

    def require_access(self, interaction, pid):
        guild, member = self._actor(interaction)
        party = self.store.get(guild.id, pid)
        if party["phase"] != "ready":
            raise MeetingError("현재 이용할 수 없는 파티방이에요.")
        if (member.id not in party["members"] and member.id not in party["watchers"]
                and not can_manage_trains(member)):
            raise MeetingError("참가하거나 관전하기를 눌러 파티방에 입장해 주세요.")
        return party

    def require_member(self, interaction, pid):
        guild, member = self._actor(interaction)
        party = self.store.get(guild.id, pid)
        if member.id not in party["members"] and not can_manage_trains(member):
            raise MeetingError("로스터는 파티원과 관리자만 수정할 수 있어요.")
        return party

    @staticmethod
    def _problem(exc):
        if isinstance(exc, ValueError) and str(exc):
            return str(exc)[:400]
        if isinstance(exc, discord.Forbidden):
            return "봇의 채널·역할·메시지 관리 권한과 역할 순서를 확인해 주세요."
        if isinstance(exc, sqlite3.Error):
            return "데이터베이스 저장 상태를 확인해 주세요."
        return "Discord 연결과 지정 채널 상태를 확인해 주세요."

    async def _log(self, guild, pid, actor, action, detail="", success=True):
        """Mirror selected operations; the SQLite audit is independent of this channel."""
        try:
            channel_id = self.store.settings(guild.id)["log_channel_id"]
            channel = guild.get_channel(channel_id) if channel_id else None
            if channel is None:
                return
            status = "완료" if success else "확인 필요"
            label = ACTION_LABELS.get(action, action)
            party = f"파티 #{pid}" if pid is not None else "서버"
            by = f"<@{actor}>" if actor else "봇"
            await channel.send(f"💌 {party} · {label} · {status} · {by} {detail}".rstrip(),
                               allowed_mentions=NO_MENTIONS)
        except discord.Forbidden as exc:
            logger.warning("계획된 만남 관리자 로그 채널 전송 권한 없음 (서버 %s, 채널 %s, Discord %s)",
                           guild.id, channel_id, exc.code)
        except Exception:
            logger.exception("계획된 만남 관리자 로그 전송 실패 (서버 %s)", guild.id)

    async def _failed(self, guild, pid, actor, action, exc):
        reason = self._problem(exc)
        try:
            count = self.store.get(guild.id, pid)["retry_count"] + 1
            delay = RETRY_DELAYS[min(count - 1, len(RETRY_DELAYS) - 1)]
            self.store.refs(guild.id, pid, dirty=True, error=reason, retry_count=count,
                            next_retry_at=int(self.store.clock()) + delay)
            self.store.audit(guild.id, pid, actor, action, reason, False)
        except sqlite3.Error:
            logger.exception("계획된 만남 복구 상태 저장 실패 (파티 %s)", pid)
        logger.warning("계획된 만남 %s 실패 (서버 %s, 파티 %s): %s · %s",
                       action, guild.id, pid, type(exc).__name__, reason)
        await self._log(guild, pid, actor, action, reason, False)
        return MeetingError(f"파티 #{pid} 처리에 확인이 필요해요. {reason} 관리자 모드에서 복구할 수 있어요.")

    def _settled(self, guild_id, pid):
        self.store.refs(guild_id, pid, dirty=False, error="", retry_count=0, next_retry_at=0)
        return self.store.get(guild_id, pid)

    async def create(self, interaction, **fields):
        guild, actor = self._actor(interaction)
        settings = self.store.settings(guild.id)
        if not settings["recruit_channel_id"]:
            raise MeetingError("관리자가 먼저 모집 채널을 설정해야 해요.")
        party = self.store.create(guild.id, actor.id, request_id=str(interaction.id), **fields)
        pid = party["id"]
        async with self._lock(guild.id, pid):
            party = self.store.get(guild.id, pid)
            if party["phase"] == "creating":
                try:
                    await self.sync.sync(guild, party)
                    party = self._settled(guild.id, pid)
                except Exception as exc:
                    raise await self._failed(guild, pid, actor.id, "create", exc) from exc
                await self._log(guild, pid, actor.id, "create")
            if party["phase"] != "ready":
                raise MeetingError("이미 종료 처리한 생성 요청이에요.")
            return party

    async def act(self, interaction, pid, action, **fields):
        guild, actor = self._actor(interaction)
        async with self._actor_turn(guild, actor, pid) as actor:
            if action == "repair":
                if not can_manage_trains(actor):
                    raise MeetingError("머장봇 관리자만 사용할 수 있어요.")
                party = await self._recover(guild, pid, actor.id, manual=True, check_members=True)
                self.store.audit(guild.id, pid, actor.id, "repair", "파티 리소스 재검증", True)
                await self._log(guild, pid, actor.id, action)
                return party
            if action not in MANAGEMENT_ACTIONS | MEMBER_ACTIONS:
                raise MeetingError("지원하지 않는 파티 요청이에요.")
            before = self.store.get(guild.id, pid)
            admin = can_manage_trains(actor)
            try:
                party = self.store.mutate(guild.id, pid, actor.id, admin, action, **fields)
            except MeetingError as exc:
                if action in MANAGEMENT_ACTIONS and (admin or actor.id == before["leader_id"]):
                    self.store.audit(guild.id, pid, actor.id, action, str(exc), False)
                raise
            access_confirmed = False
            try:
                if action in {"join", "watch"}:
                    try:
                        await self.sync.sync_access(guild, party)
                        access_confirmed = True
                    except Exception:
                        # Access never counts as a successful admission until Discord confirms it.
                        if actor.id not in before["members"] and (
                            action == "join" or actor.id not in before["watchers"]
                        ):
                            self.store.undo_admit(guild.id, pid, actor.id, action,
                                                  actor.id in before["watchers"])
                            try:
                                await self.sync.sync_access(guild, self.store.get(guild.id, pid))
                            except Exception:
                                logger.warning("파티 %s 접근 권한 원상 복구 대기", pid)
                        raise
                if action == "promote":
                    await self.sync.promote(guild, party)
                await self.sync.sync(guild, self.store.get(guild.id, pid))
                result = self._settled(guild.id, pid)
            except Exception as exc:
                failure = await self._failed(guild, pid, actor.id, action, exc)
                if access_confirmed:
                    current = self.store.get(guild.id, pid)
                    # ACL succeeded; a stale public card must not make the user retry admission.
                    current["sync_warning"] = str(failure)
                    return current
                raise failure from exc
            if action in {"transfer", "kick", "allow", "end", "resume", "disband",
                          "open_watch", "close_watch"} or (admin and action in MANAGEMENT_ACTIONS):
                detail = f"대상 <@{int(fields['target'])}>" if "target" in fields else ""
                await self._log(guild, pid, actor.id, action, detail)
            return result

    async def _reconcile_departures(self, guild, party):
        for uid in party["members"] + party["watchers"]:
            if self.bot.intents.members and guild.get_member(uid) is not None:
                continue
            try:
                await guild.fetch_member(uid)
            except discord.NotFound as exc:
                # A 404 for the guild itself must not erase a member's roster.
                if exc.code != 10007:  # Discord: Unknown Member
                    raise
                self.store.depart(guild.id, party["id"], uid)

    async def _verify_members(self, guild, party, actor):
        pid = party["id"]
        pending = party["member_check"]
        if pending is None:
            # Record the work before the first REST lookup so cancellation or a
            # process exit cannot convert a partial verification into a clean one.
            self.store.save_member_check(guild.id, pid)
        try:
            await self._reconcile_departures(guild, party)
        except Exception as exc:
            count = (pending["failures"] if pending else 0) + 1
            retry_at = None
            if count <= len(RETRY_DELAYS):
                retry_at = int(self.store.clock()) + RETRY_DELAYS[count - 1]
            reason = f"서버 구성원 확인이 끝나지 않았어요. {self._problem(exc)}"
            self.store.save_member_check(guild.id, pid, count, retry_at, reason)
            self.store.audit(guild.id, pid, actor, "member_check", reason, False)
            await self._log(guild, pid, actor, "구성원 확인", reason, False)
            return MeetingError(f"파티 #{pid} · {reason}")
        self.store.complete_member_check(guild.id, pid)
        return None

    async def _recover(self, guild, pid, actor=0, *, manual=False, check_members=False):
        party = self.store.get(guild.id, pid)
        if party["phase"] == "disbanded":
            return party
        member_problem = None
        pending = party["member_check"]
        if pending:
            check_members = manual or (pending["retry_at"] is not None and pending["retry_at"] <= self.store.clock())
        if check_members and party["phase"] in {"creating", "ready"}:
            member_problem = await self._verify_members(guild, party, actor)
            party = self.store.get(guild.id, pid)
        if not manual and (party["retry_count"] > len(RETRY_DELAYS)
                           or party["next_retry_at"] > self.store.clock()):
            if member_problem:
                raise member_problem
            return party
        try:
            # Restore room membership/ACL first, even if an earlier ad's delivery is uncertain.
            await self.sync.sync(guild, party)
            party = self.store.get(guild.id, pid)
            if party["promo_token"]:
                await self.sync.promote(guild, party)
            party = self._settled(guild.id, pid)
        except Exception as exc:
            raise await self._failed(guild, pid, actor, "repair", exc) from exc
        if member_problem:
            raise member_problem
        return party

    async def recover(self, guild, pid, *, manual=False, check_members=False):
        async with self._lock(guild.id, pid):
            return await self._recover(guild, pid, manual=manual, check_members=check_members)

    async def refresh_access(self, guild, pid):
        """A role losing admin rights cannot depend on a successful public-card repair."""
        async with self._lock(guild.id, pid):
            party = self.store.get(guild.id, pid)
            if party["phase"] not in {"ready", "creating", "disbanding"} or not party["room_id"]:
                return
            try:
                await self.sync.sync_access(guild, party)
            except Exception as exc:
                raise await self._failed(guild, pid, 0, "권한 복구", exc) from exc

    async def member_left(self, guild, uid):
        for pid in self.store.associated(guild.id, uid):
            async with self._lock(guild.id, pid):
                try:
                    self.store.depart(guild.id, pid, uid)
                    await self._recover(guild, pid, manual=True)
                except Exception:
                    logger.exception("서버 탈퇴 후 파티 갱신 실패 (파티 %s)", pid)

    async def restore_panel(self, guild):
        async with self._lock(guild.id, "settings"):
            settings = self.store.settings(guild.id)
            if settings["panel_channel_id"]:
                await self.sync.sync_panel(guild, settings)

    async def configure(self, interaction, **fields):
        guild, actor = self._actor(interaction)
        async with self._actor_turn(guild, actor, "settings") as actor:
            if not can_manage_trains(actor):
                raise MeetingError("머장봇 관리자만 사용할 수 있어요.")
            settings = self.store.settings(guild.id)
            if set(fields) - (set(settings) - {"guild_id", "panel_message_id"}):
                raise MeetingError("지원하지 않는 설정이에요.")
            for key in ("panel_channel_id", "recruit_channel_id", "promo_channel_id",
                        "category_id", "log_channel_id"):
                if key not in fields or fields[key] is None:
                    continue
                channel = guild.get_channel(int(fields[key]))
                expected = discord.CategoryChannel if key == "category_id" else discord.TextChannel
                if not isinstance(channel, expected):
                    raise MeetingError("이 서버에 있는 올바른 종류의 채널을 선택해 주세요.")
            desired = dict(settings, **fields)
            if desired["enabled"] and not all(desired[k] for k in
                    ("panel_channel_id", "recruit_channel_id", "promo_channel_id")):
                raise MeetingError("먼저 메인 패널·모집·홍보 채널을 지정해 주세요.")
            result = self.store.configure(guild.id, actor.id, **fields)
            try:
                if result["panel_channel_id"]:
                    await self.sync.sync_panel(guild, result)
                jobs = getattr(self.bot, "meeting_jobs", None)
                if jobs is not None:
                    state = jobs.state(guild.id)
                    state.panel_due = state.panel_failures = 0
            except Exception as exc:
                reason = self._problem(exc)
                self.store.audit(guild.id, None, actor.id, "configure", reason, False)
                await self._log(guild, None, actor.id, "configure", reason, False)
                raise MeetingError(f"설정은 저장됐어요. 메인 패널 복구가 필요해요. {reason}") from exc
            await self._log(guild, None, actor.id, "configure")
            return self.store.settings(guild.id)
