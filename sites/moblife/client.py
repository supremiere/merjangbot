# 모비라이프의 프록시·OpenAPI 주소와 인증 헤더를 관리합니다.
class MoblifeClient:
    def __init__(self, http, settings):
        self.http = http
        self.base_url = settings.moblife_proxy_base or "https://mabimobi.life"
        self.api_key = settings.moblife_api_key
        self.eab_key = settings.moblife_eab_key
        self.headers = {
            "Accept": "application/json",
            "User-Agent": "머장봇/1.0",
            "Referer": self.base_url + "/",
        }

    async def get(self, path, *, params=None):
        return await self.http.get_json(
            self.base_url + path, headers=self.headers, params=params
        )

    async def get_openapi(self, path, *, params=None):
        if not self.api_key:
            raise RuntimeError("MOBLIFE_API_KEY_MISSING")
        headers = {
            "Accept": "application/json",
            "User-Agent": "머장봇/1.0",
            "Authorization": f"Bearer {self.api_key}",
        }
        return await self.http.get_json(
            "https://open.mabimobi.life/v1" + path, headers=headers, params=params
        )
