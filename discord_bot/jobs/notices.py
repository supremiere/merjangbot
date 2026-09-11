# 공지를 주기적으로 확인하고 새 글을 전송한 뒤 전송 이력을 저장합니다.
import logging

from discord.ext import tasks

from discord_bot.presenters.notices import build_notice_embed

logger = logging.getLogger(__name__)


class NoticeJobs:
    def __init__(self, bot):
        self.bot = bot

    async def check_once(self):
        channel = self.bot.get_channel(self.bot.settings.notice_channel_id)
        if channel is None:
            logger.warning("공지 채널을 찾을 수 없습니다.")
            return
        groups = await self.bot.official.fetch_all()
        if self.bot.notices.is_empty():
            for group in groups:
                for post in group:
                    self.bot.notices.save(post)
            return
        for group in groups:
            for post in reversed(group):
                if not self.bot.notices.is_sent(post["url"]):
                    await channel.send(embed=build_notice_embed(post))
                    self.bot.notices.save(post)

    @tasks.loop(minutes=1)
    async def poll(self):
        try:
            await self.check_once()
        except Exception:
            logger.exception("공홈 감시 오류")

    @poll.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    def start(self):
        if not self.poll.is_running():
            self.poll.start()

    def stop(self):
        self.poll.cancel()
