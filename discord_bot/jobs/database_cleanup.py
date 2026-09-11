# 7일마다 저장소 정리 기능을 실행합니다.
import logging

from discord.ext import tasks

from storage.cleanup import cleanup_database

logger = logging.getLogger(__name__)


class DatabaseCleanupJobs:
    def __init__(self, bot):
        self.bot = bot

    @tasks.loop(hours=168)
    async def poll(self):
        try:
            cleanup_database(self.bot.database)
        except Exception:
            logger.exception("DB 자동청소 오류")

    @poll.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    def start(self):
        if not self.poll.is_running():
            self.poll.start()

    def stop(self):
        self.poll.cancel()
