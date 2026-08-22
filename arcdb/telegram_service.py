"""Single-owner Telethon service exposed only through authenticated loopback HTTP."""

from __future__ import annotations

import asyncio
import atexit
from dataclasses import dataclass
import logging
import os
import queue
import secrets
import threading
from typing import Iterator

from flask import Flask, Response, jsonify, request
from telethon import TelegramClient, connection
from telethon.errors import AuthKeyUnregisteredError

from arcdb.telegram_gateway import TelegramGateway


DOWNLOAD_CHUNK_SIZE = 512 * 1024
LOGGER = logging.getLogger("arcdb.telegram")


class TelegramServiceError(RuntimeError):
    def __init__(self, code: str, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class TelegramMedia:
    filename: str
    file_size: int | None
    chunks: Iterator[bytes]


class TelegramRuntime:
    """Own exactly one Telethon client and its asyncio loop in this process."""

    def __init__(
        self,
        *,
        api_id: int,
        api_hash: str,
        phone: str,
        session_path: str,
        proxy: tuple[str, int, str] | None = None,
    ) -> None:
        self.api_id = api_id
        self.api_hash = api_hash
        self.phone = phone
        self.session_path = session_path
        self.proxy = proxy
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._startup_finished = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None
        self._thread: threading.Thread | None = None
        self._startup_error: BaseException | None = None

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set() and self._thread is not None and self._thread.is_alive()

    def start(self, timeout: int = 45) -> None:
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("Telegram runtime has already been started")
            self._thread = threading.Thread(
                target=self._run,
                name="arcdb-telegram-client",
                daemon=True,
            )
            self._thread.start()
        if not self._startup_finished.wait(max(1, timeout)):
            raise RuntimeError("Telegram client startup timed out")
        if self._startup_error is not None:
            raise RuntimeError("Telegram client startup failed") from None
        if not self.is_ready:
            raise RuntimeError("Telegram client did not become ready")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        kwargs = {
            "loop": loop,
            "receive_updates": False,
            "connection_retries": 3,
            "request_retries": 1,
            "timeout": 8,
        }
        if self.proxy:
            kwargs["connection"] = connection.ConnectionTcpMTProxyRandomizedIntermediate
            kwargs["proxy"] = self.proxy
        async def boot() -> None:
            await self._client.start(phone=self.phone)
            await self._client.get_dialogs()

        try:
            self._client = TelegramClient(
                self.session_path,
                self.api_id,
                self.api_hash,
                **kwargs,
            )
            loop.run_until_complete(boot())
            self._ready.set()
        except BaseException as exc:
            self._startup_error = exc
            LOGGER.error(
                "event=startup_error error_type=%s", type(exc).__name__
            )
        finally:
            self._startup_finished.set()

        if self._ready.is_set():
            heartbeat = loop.create_task(self._heartbeat())
            try:
                loop.run_forever()
            finally:
                heartbeat.cancel()
                loop.run_until_complete(asyncio.gather(heartbeat, return_exceptions=True))
                loop.run_until_complete(self._disconnect())
        else:
            loop.run_until_complete(self._disconnect())
        loop.close()
        self._ready.clear()

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(150)
            try:
                if not self._client.is_connected():
                    await asyncio.wait_for(self._client.connect(), timeout=8.0)
                else:
                    await asyncio.wait_for(self._client.get_me(), timeout=5.0)
            except AuthKeyUnregisteredError:
                self._ready.clear()
                return
            except Exception:
                continue

    async def _disconnect(self) -> None:
        if self._client is not None and self._client.is_connected():
            await self._client.disconnect()

    def stop(self, timeout: int = 10) -> None:
        loop = self._loop
        thread = self._thread
        if loop is None or thread is None or not thread.is_alive():
            return
        future = asyncio.run_coroutine_threadsafe(self._disconnect(), loop)
        try:
            future.result(timeout=max(1, timeout // 2))
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=max(1, timeout))

    def open_media(self, channel_id: int, message_id: int) -> TelegramMedia:
        if not self.is_ready or self._loop is None or self._client is None:
            raise TelegramServiceError("not_ready", 503)

        async def fetch():
            return await asyncio.wait_for(
                self._client.get_messages(channel_id, ids=message_id), timeout=10.0
            )

        future = asyncio.run_coroutine_threadsafe(fetch(), self._loop)
        try:
            message = future.result(timeout=15)
        except TimeoutError as exc:
            raise TelegramServiceError("upstream_timeout", 504) from exc
        except Exception as exc:
            raise TelegramServiceError("upstream_unavailable", 503) from exc
        if not message or not message.media:
            raise TelegramServiceError("media_not_found", 404)

        filename = (
            message.file.name
            if message.file and getattr(message.file, "name", None)
            else "file.epub"
        )
        size = (
            int(message.file.size)
            if message.file and getattr(message.file, "size", None)
            else None
        )
        return TelegramMedia(filename, size, self._stream_chunks(message.media))

    def _stream_chunks(self, media) -> Iterator[bytes]:
        chunk_queue: queue.Queue[bytes | object] = queue.Queue(maxsize=10)
        sentinel = object()
        abort = threading.Event()

        async def produce() -> None:
            try:
                async for chunk in self._client.iter_download(
                    media, chunk_size=DOWNLOAD_CHUNK_SIZE
                ):
                    while not abort.is_set():
                        try:
                            chunk_queue.put_nowait(chunk)
                            break
                        except queue.Full:
                            await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.error(
                    "event=stream_error error_type=%s", type(exc).__name__
                )
            finally:
                while not abort.is_set():
                    try:
                        chunk_queue.put_nowait(sentinel)
                        break
                    except queue.Full:
                        await asyncio.sleep(0.05)

        future = asyncio.run_coroutine_threadsafe(produce(), self._loop)
        try:
            while True:
                item = chunk_queue.get(timeout=30)
                if item is sentinel:
                    break
                yield item
        except (GeneratorExit, queue.Empty):
            abort.set()
        finally:
            abort.set()
            future.cancel()


def create_service_app(runtime, token: str) -> Flask:
    if len(token) < 32:
        raise ValueError("TELEGRAM_SERVICE_TOKEN must contain at least 32 characters")
    app = Flask("arcdb-telegram")

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    @app.get("/readyz")
    def readyz():
        status = 200 if runtime.is_ready else 503
        return jsonify({"status": "ready" if status == 200 else "not_ready"}), status

    @app.get("/v1/media/<channel_text>/<message_text>")
    def media(channel_text: str, message_text: str):
        supplied = request.headers.get("Authorization", "")
        expected = f"Bearer {token}"
        if not secrets.compare_digest(supplied, expected):
            return jsonify({"status": "error", "error": "unauthorized"}), 401
        try:
            channel_id = int(channel_text)
            message_id = int(message_text)
            if message_id <= 0:
                raise ValueError
        except ValueError:
            return jsonify({"status": "error", "error": "invalid_media_id"}), 400
        try:
            result = runtime.open_media(channel_id, message_id)
        except TelegramServiceError as exc:
            return jsonify({"status": "error", "error": exc.code}), exc.status_code

        headers = {
            "X-ArchiveDB-Filename": TelegramGateway.encode_filename(result.filename),
            "Cache-Control": "no-store",
        }
        if result.file_size:
            headers["Content-Length"] = str(result.file_size)
        return Response(
            result.chunks,
            mimetype="application/epub+zip",
            headers=headers,
            direct_passthrough=True,
        )

    return app


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required for arcdb-telegram")
    return value


def create_configured_app() -> Flask:
    try:
        api_id = int(_required_env("TELEGRAM_API_ID"))
    except ValueError as exc:
        raise RuntimeError("TELEGRAM_API_ID must be an integer") from exc
    proxy = None
    if os.environ.get("USE_PROXY", "0") == "1":
        server = _required_env("MTPROXY_SERVER")
        secret = _required_env("MTPROXY_SECRET")
        try:
            port = int(os.environ.get("MTPROXY_PORT", "443"))
        except ValueError as exc:
            raise RuntimeError("MTPROXY_PORT must be an integer") from exc
        proxy = (server, port, secret)

    runtime = TelegramRuntime(
        api_id=api_id,
        api_hash=_required_env("TELEGRAM_API_HASH"),
        phone=_required_env("TELEGRAM_PHONE"),
        session_path=_required_env("SESSION_PATH"),
        proxy=proxy,
    )
    runtime.start(timeout=int(os.environ.get("TELEGRAM_STARTUP_TIMEOUT_SECONDS", "45")))
    atexit.register(runtime.stop)
    app = create_service_app(runtime, _required_env("TELEGRAM_SERVICE_TOKEN"))
    app.extensions["arcdb_telegram_runtime"] = runtime
    return app
