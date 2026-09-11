# 룬 통계를 조회하고 메모리·영속 캐시를 갱신하며 실패 시 이전 데이터를 유지합니다.
import asyncio

from .models import RuneStats
from .parser import parse_page

RUNE_STATS_URL = "https://erinndata.pages.dev/cheatsheet/"


class RuneStatsService:
    def __init__(self, http, repository):
        self.http = http
        self.repository = repository
        self.cache: dict[str, RuneStats] = {}
        self._refresh_lock = asyncio.Lock()

    def load_cache(self):
        self.cache = self.repository.load()

    async def refresh(self, *, missing_class=None):
        async with self._refresh_lock:
            # 동시에 요청된 직업이 먼저 갱신됐다면 같은 페이지를 다시 요청하지 않습니다.
            if missing_class is not None and missing_class in self.cache:
                return self.cache
            html = await self.http.get_text(
                RUNE_STATS_URL,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; MerjangBot/1.0)",
                    "Accept": "text/html,application/xhtml+xml",
                },
            )
            parsed = parse_page(html)
            # 정상 데이터가 저장된 뒤에 메모리 캐시도 교체합니다.
            self.repository.save(parsed)
            self.cache = parsed
            return parsed

    async def get_class(self, class_name):
        if class_name not in self.cache:
            await self.refresh(missing_class=class_name)
        return self.cache.get(class_name)
