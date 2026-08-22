# Telegram service separation

## Status and scope

The repository/local/CI runtime now keeps Telethon out of `arcdb-web`. Telegram
media is owned by one `arcdb-telegram` process and exposed to web workers through
authenticated loopback HTTP. This is implemented and tested in the repository; it
is not evidence that the service is installed or enabled in production.

The split preserves the existing authenticated `/download/<novel>` route. Local
files remain preferred. Only the Telegram fallback crosses the internal boundary.

## Boundary

```text
authenticated browser
  -> arcdb-web /download/...
     -> http://127.0.0.1:5010/v1/media/<channel>/<message>
        Authorization: Bearer <shared random token>
        -> one arcdb-telegram process
           -> one Telethon client/session
```

- `arcdb/app.py` does not import Telethon, create an asyncio loop or start a
  Telegram thread.
- The service rejects media requests without the shared token.
- The web client rejects non-loopback service URLs and does not follow redirects,
  preventing the token from being sent to another host.
- The response streams bounded chunks; neither process materializes the complete
  media file in RAM.
- Service errors returned to users are sanitized and contain no Telegram payload,
  channel details, session path or credentials.
- `healthz` means the service process responds; `readyz` additionally requires a
  connected Telethon runtime.

## Configuration ownership

Use separate private environment files. The web environment owns only:

```env
TELEGRAM_SERVICE_URL=http://127.0.0.1:5010
TELEGRAM_SERVICE_TOKEN=<same random token, at least 32 characters>
TELEGRAM_SERVICE_CONNECT_TIMEOUT=3
TELEGRAM_SERVICE_READ_TIMEOUT=30
```

The Telegram environment owns `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`,
`TELEGRAM_PHONE`, `SESSION_PATH`, the shared token and optional MTProxy settings.
Start from `.env.telegram.example`; never commit the filled file or session.

Generate the token independently from the Flask secret, for example:

```text
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

The production service must bind only to loopback and must run exactly one Gunicorn
worker. Threads can serve concurrent streams without creating additional Telethon
clients. `deploy/systemd/arcdb-telegram.service.example` encodes these constraints.

## Local operation

Telegram stays disabled by default. To test with a protected local session, populate
the Telegram values and token in ignored `.env`, set
`ARCHIVEDB_START_TELEGRAM=1`, and keep `TELEGRAM_SERVICE_URL` on loopback.
`scripts/dev_bootstrap.py` then starts web, packager and Telegram as separate child
processes and fails visibly if either worker exits.

The standalone entrypoint is:

```text
PYTHONPATH=. python scripts/run_telegram.py
```

## Inventory-gated production rollout

1. Confirm the real application user/path, Python environment, session path and
   Block Volume mount with the private production inventory.
2. Back up the protected Telethon session and verify the backup hash without
   publishing the file or path.
3. Create distinct mode-0600 web and Telegram environment files. Put Telegram API
   credentials only in the Telegram file and the same new random service token in
   both.
4. Substitute the explicit inventory-confirmed values in the systemd template.
5. Start only `arcdb-telegram`; require loopback `healthz` and `readyz` to return 200.
6. Add the loopback URL/token to web configuration and restart web. Do not increase
   web worker count during this rollout.
7. Exercise login, library and one authorized local download, then authorized
   translated/raw Telegram downloads. Confirm 404 and service-unavailable behavior.
8. Restart web and prove Telegram remains ready; restart Telegram and prove web
   remains usable while Telegram downloads return a bounded 503, then recover.
9. Observe sanitized service/web errors before considering the repository phase
   production-enabled.

## Rollback

This split changes no user-state schema or Telegram session format. Before enabling
it, retain the previously deployed revision and its environment. To roll back:

1. stop the new Telegram service;
2. redeploy the previous web revision and its previous protected Telegram settings;
3. restart web at the same public endpoint;
4. repeat the authenticated download smoke check;
5. preserve the new service env and session backup for diagnosis—do not delete or
   overwrite either session blindly.

If only Telegram is unhealthy, leave web running: local library/reader/API flows
remain available and Telegram-backed downloads fail closed with HTTP 503.
