import unittest
from unittest.mock import AsyncMock, MagicMock, Mock

from multidict import CIMultiDict

from sites.http import HttpClient, HttpError


class HttpDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_error_keeps_stage_headers_and_bounded_json_body(self):
        response = Mock()
        response.status = 403
        response.charset = "utf-8"
        response.headers = CIMultiDict({
            "X-Merjang-Proxy": "moblife",
            "X-Merjang-Stage": "upstream",
            "Server": "cloudflare",
            "CF-Ray": "sample-ray",
        })
        response.content.read = AsyncMock(return_value=b'{"stage":"upstream"}')
        session = MagicMock()
        session.closed = False
        session.get.return_value.__aenter__ = AsyncMock(return_value=response)
        session.get.return_value.__aexit__ = AsyncMock(return_value=None)
        client = HttpClient()
        client.session = session

        with self.assertRaises(HttpError) as caught:
            await client.get_text("https://example.com/d/api/v1/maintenance-status")

        message = str(caught.exception)
        self.assertIn("x-merjang-stage=upstream", message)
        self.assertIn("x-merjang-proxy=moblife", message)
        self.assertIn("cf-ray=sample-ray", message)
        self.assertIn('body=\'{"stage":"upstream"}\'', message)
        response.content.read.assert_awaited_once_with(1024)

    def test_error_without_worker_headers_marks_them_missing(self):
        message = str(HttpError(403, "https://example.com", headers={
            "server": "cloudflare", "cf-mitigated": "challenge"
        }, body="blocked"))
        self.assertIn("x-merjang-stage=-", message)
        self.assertIn("x-merjang-proxy=-", message)
        self.assertIn("cf-mitigated=challenge", message)
