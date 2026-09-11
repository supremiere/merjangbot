# 점검 상태에 따라 채널명과 기존 상태 메시지를 1분마다 갱신합니다.
import logging
from datetime import datetime, timezone

import discord
from discord.ext import tasks

from discord_bot.presenters.server_status import (
    SERVER_STATUS_FOOTER_PREFIX,
    build_status_channel_name,
    build_status_embed,
    strip_status_dot,
)

logger = logging.getLogger(__name__)


class ServerStatusJobs:
    def __init__(self, bot):
        self.bot = bot
        self.state = {"message": None, "last_state": None, "base_channel_name": None}

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

        # API 오류는 점검으로 오인하지 않고 기존 상태를 유지한다.
        try:
            data = await self.bot.maintenance.fetch()
        except Exception as e:
            print(
                "[서버상태] 모비라이프 상태 확인 실패 - 기존 상태 유지:",
                e,
            )
            return

        now_utc = datetime.now(timezone.utc)
        is_maintenance = bool(data.get("is_maintenance"))
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
        except discord.Forbidden:
            print("[서버상태] 메시지 전송/수정 권한이 없습니다.")
        except discord.HTTPException as e:
            print("[서버상태] 상태 메시지 갱신 실패:", e)

        if (
            self.state["last_state"] is None
            or self.state["last_state"] != is_maintenance
        ):
            state_text = "점검 중" if is_maintenance else "정상 운영"
            print(f"[서버상태] 상태 확인: {state_text}")

        self.state["last_state"] = is_maintenance

    @tasks.loop(minutes=1)
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
