from __future__ import annotations

from pathlib import Path
import os
import unittest
from unittest import mock

import requests

from arcdb.telegram_gateway import TelegramGateway, TelegramGatewayError
from arcdb.telegram_service import (
    TelegramMedia,
    TelegramRuntime,
    TelegramServiceError,
    create_configured_app,
    create_service_app,
)


ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, status_code=200, *, headers=None, chunks=()) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.chunks = list(chunks)
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def iter_content(self, chunk_size):
        del chunk_size
        yield from self.chunks


class FakeSession:
    def __init__(self, response=None, error=None) -> None:
        self.response = response
        self.error = error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


class TelegramGatewayTests(unittest.TestCase):
    def test_gateway_allows_only_loopback_and_requires_token(self) -> None:
        for url in ("https://127.0.0.1:5010", "http://example.com:5010"):
            with self.assertRaises(ValueError):
                TelegramGateway(url, "x" * 32)
        with self.assertRaises(ValueError):
            TelegramGateway("http://127.0.0.1:5010", "")

    def test_gateway_authenticates_stream_without_redirects(self) -> None:
        response = FakeResponse(
            headers={
                "X-ArchiveDB-Filename": "folder%2Fbook.epub",
                "Content-Length": "7",
            },
            chunks=(b"payload",),
        )
        session = FakeSession(response=response)
        gateway = TelegramGateway(
            "http://127.0.0.1:5010", "s" * 32, session=session
        )
        opened = gateway.open_media(-100123, 5)
        self.assertIs(opened, response)
        url, kwargs = session.calls[0]
        self.assertEqual(url, "http://127.0.0.1:5010/v1/media/-100123/5")
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer " + "s" * 32})
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["stream"])
        self.assertEqual(gateway.response_filename(opened), "book.epub")
        self.assertEqual(gateway.response_length(opened), "7")

    def test_gateway_fails_closed_and_closes_error_response(self) -> None:
        missing = FakeResponse(status_code=404)
        gateway = TelegramGateway(
            "http://localhost:5010", "s" * 32, session=FakeSession(response=missing)
        )
        with self.assertRaises(TelegramGatewayError) as caught:
            gateway.open_media(-1, 1)
        self.assertEqual(caught.exception.status_code, 404)
        self.assertTrue(missing.closed)

        timeout_gateway = TelegramGateway(
            "http://[::1]:5010",
            "s" * 32,
            session=FakeSession(error=requests.Timeout("private upstream detail")),
        )
        with self.assertRaises(TelegramGatewayError) as timeout:
            timeout_gateway.open_media(-1, 1)
        self.assertEqual(str(timeout.exception), "Timeout fetching from Telegram.")
        self.assertNotIn("private", str(timeout.exception))


class FakeRuntime:
    def __init__(self) -> None:
        self.is_ready = True
        self.calls = []
        self.error = None

    def open_media(self, channel_id, message_id):
        self.calls.append((channel_id, message_id))
        if self.error:
            raise self.error
        return TelegramMedia("novel ü.epub", 6, iter((b"abc", b"def")))


class TelegramServiceAppTests(unittest.TestCase):
    TOKEN = "t" * 32

    def setUp(self) -> None:
        self.runtime = FakeRuntime()
        self.client = create_service_app(self.runtime, self.TOKEN).test_client()

    def auth(self):
        return {"Authorization": f"Bearer {self.TOKEN}"}

    def test_health_readiness_auth_and_streaming(self) -> None:
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(self.client.get("/readyz").status_code, 200)
        self.assertEqual(self.client.get("/v1/media/-1001/7").status_code, 401)

        response = self.client.get("/v1/media/-1001/7", headers=self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"abcdef")
        self.assertEqual(response.headers["Content-Length"], "6")
        self.assertEqual(self.runtime.calls, [(-1001, 7)])

    def test_not_ready_and_upstream_errors_are_sanitized(self) -> None:
        self.runtime.is_ready = False
        self.assertEqual(self.client.get("/readyz").status_code, 503)
        self.runtime.error = TelegramServiceError("media_not_found", 404)
        response = self.client.get("/v1/media/-1/2", headers=self.auth())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"], "media_not_found")
        self.assertEqual(
            self.client.get("/v1/media/nope/2", headers=self.auth()).status_code, 400
        )

    def test_runtime_rejects_duplicate_start(self) -> None:
        runtime = TelegramRuntime(
            api_id=1,
            api_hash="hash",
            phone="phone",
            session_path="session",
        )
        runtime._thread = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, "already been started"):
            runtime.start()

    def test_configured_factory_starts_one_runtime_explicitly(self) -> None:
        configured = {
            "TELEGRAM_API_ID": "123",
            "TELEGRAM_API_HASH": "hash",
            "TELEGRAM_PHONE": "+10000000000",
            "SESSION_PATH": "private-session",
            "TELEGRAM_SERVICE_TOKEN": self.TOKEN,
        }
        fake = mock.Mock()
        fake.is_ready = True
        with mock.patch.dict(os.environ, configured, clear=True), mock.patch(
            "arcdb.telegram_service.TelegramRuntime", return_value=fake
        ) as runtime_class:
            app = create_configured_app()
        runtime_class.assert_called_once_with(
            api_id=123,
            api_hash="hash",
            phone="+10000000000",
            session_path="private-session",
            proxy=None,
        )
        fake.start.assert_called_once_with(timeout=45)
        self.assertIs(app.extensions["arcdb_telegram_runtime"], fake)


class TelegramIsolationTests(unittest.TestCase):
    def test_web_runtime_has_no_telethon_client_or_background_loop(self) -> None:
        web = (ROOT / "arcdb" / "app.py").read_text(encoding="utf-8")
        for forbidden in (
            "TelegramClient",
            "from telethon",
            "telethon_loop",
            "run_coroutine_threadsafe",
            "client.iter_download",
            "ARCHIVEDB_NO_TELEGRAM",
            "TELEGRAM_API_HASH",
            "TELEGRAM_PHONE",
            "SESSION_PATH",
        ):
            self.assertNotIn(forbidden, web)
        self.assertIn("TELEGRAM_GATEWAY.open_media", web)


if __name__ == "__main__":
    unittest.main()
