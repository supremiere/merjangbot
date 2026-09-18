# 모비라이프의 프록시·OpenAPI 주소와 인증 헤더를 관리합니다.
import logging

logger = logging.getLogger(__name__)

DIRECT_BASE = "https://mabimobi.life"


class MoblifeClient:
    def __init__(self, http, settings):
        self.http = http
        self.base_url = settings.moblife_proxy_base or DIRECT_BASE
        self.api_key = settings.moblife_api_key
        self.eab_key = settings.moblife_eab_key

    def _headers(self, base_url):
        return {
            "Accept": "application/json",
            "User-Agent": "머장봇/1.0",
            "Referer": base_url + "/",
        }

    async def get(self, path, *, params=None):
        # 평소에는 기존 프록시를 우선 사용합니다.
        try:
            return await self.http.get_json(
                self.base_url + path,
                headers=self._headers(self.base_url),
                params=params,
            )
        except Exception as proxy_error:
            # 프록시 자체가 죽었거나 라우트가 깨졌을 때만 본서버로 한 번 우회합니다.
            # 이미 본서버를 직접 쓰는 설정이면 같은 요청을 반복하지 않습니다.
            if self.base_url.rstrip("/") == DIRECT_BASE:
                raise

            logger.warning(
                "모비라이프 프록시 요청 실패, 본서버로 우회: %s (%s)",
                path,
                proxy_error,
            )
            try:
                return await self.http.get_json(
                    DIRECT_BASE + path,
                    headers=self._headers(DIRECT_BASE),
                    params=params,
                )
            except Exception:
                # 최종 예외는 원래 프록시 오류가 아니라 실제 우회 실패 원인이 보이도록 전달합니다.
                logger.exception("모비라이프 본서버 우회도 실패: %s", path)
                raise

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
