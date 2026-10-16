"""Server-to-server access management; never use the statistics read key."""
import json
import re
from urllib.parse import urlsplit

import aiohttp

DISCORD_ID = re.compile(r'^[0-9]{17,20}$')


class SpecialUsersService:
    def __init__(self, origin, key):
        self.origin, self.key = origin.rstrip('/'), key

    async def request(self, *, action=None, user_id=None, after=None):
        parsed = urlsplit(self.origin)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.path or parsed.query
                or parsed.fragment or parsed.username is not None or len(self.key) < 32):
            raise RuntimeError('특별 허용 관리 서버 설정을 확인해주세요.')
        if ((action is not None and (action not in {'add', 'remove'} or not DISCORD_ID.fullmatch(user_id or '')))
                or (after and not DISCORD_ID.fullmatch(after))):
            raise RuntimeError('Discord 사용자 ID는 17~20자리 숫자로 입력해주세요.')
        options = {'headers': {'Authorization': 'Bearer '+self.key}, 'allow_redirects': False}
        if action is not None:
            options['json'] = {'action': action, 'user_id': user_id}
        else:
            options['params'] = {'after': after} if after else {}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as http:
                async with http.request('POST' if action else 'GET', self.origin+'/admin/special-users', **options) as response:
                    if response.status != 200:
                        raise RuntimeError('특별 허용 관리 요청에 실패했습니다. 서버 설정과 연결을 확인해주세요.')
                    raw = bytearray()
                    async for part in response.content.iter_chunked(8192):
                        raw.extend(part)
                        if len(raw) > 32768:
                            raise ValueError()
                    data = json.loads(raw)
                    if action:
                        if (data.get('ok') is not True or data.get('user_id') != user_id
                                or not isinstance(data.get('changed'), bool)):
                            raise ValueError()
                    else:
                        users = data.get('users')
                        if (not isinstance(users, list) or len(users) > 25
                                or any(not isinstance(row, dict) or not DISCORD_ID.fullmatch(row.get('discord_user_id', '')) for row in users)
                                or (data.get('next_cursor') is not None and not DISCORD_ID.fullmatch(data['next_cursor']))):
                            raise ValueError()
                    return data
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError, AttributeError):
            raise RuntimeError('특별 허용 관리 서버 응답을 확인할 수 없습니다. 잠시 후 다시 시도해주세요.') from None
