"""Authenticated loopback client used by the web process for Telegram media."""

from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlsplit

import requests


class TelegramGatewayError(RuntimeError):
    """A sanitized failure returned to the ArchiveDB web route."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class TelegramGateway:
    """Stream media from the separately managed loopback Telegram service."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        connect_timeout: int = 3,
        read_timeout: int = 30,
        session: requests.Session | None = None,
    ) -> None:
        parsed = urlsplit(base_url.rstrip("/"))
        if parsed.scheme != "http" or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("TELEGRAM_SERVICE_URL must use loopback HTTP")
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError(
                "TELEGRAM_SERVICE_URL must not contain credentials, a path, or a query"
            )
        if len(token) < 32:
            raise ValueError("TELEGRAM_SERVICE_TOKEN must contain at least 32 characters")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.connect_timeout = max(1, int(connect_timeout))
        self.read_timeout = max(1, int(read_timeout))
        self.session = session or requests.Session()

    def open_media(self, channel_id: int, message_id: int):
        if isinstance(channel_id, bool) or not isinstance(channel_id, int):
            raise ValueError("channel_id must be an integer")
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            raise ValueError("message_id must be a positive integer")
        url = f"{self.base_url}/v1/media/{channel_id}/{message_id}"
        try:
            response = self.session.get(
                url,
                headers={"Authorization": f"Bearer {self.token}"},
                stream=True,
                allow_redirects=False,
                timeout=(self.connect_timeout, self.read_timeout),
            )
        except requests.Timeout as exc:
            raise TelegramGatewayError("Timeout fetching from Telegram.", 504) from exc
        except requests.RequestException as exc:
            raise TelegramGatewayError("Telegram service unavailable.", 503) from exc

        if response.status_code == 200:
            return response
        response.close()
        if response.status_code == 404:
            raise TelegramGatewayError("File missing from channel.", 404)
        if response.status_code == 504:
            raise TelegramGatewayError("Timeout fetching from Telegram.", 504)
        if response.status_code in {401, 403, 502, 503}:
            raise TelegramGatewayError("Telegram service unavailable.", 503)
        raise TelegramGatewayError("Telegram download failed.", 502)

    @staticmethod
    def response_filename(response, fallback: str = "file.epub") -> str:
        encoded = response.headers.get("X-ArchiveDB-Filename", "")
        if not encoded or len(encoded) > 2048:
            return fallback
        name = PurePosixPath(unquote(encoded).replace("\\", "/")).name.strip()
        return name or fallback

    @staticmethod
    def response_length(response) -> str | None:
        value = response.headers.get("Content-Length", "")
        if not value.isdigit() or int(value) <= 0:
            return None
        return value

    @staticmethod
    def encode_filename(filename: str) -> str:
        """Shared header encoding used by service tests and implementations."""
        return quote(filename, safe="")
