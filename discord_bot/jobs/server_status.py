# 공식 점검 공지를 20초마다 확인하고 채널명·상태 메시지·오픈알림을 갱신합니다.
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import tasks

from discord_bot.presenters.server_status import (
    SERVER_STATUS_FOOTER_PREFIX,
    build_status_channel_name,
    build_status_embed,
    strip_status_dot,
)

logger = logging.getLogger(__name__)


def _mention_chunks(user_ids, max_length=1800):
    chunks = []
    current = ""
    for user_id in user_ids:
        mention = f"<@{user_id}>"
        candidate = f"{current} {mention}" if current else mention
        if len(candidate) > max_length and current:
            chunks.append(current)
            current = mention
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


class ServerStatusJobs:
    def __init__(self, bot):
        self.bot = bot
        self.state = {
            "message": None,
            "last_state": None,
            "base_channel_name": None,
            "last_render_at": None,
        }

    async def find_existing_message(self, channel):
        if self.bot.user is None:
            return None

        try:
            async for message in channel.history(limit=50):
                if message.author.id != self.bot.user.id or not message.embeds:
                    continue

                footer_text = message.embeds[0].footer.text or ""
                if footer_text.startswith(SERVER_STATUS_FOOTER_PREFIX):
                    return message
        except discord.Forbidden:
            print(
                "[서버상태] 메시지 기록 보기 권한이 없어 "
                "기존 상태 메시지를 찾지 못했습니다."
            )
        except discord.HTTPException as e:
            print("[서버상태] 기존 상태 메시지 검색 실패:", e)

        return None

    def reminder_sent(self, maintenance_start, reminder_kind):
        with self.bot.database.connect() as conn:
            return (
                conn.execute(
                    """
                    SELECT 1 FROM maintenance_reminder_events
                    WHERE maintenance_start = ? AND reminder_kind = ?
                    """,
                    (maintenance_start, reminder_kind),
                ).fetchone()
                is not None
            )

    def mark_reminder_sent(self, maintenance_start, reminder_kind, sent_at):
        with self.bot.database.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO maintenance_reminder_events
                    (maintenance_start, reminder_kind, sent_at)
                VALUES (?, ?, ?)
                """,
                (maintenance_start, reminder_kind, sent_at),
            )

    def build_shutdown_command(self, start):
        kst = timezone(timedelta(hours=9))
        local_start = start.astimezone(kst)
        target = local_start.strftime("%Y-%m-%d %H:%M")
        return (
            "powershell -Command \"$t=[datetime]::Parse('"
            + target
            + "'); shutdown /s /t ([Math]::Max(0,[int]($t-(Get-Date)).TotalSeconds))\""
        )

    async def send_maintenance_reminder(self, channel, data, now_utc):
        start_text = data.get("next_maintenance_start_time")
        if not start_text:
            return

        start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
        seconds_left = (start - now_utc).total_seconds()
        if seconds_left <= 0:
            return

        kst = timezone(timedelta(hours=9))
        local_start = start.astimezone(kst)

        # 06:00 점검은 12시간 전, 그 외 임시/긴급 점검은 30분 전에 안내한다.
        if local_start.hour == 6 and local_start.minute == 0:
            reminder_kind = "12h"
            threshold = 12 * 60 * 60
            headline = (
                "⚠️ **12시간 뒤에 마비노기 모바일 점검이 시작됩니다. "
                "햄순이 가동에 참고해주세요.**"
            )
        else:
            reminder_kind = "30m"
            threshold = 30 * 60
            headline = (
                "⚠️ **30분 뒤에 마비노기 모바일 점검이 시작됩니다. "
                "햄순이 가동에 참고해주세요.**"
            )

        if seconds_left > threshold or self.reminder_sent(start_text, reminder_kind):
            return

        command = self.build_shutdown_command(start)
        await channel.send(
            headline
            + f"\n점검 시작: **{local_start.month}/{local_start.day} "
            + f"{local_start.hour:02d}:{local_start.minute:02d}**"
            + "\n햄순이 자동종료 예약 명령어:"
            + f"\n```powershell\n{command}\n```"
        )
        self.mark_reminder_sent(start_text, reminder_kind, now_utc.isoformat())
        print(f"[서버상태] 점검 사전안내 전송({reminder_kind}): {start_text}")

    async def send_open_notification(self, channel):
        user_ids = self.bot.open_subscriptions.list_ids()
        chunks = _mention_chunks(user_ids)
        if not chunks:
            return

        mentions = discord.AllowedMentions(
            users=True, roles=False, everyone=False, replied_user=False
        )
        for chunk in chunks[:-1]:
            await channel.send(content=chunk, allowed_mentions=mentions)

        await channel.send(
            content=(
                f"{chunks[-1]}\n"
                "🟢 **마비노기 모바일 서버가 오픈됐습니다!**\n"
                "점검이 종료되어 정상 운영 상태로 전환됐습니다."
            ),
            allowed_mentions=mentions,
        )
        print(f"[서버상태] 오픈알림 전송: {len(user_ids)}명")

    async def update_once(self):
        channel = self.bot.get_channel(self.bot.settings.server_status_channel_id)
        if channel is None:
            print(
                f"[서버상태] 채널을 찾을 수 없습니다. "
                f"ID={self.bot.settings.server_status_channel_id}"
            )
            return

        if self.state["base_channel_name"] is None:
            self.state["base_channel_name"] = strip_status_dot(
                getattr(channel, "name", "")
            )

        # 공식 점검 공지 조회 오류는 점검으로 오인하지 않고 기존 상태를 유지한다.
        try:
            data = await self.bot.maintenance.fetch()
        except Exception as e:
            print(
                "[서버상태] 공식 점검 공지 확인 실패 - 기존 상태 유지:",
                e,
            )
            return

        is_maintenance = bool(data.get("is_maintenance"))
        self.bot.abyss.set_maintenance_active(is_maintenance)

        # 완료된 점검 시작/종료시각을 어비스 추론용 데이터로도 저장한다.
        try:
            self.bot.abyss.note_maintenance(
                data.get("last_maintenance_start_time"),
                data.get("last_maintenance_end_time"),
                data.get("last_maintenance_url"),
            )
        except Exception:
            logger.exception("점검 이력 저장 오류")

        now_utc = datetime.now(timezone.utc)

        try:
            await self.send_maintenance_reminder(channel, data, now_utc)
        except discord.Forbidden:
            print("[서버상태] 점검 사전안내 전송 권한이 없습니다.")
        except discord.HTTPException as e:
            print("[서버상태] 점검 사전안내 전송 실패:", e)
        except Exception:
            logger.exception("점검 사전안내 처리 오류")

        previous_state = self.state["last_state"]
        desired_name = build_status_channel_name(
            self.state["base_channel_name"],
            is_maintenance,
        )

        # 채널명 본문은 유지하고 앞의 상태 동그라미만 바꾼다.
        if getattr(channel, "name", None) != desired_name:
            try:
                await channel.edit(
                    name=desired_name,
                    reason="머장봇 서버 상태 자동 반영",
                )
                print(f"[서버상태] 채널명 변경: {desired_name}")
            except discord.Forbidden:
                print(
                    "[서버상태] 채널명 변경 권한이 없습니다. "
                    "머장봇의 '채널 관리' 권한을 확인해주세요."
                )
            except discord.HTTPException as e:
                print("[서버상태] 채널명 변경 실패:", e)

        state_changed = (
            previous_state is not None and previous_state != is_maintenance
        )
        last_render_at = self.state["last_render_at"]
        render_due = (
            self.state["message"] is None
            or last_render_at is None
            or state_changed
            or (now_utc - last_render_at).total_seconds() >= 60
        )

        # 점검 여부는 20초마다 확인하되 디스코드 메시지는 1분마다 또는 상태 전환 때만 갱신한다.
        if render_due:
            embed = build_status_embed(data, now_utc)
            try:
                if self.state["message"] is None:
                    self.state["message"] = await self.find_existing_message(channel)

                if self.state["message"] is None:
                    self.state["message"] = await channel.send(embed=embed)
                    print(f"[서버상태] 상태 메시지 생성: {self.state['message'].id}")
                else:
                    try:
                        await self.state["message"].edit(embed=embed)
                    except discord.NotFound:
                        self.state["message"] = await channel.send(embed=embed)
                        print(f"[서버상태] 상태 메시지 재생성: {self.state['message'].id}")
                self.state["last_render_at"] = now_utc
            except discord.Forbidden:
                print("[서버상태] 메시지 전송/수정 권한이 없습니다.")
            except discord.HTTPException as e:
                print("[서버상태] 상태 메시지 갱신 실패:", e)

        if previous_state is None or previous_state != is_maintenance:
            state_text = "점검 중" if is_maintenance else "정상 운영"
            print(f"[서버상태] 상태 확인: {state_text}")

        # 오픈알림은 반드시 '점검 중(True) -> 정상(False)' 전환에만 보낸다.
        # 시작 직후 정상 상태(None -> False)나 점검 시작(False -> True)에는 보내지 않는다.
        if previous_state is True and is_maintenance is False:
            try:
                await self.send_open_notification(channel)
            except discord.Forbidden:
                print("[서버상태] 오픈알림 전송 권한이 없습니다.")
            except discord.HTTPException as e:
                print("[서버상태] 오픈알림 전송 실패:", e)
            except Exception:
                logger.exception("오픈알림 전송 오류")

        self.state["last_state"] = is_maintenance

    @tasks.loop(seconds=20)
    async def poll(self):
        try:
            await self.update_once()
        except Exception:
            logger.exception("서버 상태 갱신 오류")

    @poll.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    def start(self):
        if not self.poll.is_running():
            self.poll.start()

    def stop(self):
        self.poll.cancel()
