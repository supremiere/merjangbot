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

        # 완전한 첫 실행이면 현재 게시물 전체를 기준값으로만 저장한다.
        if self.bot.notices.is_empty():
            for group in groups:
                for post in group:
                    self.bot.notices.save(post)
            return

        for group in groups:
            if not group:
                continue

            category = group[0]["category"]

            # 새 게시판 감시 기능이 추가된 첫 실행에서는 기존 글을 소급 전송하지 않는다.
            # 현재 목록을 기준값으로 저장한 뒤 다음 새 글부터 알림을 보낸다.
            if not self.bot.notices.has_category(category):
                for post in group:
                    self.bot.notices.save(post)
                logger.info("공홈 %s 기존 글 %s개 기준값 등록", category, len(group))
                continue

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
