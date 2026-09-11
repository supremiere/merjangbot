# 사이트 조회용 HTTP 세션을 재사용하고 타임아웃·응답 오류를 처리합니다.
import json

import aiohttp


class HttpError(RuntimeError):
    def __init__(self, status, url):
        self.status = status
        super().__init__(f"외부 데이터 요청 실패 (HTTP {status}): {url}")


class HttpClient:
    def __init__(self):
        self.session = None

    async def start(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20)
            )

    async def close(self):
        if self.session is not None and not self.session.closed:
            await self.session.close()

    async def get_text(self, url, *, headers=None, params=None):
        if self.session is None or self.session.closed:
            raise RuntimeError("HTTP 클라이언트가 초기화되지 않았습니다.")
        async with self.session.get(url, headers=headers, params=params) as response:
            if response.status != 200:
                raise HttpError(response.status, url)
            return await response.text()

    async def get_json(self, url, *, headers=None, params=None):
        return json.loads(await self.get_text(url, headers=headers, params=params))
