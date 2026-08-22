from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path

from werkzeug.security import check_password_hash

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.environ.get("ARCHIVEDB_TEST_BASE_URL", "http://127.0.0.1:5004").rstrip("/")
ORIGIN = os.environ.get("ARCHIVEDB_TEST_ORIGIN", "http://127.0.0.1:5004")
META = ROOT / "data" / "metadata"
USERS_PATH = META / "users.json"
DB_PATH = ROOT / "data" / "arcdb.sqlite3"


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def recover_fixture_code(code_hash: str, secret: str) -> str:
    """Recover only a six-digit local CI fixture code from its one-way stored hash."""
    for value in range(1_000_000):
        candidate = f"{value:06d}"
        digest = hashlib.sha256(f"{secret}:{candidate}".encode("utf-8")).hexdigest()
        if digest == code_hash:
            return candidate
    raise AssertionError("Could not recover the local fixture auth code.")


class Client:
    def __init__(self) -> None:
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)
        )

    def get(self, path: str):
        return self.opener.open(BASE_URL + path, timeout=30)

    def post(self, path: str, fields: dict[str, str]):
        body = urllib.parse.urlencode(fields).encode("utf-8")
        request = urllib.request.Request(
            BASE_URL + path,
            data=body,
            headers={"Origin": ORIGIN},
            method="POST",
        )
        return self.opener.open(request, timeout=30)


def load_users() -> dict:
    data = json.loads(USERS_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict), data
    return data


def main() -> int:
    env = parse_env(ROOT / ".env")
    secret = env["FLASK_SECRET_KEY"]
    admin_email = env.get("LOCAL_DEV_EMAIL", "dev@arcdb.local")
    admin_password = env.get("LOCAL_DEV_PASSWORD", "arcdb-dev-123")
    email = f"auth-flow-{time.time_ns()}@arcdb.local"
    original_password = "ci-auth-original-123"
    replacement_password = "ci-auth-replacement-456"

    admin = Client()
    response = admin.post(
        "/login", {"email": admin_email, "password": admin_password}
    )
    assert urllib.parse.urlparse(response.geturl()).path == "/", response.geturl()
    response = admin.post(
        "/admin/access", {"action": "add", "emails": email}
    )
    assert response.status == 200, response.status

    client = Client()
    response = client.post(
        "/register", {"email": email, "password": original_password}
    )
    assert urllib.parse.urlparse(response.geturl()).path == "/verify", response.geturl()
    registered = load_users()[email]
    original_hash = registered["pwd_hash"]
    assert check_password_hash(original_hash, original_password)
    assert registered["verified"] is False
    verification_code = recover_fixture_code(registered["code_hash"], secret)

    response = client.post(
        "/verify", {"email": email, "code": verification_code}
    )
    assert urllib.parse.urlparse(response.geturl()).path == "/", response.geturl()
    verified = load_users()[email]
    assert verified["verified"] is True
    assert verified["pwd_hash"] == original_hash
    assert not {"code_hash", "code_expires", "code_attempts"} & set(verified)

    client.post("/logout", {})
    response = client.post(
        "/login", {"email": email, "password": original_password}
    )
    assert urllib.parse.urlparse(response.geturl()).path == "/", response.geturl()
    client.post("/logout", {})

    response = client.post("/forgot", {"email": email})
    assert urllib.parse.urlparse(response.geturl()).path == "/reset_password", response.geturl()
    reset_pending = load_users()[email]
    reset_code = recover_fixture_code(reset_pending["reset_code_hash"], secret)
    assert reset_pending["reset_code_attempts"] == 0

    wrong_code = "000000" if reset_code != "000000" else "000001"
    response = client.post(
        "/reset_password",
        {"email": email, "code": wrong_code, "password": replacement_password},
    )
    assert response.status == 200, response.status
    assert load_users()[email]["reset_code_attempts"] == 1

    response = client.post(
        "/reset_password",
        {"email": email, "code": reset_code, "password": replacement_password},
    )
    assert urllib.parse.urlparse(response.geturl()).path == "/", response.geturl()
    reset = load_users()[email]
    assert reset["verified"] is True
    assert reset["pwd_hash"] != original_hash
    assert check_password_hash(reset["pwd_hash"], replacement_password)
    assert not {
        "reset_code_hash",
        "reset_code_expires",
        "reset_code_attempts",
    } & set(reset)

    client.post("/logout", {})
    response = client.post(
        "/login", {"email": email, "password": replacement_password}
    )
    assert urllib.parse.urlparse(response.geturl()).path == "/", response.geturl()

    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT pwd_hash, verified, payload_json FROM users WHERE email=?",
            (email,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == reset["pwd_hash"]
    assert row[1] == 1
    assert json.loads(row[2]) == reset
    print("Runtime registration, verification, login and password-reset parity passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
