# 시작 시·6시간마다 갱신하고 실패하면 5분 간격으로 다시 시도합니다.
from discord.ext import tasks


class AbyssRankingJobs:
    def __init__(self, bot):
        self.bot = bot

    @tasks.loop(minutes=5)
    async def poll(self):
        task = self.bot.abyss_ranking.request_refresh()
        if task is not None:
            await task

    @poll.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    def start(self):
        if not self.poll.is_running():
            self.poll.start()

    def stop(self):
        self.poll.cancel()
