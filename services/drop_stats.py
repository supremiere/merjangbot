"""Read-only authenticated access to the DungeonHelper statistics service."""
from urllib.parse import urlsplit

import aiohttp

class DropStatsService:
    def __init__(self, origin, key):
        self.origin,self.key = origin.rstrip('/'),key

    async def fetch(self,user_id=None):
        parsed = urlsplit(self.origin)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.path or parsed.query
                or parsed.fragment or parsed.username or not self.key):
            raise RuntimeError('통계 서버가 아직 연결되지 않았습니다.')
        params = {'user_id':str(user_id)} if user_id is not None else {}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as http:
                async with http.get(self.origin + '/drop-stats',params=params,
                        headers={'Authorization':'Bearer ' + self.key},allow_redirects=False) as response:
                    if response.status != 200:
                        raise RuntimeError('통계 서버에 연결할 수 없습니다. 잠시 후 다시 시도해주세요.')
                    if response.content_length is not None and response.content_length > 65536:
                        raise RuntimeError('통계 응답을 확인할 수 없습니다.')
                    raw = bytearray()
                    async for part in response.content.iter_chunked(8192):
                        raw.extend(part)
                        if len(raw) > 65536:
                            raise RuntimeError('통계 응답을 확인할 수 없습니다.')
                    import json
                    return json.loads(raw)
        except (aiohttp.ClientError,TimeoutError,ValueError):
            raise RuntimeError('통계 서버에 연결할 수 없습니다. 잠시 후 다시 시도해주세요.') from None
