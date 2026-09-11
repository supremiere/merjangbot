# Discord 클라이언트의 수명주기·명령어 동기화·반복 작업 시작과 종료를 관리합니다.
import asyncio
import logging

import discord
from discord import app_commands

from discord_bot.commands import abyss, cleanup, market, rune_stats, sheets
from discord_bot.jobs.abyss import AbyssJobs
from discord_bot.jobs.database_cleanup import DatabaseCleanupJobs
from discord_bot.jobs.notices import NoticeJobs
from discord_bot.jobs.rune_stats import RuneStatsJobs
from discord_bot.jobs.server_status import ServerStatusJobs

logger = logging.getLogger(__name__)


class MerjangBot(discord.Client):
    def __init__(
        self,
        *,
        settings,
        http,
        database,
        official,
        abyss_service,
        market_service,
        sheet_service,
        maintenance_service,
        notices,
        abyss_alerts,
        subscriptions,
        rune_stats_service,
    ):
        super().__init__(intents=discord.Intents.default())
        self.settings = settings
        self.http_client = http
        self.database = database
        self.official = official
        self.abyss = abyss_service
        self.market = market_service
        self.sheets = sheet_service
        self.maintenance = maintenance_service
        self.notices = notices
        self.abyss_alerts = abyss_alerts
        self.subscriptions = subscriptions
        self.rune_stats = rune_stats_service
        self.tree = app_commands.CommandTree(self)
        self._synced_guilds = set()
        self._global_commands_cleared = False
        self._sync_lock = asyncio.Lock()
        for module in (abyss, market, sheets, cleanup, rune_stats):
            module.register(self)
        self.jobs = [
            NoticeJobs(self),
            AbyssJobs(self),
            ServerStatusJobs(self),
            DatabaseCleanupJobs(self),
            RuneStatsJobs(self),
        ]

    async def setup_hook(self):
        self.database.initialize()
        self.rune_stats.load_cache()
        await self.http_client.start()
        for job in self.jobs:
            job.start()

    async def sync_commands(self):
        async with self._sync_lock:
            for guild in self.guilds:
                if guild.id not in self._synced_guilds:
                    self.tree.copy_global_to(guild=guild)
                    await self.tree.sync(guild=guild)
                    self._synced_guilds.add(guild.id)
            if not self._global_commands_cleared:
                # 과거 전역 등록은 삭제하되 새 서버 등록에 필요한 로컬 명령어 정의는 유지합니다.
                commands = self.tree.get_commands()
                self.tree.clear_commands(guild=None)
                try:
                    await self.tree.sync()
                    self._global_commands_cleared = True
                finally:
                    for command in commands:
                        self.tree.add_command(command)

    async def on_ready(self):
        logger.info("머장봇 접속 성공: %s / 서버 %s개", self.user, len(self.guilds))
        try:
            await self.sync_commands()
        except Exception:
            logger.exception("명령어 동기화 오류")

    async def on_guild_join(self, guild):
        try:
            await self.sync_commands()
        except Exception:
            logger.exception("새 서버 명령어 등록 오류")

    async def close(self):
        pending = []
        for job in self.jobs:
            for name in ("poll", "refresh", "alerts"):
                loop = getattr(job, name, None)
                if loop is not None:
                    task = loop.get_task()
                    if task is not None:
                        pending.append(task)
            job.stop()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        try:
            await super().close()
        finally:
            await self.http_client.close()
