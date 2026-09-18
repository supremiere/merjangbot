import os
import unittest
from unittest.mock import AsyncMock, patch

from config import Settings
from sites.moblife.client import MoblifeClient


class DirectMoblifeTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_proxy_environment_cannot_redirect_requests(self):
        with patch.dict(os.environ, {
            "DISCORD_TOKEN": "test",
            "NOTICE_CHANNEL_ID": "1",
            "ABYSS_CHANNEL_ID": "2",
            "MOBLIFE_PROXY_BASE": "https://moblife-proxy.ninemailz.workers.dev",
        }):
            settings = Settings.from_env(env_file="missing-test-env")

        http = AsyncMock()
        client = MoblifeClient(http, settings)
        await client.get("/d/api/v1/maintenance-status")

        self.assertEqual(client.base_url, "https://mabimobi.life")
        self.assertEqual(
            http.get_json.await_args.args[0],
            "https://mabimobi.life/d/api/v1/maintenance-status",
        )
        self.assertEqual(
            http.get_json.await_args.kwargs["headers"]["Referer"],
            "https://mabimobi.life/",
        )
