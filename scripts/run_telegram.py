"""Run the single-owner Telegram service for local development."""

from __future__ import annotations

import os

from werkzeug.serving import make_server

from arcdb.telegram_service import create_configured_app


def main() -> int:
    host = os.environ.get("TELEGRAM_SERVICE_HOST", "127.0.0.1").strip()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("arcdb-telegram must bind to a loopback address")
    port = int(os.environ.get("TELEGRAM_SERVICE_PORT", "5010"))
    app = create_configured_app()
    server = make_server(host, port, app, threaded=True)
    try:
        server.serve_forever()
    finally:
        app.extensions["arcdb_telegram_runtime"].stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
