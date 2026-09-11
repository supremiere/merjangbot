# 설정·수집기·저장소를 생성하여 디스코드 봇에 명시적으로 연결합니다.
from discord_bot.bot import MerjangBot
from sites.erinndata.runes import RuneStatsService
from sites.http import HttpClient
from sites.moblife.abyss import AbyssService
from sites.moblife.client import MoblifeClient
from sites.moblife.maintenance import MaintenanceService
from sites.moblife.market import MarketService
from sites.moblife.sheets import SheetService
from sites.official.notices import OfficialNotices
from storage.abyss import AbyssRepository
from storage.database import Database
from storage.notices import NoticeRepository
from storage.rune_stats import RuneStatsRepository
from storage.subscriptions import SubscriptionRepository


def create_bot(settings):
    http = HttpClient()
    database = Database(settings.db_file)
    moblife = MoblifeClient(http, settings)
    return MerjangBot(
        settings=settings,
        http=http,
        database=database,
        rune_stats_service=RuneStatsService(http, RuneStatsRepository(database)),
        official=OfficialNotices(http),
        abyss_service=AbyssService(moblife),
        market_service=MarketService(moblife),
        sheet_service=SheetService(moblife),
        maintenance_service=MaintenanceService(moblife),
        notices=NoticeRepository(database),
        abyss_alerts=AbyssRepository(database),
        subscriptions=SubscriptionRepository(database),
    )
