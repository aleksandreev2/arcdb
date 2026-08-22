from __future__ import annotations

import hashlib
import http.cookiejar
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.environ.get("ARCHIVEDB_TEST_BASE_URL", "http://127.0.0.1:5004")
ORIGIN = os.environ.get("ARCHIVEDB_TEST_ORIGIN", BASE_URL).rstrip("/")


def main() -> int:
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    login = urllib.request.Request(
        BASE_URL + "/login",
        data=urllib.parse.urlencode(
            {"email": "dev@arcdb.local", "password": "arcdb-dev-123"}
        ).encode("utf-8"),
        headers={"Origin": ORIGIN},
    )
    with opener.open(login, timeout=30) as response:
        response.read()

    with opener.open(BASE_URL + "/", timeout=30) as response:
        page = response.read().decode("utf-8")
        csp = response.headers.get("Content-Security-Policy", "")

    directives = {
        parts[0]: parts[1:]
        for raw in csp.split(";")
        if (parts := raw.strip().split())
    }
    script_sources = directives.get("script-src", [])
    nonce_sources = [source for source in script_sources if source.startswith("'nonce-")]
    assert len(nonce_sources) == 1, csp
    assert "'self'" in script_sources and "'unsafe-inline'" not in script_sources, csp
    assert directives.get("script-src-attr") == ["'none'"], csp
    assert directives.get("style-src") == ["'self'"], csp
    assert directives.get("style-src-attr") == ["'none'"], csp
    assert "cdnjs.cloudflare.com" not in csp and "JSZip" not in page, csp

    header_nonce = nonce_sources[0][len("'nonce-"):-1]
    page_nonces = re.findall(r'<script\b[^>]*\bnonce="([^"]+)"', page, re.IGNORECASE)
    assert page_nonces and set(page_nonces) == {header_nonce}, page_nonces
    assert re.search(r"\son[a-z]+\s*=", page, re.IGNORECASE) is None

    css_dir = ROOT / "arcdb" / "static" / "css"
    for name in (
        "auth.css", "gallery.css", "reader.css", "community.css", "admin-access.css"
    ):
        asset = css_dir / name
        version = hashlib.sha256(asset.read_bytes()).hexdigest()[:16]
        with opener.open(
            f"{BASE_URL}/static/css/{name}?v={version}", timeout=30
        ) as response:
            served = response.read()
            cache_control = response.headers.get("Cache-Control", "")
        assert served == asset.read_bytes()
        assert cache_control == "public, max-age=31536000, immutable", (
            name, cache_control
        )

    with opener.open(f"{BASE_URL}/static/css/auth.css", timeout=30) as response:
        response.read()
        unversioned_cache = response.headers.get("Cache-Control", "")
    assert "immutable" not in unversioned_cache, unversioned_cache
    with opener.open(
        f"{BASE_URL}/static/css/auth.css?v={'0' * 16}", timeout=30
    ) as response:
        response.read()
        wrong_version_cache = response.headers.get("Cache-Control", "")
    assert "immutable" not in wrong_version_cache, wrong_version_cache
    assert re.search(r"\sstyle\s*=", page, re.IGNORECASE) is None
    assert re.search(r"<style\b", page, re.IGNORECASE) is None
    print("Runtime script/style CSP and fingerprinted static asset workflow passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
