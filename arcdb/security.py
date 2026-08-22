"""Browser-origin validation for state-changing web requests."""

from __future__ import annotations

from urllib.parse import urlsplit


class OriginConfigurationError(ValueError):
    pass


def canonical_origin(value: str, *, origin_only: bool = False) -> str | None:
    raw = (value or "").strip()
    if not raw or raw == "null" or any(char.isspace() for char in raw):
        return None
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password or parsed.fragment:
        return None
    if origin_only and (parsed.path not in {"", "/"} or parsed.query):
        return None
    default_port = 80 if parsed.scheme == "http" else 443
    authority = parsed.hostname.casefold()
    if ":" in authority:
        authority = f"[{authority}]"
    if port is not None and port != default_port:
        authority += f":{port}"
    return f"{parsed.scheme}://{authority}"


def parse_allowed_origins(raw: str | None) -> frozenset[str]:
    origins = set()
    for value in (raw or "").split(","):
        if not value.strip():
            continue
        normalized = canonical_origin(value, origin_only=True)
        if normalized is None:
            raise OriginConfigurationError("Allowed origins must be absolute HTTP(S) origins")
        origins.add(normalized)
    return frozenset(origins)


def request_source_allowed(
    *,
    origin: str | None,
    referer: str | None,
    host: str,
    allowed_origins: frozenset[str],
) -> bool:
    source = canonical_origin(origin or "")
    if source is None and not origin:
        source = canonical_origin(referer or "")
    if source is None:
        return False
    if allowed_origins:
        return source in allowed_origins
    try:
        request_host = urlsplit("//" + host).hostname
    except ValueError:
        return False
    source_host = urlsplit(source).hostname
    return bool(
        request_host
        and source_host
        and request_host.casefold() == source_host.casefold()
    )
