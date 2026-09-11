# 시작 후와 매 6시간마다 룬 통계를 갱신하며 오류 시 이전 캐시를 유지합니다.
import logging

from discord.ext import tasks

logger = logging.getLogger(__name__)


class RuneStatsJobs:
    def __init__(self, bot):
        self.bot = bot

    @tasks.loop(hours=6)
    async def poll(self):
        try:
            parsed = await self.bot.rune_stats.refresh()
            logger.info("룬통계 갱신 완료: %s직업", len(parsed))
        except Exception:
            logger.exception("룬통계 자동 갱신 실패: 기존 캐시 유지")

    @poll.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    def start(self):
        if not self.poll.is_running():
            self.poll.start()

    def stop(self):
        self.poll.cancel()
