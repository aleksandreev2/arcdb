from __future__ import annotations

import http.cookiejar
import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from flask import Flask
from flask.sessions import SecureCookieSessionInterface

from arcdb.storage.state_parity import verify_metadata_domains_parity


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.environ.get("ARCHIVEDB_TEST_BASE_URL", "http://127.0.0.1:5004")
ORIGIN = os.environ.get("ARCHIVEDB_TEST_ORIGIN", "http://127.0.0.1:5004")
META_DIR = ROOT / "data" / "metadata"
DB_PATH = ROOT / "data" / "arcdb.sqlite3"
TEST_EMAIL = "phase2c-reader@example.test"


def fixture_secret() -> str:
    for raw_line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "FLASK_SECRET_KEY":
            return value.strip().strip('"').strip("'")
    raise AssertionError("FLASK_SECRET_KEY is missing from the CI fixture environment.")


class ApiClient:
    def __init__(self) -> None:
        cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cookies)
        )

    def authenticate_fixture(self, email: str) -> None:
        fixture_app = Flask("arcdb-metadata-workflow")
        fixture_app.secret_key = fixture_secret()
        serializer = SecureCookieSessionInterface().get_signing_serializer(fixture_app)
        assert serializer is not None
        cookie = serializer.dumps({"user_email": email})
        self.opener.addheaders = [("Cookie", f"session={cookie}")]

    def request(
        self,
        path: str,
        *,
        data: bytes | None = None,
        content_type: str | None = None,
        expected_status: int = 200,
    ) -> bytes:
        headers = {"Content-Type": content_type} if content_type else {}
        if data is not None:
            headers["Origin"] = ORIGIN
        request = urllib.request.Request(
            f"{BASE_URL}{path}",
            data=data,
            headers=headers,
            method="POST" if data is not None else "GET",
        )
        try:
            response = self.opener.open(request, timeout=60)
            status = response.status
            body = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = exc.read()
        if status != expected_status:
            raise AssertionError(
                f"{path} returned HTTP {status}, expected {expected_status}: "
                f"{body.decode('utf-8', errors='replace')}"
            )
        return body

    def login(self) -> None:
        body = urllib.parse.urlencode(
            {"email": "dev@arcdb.local", "password": "arcdb-dev-123"}
        ).encode()
        self.request(
            "/login",
            data=body,
            content_type="application/x-www-form-urlencoded",
        )

    def json(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        expected_status: int = 200,
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        body = self.request(
            path,
            data=data,
            content_type="application/json" if data is not None else None,
            expected_status=expected_status,
        )
        return json.loads(body.decode("utf-8"))

    def form(self, path: str, values: dict[str, str]) -> str:
        body = urllib.parse.urlencode(values).encode()
        return self.request(
            path,
            data=body,
            content_type="application/x-www-form-urlencoded",
        ).decode("utf-8")

    def multipart(
        self,
        path: str,
        fields: dict[str, str],
        *,
        file_field: str,
        filename: str,
        file_bytes: bytes,
        cover_bytes: bytes | None = None,
    ) -> dict[str, Any]:
        boundary = "----ArchiveDBPhase2C" + uuid.uuid4().hex
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                    value.encode("utf-8"),
                    b"\r\n",
                ]
            )
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="{file_field}"; '
                    f'filename="{filename}"\r\n'
                ).encode(),
                b"Content-Type: application/epub+zip\r\n\r\n",
                file_bytes,
                b"\r\n",
            ]
        )
        if cover_bytes is not None:
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    b'Content-Disposition: form-data; name="cover"; filename="cover.png"\r\n',
                    b"Content-Type: image/png\r\n\r\n",
                    cover_bytes,
                    b"\r\n",
                ]
            )
        chunks.append(f"--{boundary}--\r\n".encode())
        body = self.request(
            path,
            data=b"".join(chunks),
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        return json.loads(body.decode("utf-8"))


def verify_metadata_parity() -> dict[str, int]:
    return verify_metadata_domains_parity(
        user_uploads_path=META_DIR / "user_uploads.json",
        custom_meta_path=META_DIR / "custom_meta.json",
        allowed_emails_path=META_DIR / "allowed_gmails.txt",
        db_path=DB_PATH,
    )


def main() -> int:
    client = ApiClient()
    client.login()

    library = client.json("/api/library", {"page": 1, "limit": 50})
    filename = next(
        str(novel["filename"])
        for novel in library["novels"]
        if novel.get("filename")
    )
    first_meta = {
        "filename": filename,
        "title_en": "Phase 2C custom metadata",
        "title_kr": "테스트",
        "author": "ArchiveDB CI",
        "cover": "",
        "tags": "Phase2C, Storage, Phase2C",
        "synopsis": "First metadata write.",
    }
    final_meta = {**first_meta, "title_en": "Phase 2C metadata updated", "synopsis": "Final metadata write."}
    client.json("/api/edit", first_meta)
    client.json("/api/edit", final_meta)
    edited_library = client.json(
        "/api/library",
        {"page": 1, "limit": 50, "search": "Phase 2C metadata updated"},
    )
    assert edited_library["total"] == 1, edited_library
    assert edited_library["novels"][0]["title_en"] == final_meta["title_en"]

    add_html = client.form(
        "/admin/access", {"action": "add", "emails": f"{TEST_EMAIL}\n{TEST_EMAIL.upper()}"}
    )
    assert "Added 1 new email" in add_html, add_html[-1000:]
    duplicate_html = client.form(
        "/admin/access", {"action": "add", "emails": TEST_EMAIL}
    )
    assert "Added 0 new email" in duplicate_html, duplicate_html[-1000:]

    fixture = min(
        (path for path in (ROOT / "data" / "batched_epubs").glob("*.epub")),
        key=lambda path: path.stat().st_size,
    )
    uploader = ApiClient()
    uploader.authenticate_fixture("reader1@arcdb.local")
    stranger = ApiClient()
    stranger.authenticate_fixture("reader2@arcdb.local")
    uploaded = uploader.multipart(
        "/api/upload_novel",
        {
            "title_en": "Phase 2C upload",
            "raw_title": "Phase 2C RAW",
            "author": "ArchiveDB CI",
            "description": "Runtime upload dual-write fixture.",
            "tags": "Phase2C, Upload",
        },
        file_field="raw_epub",
        filename="phase2c.epub",
        file_bytes=fixture.read_bytes(),
        cover_bytes=b"\x89PNG\r\n\x1a\nfixture",
    )
    upload_id = str(uploaded["id"])
    cover_url = f"/api/upload/{urllib.parse.quote(upload_id, safe='')}/asset/cover"
    uploader.request(cover_url)
    client.request(cover_url)
    stranger.request(cover_url, expected_status=404)
    pending_counts = verify_metadata_parity()
    assert pending_counts["uploads"] == 1, pending_counts

    approved_html = client.form(
        "/admin/access", {"action": "approve_upload", "upload_id": upload_id}
    )
    assert "Approved and published novel" in approved_html, approved_html[-1000:]
    approved_library = client.json(
        "/api/library", {"page": 1, "limit": 50, "search": upload_id}
    )
    assert approved_library["total"] == 1, approved_library
    assert str(approved_library["novels"][0]["id"]) == upload_id
    stranger.request(cover_url)
    duplicate_approve = client.form(
        "/admin/access", {"action": "approve_upload", "upload_id": upload_id}
    )
    assert "already approved" in duplicate_approve, duplicate_approve[-1000:]
    verify_metadata_parity()
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT approved, payload_json FROM user_uploads WHERE upload_id=?",
            (upload_id,),
        ).fetchone()
        assert row is not None and row[0] == 1, row
        assert json.loads(row[1])["status"] == "approved", row
    finally:
        conn.close()

    rejected_html = client.form(
        "/admin/access", {"action": "reject_upload", "upload_id": upload_id}
    )
    assert "Rejected and deleted upload" in rejected_html, rejected_html[-1000:]
    rejected_library = client.json(
        "/api/library", {"page": 1, "limit": 50, "search": upload_id}
    )
    assert rejected_library["total"] == 0, rejected_library
    missing_reject = client.form(
        "/admin/access", {"action": "reject_upload", "upload_id": upload_id}
    )
    assert "Upload not found" in missing_reject, missing_reject[-1000:]

    revoked_html = client.form(
        "/admin/access", {"action": "revoke", "email": TEST_EMAIL, "reason": "Phase 2C CI"}
    )
    assert "Revoked access" in revoked_html, revoked_html[-1000:]
    missing_revoke = client.form(
        "/admin/access", {"action": "revoke", "email": TEST_EMAIL}
    )
    assert "was not on the allowlist" in missing_revoke, missing_revoke[-1000:]

    final_counts = verify_metadata_parity()
    assert final_counts["uploads"] == 0, final_counts
    custom = json.loads((META_DIR / "custom_meta.json").read_text(encoding="utf-8"))
    assert custom[filename]["title_en"] == final_meta["title_en"], custom[filename]
    allowed = {
        line.strip().lower()
        for line in (META_DIR / "allowed_gmails.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert TEST_EMAIL not in allowed, allowed
    conn = sqlite3.connect(DB_PATH)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM user_uploads WHERE upload_id=?", (upload_id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM allowed_emails WHERE email=?", (TEST_EMAIL,)
        ).fetchone()[0] == 0
        row = conn.execute(
            "SELECT payload_json FROM custom_metadata WHERE filename=?", (filename,)
        ).fetchone()
        assert row is not None and json.loads(row[0])["title_en"] == final_meta["title_en"]
    finally:
        conn.close()

    print("Runtime uploads/custom-metadata/allowlist API workflow and parity passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
