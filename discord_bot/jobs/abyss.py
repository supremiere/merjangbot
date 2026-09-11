# 어비스 데이터를 갱신하고 구독자 멘션을 포함한 중복 없는 사전 알림을 보냅니다.
import logging
from datetime import datetime, timezone

import discord
from discord.ext import tasks

from discord_bot.presenters.abyss import build_abyss_alert

logger = logging.getLogger(__name__)


def build_mention_chunks(user_ids, max_length=1800):
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


class AbyssJobs:
    def __init__(self, bot):
        self.bot = bot

    async def check_once(self, now=None):
        channel = self.bot.get_channel(self.bot.settings.abyss_channel_id)
        if channel is None:
            return
        for spawn, minutes, estimated in self.bot.abyss.due_alerts(
            now or datetime.now(timezone.utc)
        ):
            key = spawn.isoformat()
            if self.bot.abyss_alerts.is_sent(key, minutes):
                continue
            chunks = build_mention_chunks(self.bot.subscriptions.list_ids())
            mentions = discord.AllowedMentions(
                users=True, roles=False, everyone=False, replied_user=False
            )
            for chunk in chunks[:-1]:
                await channel.send(content=chunk, allowed_mentions=mentions)
            await channel.send(
                content=chunks[-1] if chunks else None,
                embed=build_abyss_alert(spawn, minutes, estimated),
                allowed_mentions=mentions,
            )
            self.bot.abyss_alerts.save(key, minutes)

    @tasks.loop(minutes=1)
    async def refresh(self):
        try:
            await self.bot.abyss.refresh()
        except Exception:
            logger.exception("어비스 데이터 갱신 오류")

    @tasks.loop(seconds=15)
    async def alerts(self):
        try:
            await self.check_once()
        except Exception:
            logger.exception("어비스 알림 오류")

    @refresh.before_loop
    async def before_refresh(self):
        await self.bot.wait_until_ready()

    @alerts.before_loop
    async def before_alerts(self):
        await self.bot.wait_until_ready()

    def start(self):
        for loop in (self.refresh, self.alerts):
            if not loop.is_running():
                loop.start()

    def stop(self):
        self.refresh.cancel()
        self.alerts.cancel()
