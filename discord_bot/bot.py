# Discord 클라이언트의 수명주기·명령어 동기화·반복 작업 시작과 종료를 관리합니다.
import asyncio
import logging

import discord
from discord import app_commands

from discord_bot.commands import abyss, cleanup, market, rune_stats, server_status, test_bot, trains
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
        abyss_ranking_service,
        market_service,
        sheet_service,
        maintenance_service,
        notices,
        abyss_alerts,
        subscriptions,
        open_subscriptions,
        rune_stats_service,
    ):
        super().__init__(intents=discord.Intents.default())
        self.settings = settings
        self.http_client = http
        self.database = database
        self.official = official
        self.abyss = abyss_service
        self.abyss_ranking = abyss_ranking_service
        self.market = market_service
        self.sheets = sheet_service
        self.maintenance = maintenance_service
        self.notices = notices
        self.abyss_alerts = abyss_alerts
        self.subscriptions = subscriptions
        self.open_subscriptions = open_subscriptions
        self.rune_stats = rune_stats_service
        self.tree = app_commands.CommandTree(self)
        self._synced_guilds = set()
        self._global_commands_cleared = False
        self._sync_lock = asyncio.Lock()
        # 모비라이프 내부/프록시 API 없이 서버상태와 어비스 핵심 기능을 독립 운영합니다.
        # 유지: /시세(OpenAPI), /룬통계, /청소, /오픈알림, 서버상태,
        #       /어비스, /어구알림, /어구제보, /머장봇테스트, /열차*, 공식 공지 감시
        # 중단: /악보, /어비스랭킹
        for module in (market, cleanup, rune_stats, server_status, abyss, test_bot, trains):
            module.register(self)
        self.jobs = [
            NoticeJobs(self),
            ServerStatusJobs(self),
            AbyssJobs(self),
            DatabaseCleanupJobs(self),
            RuneStatsJobs(self),
        ]

    async def setup_hook(self):
        self.database.initialize()
        self.add_view(self.train_controller.panel_view)
        self.abyss.repository.seed_initial_observation()
        await self.abyss.refresh()
        self.rune_stats.load_cache()
        self.abyss_ranking.load_cache()
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
        try:
            await self.train_controller.ensure_all_panels()
        except Exception:
            logger.exception("우만열차 현황판 초기화 오류")

    async def on_guild_join(self, guild):
        try:
            await self.sync_commands()
        except Exception:
            logger.exception("새 서버 명령어 등록 오류")
        try:
            await self.train_controller.ensure_panel(guild)
        except Exception:
            logger.exception("새 서버 우만열차 현황판 초기화 오류")

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
        await self.abyss_ranking.close()
        try:
            await super().close()
        finally:
            await self.http_client.close()
