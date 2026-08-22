from __future__ import annotations

import http.cookiejar
import json
import os
from pathlib import Path
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from flask import Flask
from flask.sessions import SecureCookieSessionInterface

from arcdb.epub_io import EpubLimits, validate_epub_archive


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.environ.get("ARCHIVEDB_TEST_BASE_URL", "http://127.0.0.1:5004")


def fixture_secret() -> str:
    for raw_line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "FLASK_SECRET_KEY":
            return value.strip().strip('"').strip("'")
    raise AssertionError("FLASK_SECRET_KEY is missing from the CI fixture environment.")


class Client:
    def __init__(self) -> None:
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def authenticate_fixture(self, email: str) -> None:
        """Use a signed CI-only session without consuming the auth rate bucket."""

        fixture_app = Flask("arcdb-epub-workflow")
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
    ) -> tuple[bytes, str]:
        headers = {"Content-Type": content_type} if content_type else {}
        if data is not None:
            headers["Origin"] = BASE_URL
        request = urllib.request.Request(
            BASE_URL + path,
            data=data,
            headers=headers,
            method="POST" if data is not None else "GET",
        )
        try:
            response = self.opener.open(request, timeout=60)
            status = response.status
            body = response.read()
            response_type = response.headers.get_content_type()
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = exc.read()
            response_type = exc.headers.get_content_type()
        if status != expected_status:
            raise AssertionError(
                f"{path} returned HTTP {status}, expected {expected_status}: "
                f"{body.decode('utf-8', errors='replace')}"
            )
        return body, response_type

    def json(
        self,
        path: str,
        payload: dict | None = None,
        *,
        expected_status: int = 200,
    ) -> dict:
        data = None if payload is None else json.dumps(payload).encode()
        body, _ = self.request(
            path,
            data=data,
            content_type="application/json" if data is not None else None,
            expected_status=expected_status,
        )
        return json.loads(body.decode())

    def multipart(
        self,
        path: str,
        *,
        mapping: dict[str, str],
        expected_status: int,
    ) -> dict:
        boundary = "----ArchiveDBEpub" + uuid.uuid4().hex
        field_name = "image_1"
        png = b"\x89PNG\r\n\x1a\nfixture"
        body = b"".join(
            (
                f"--{boundary}\r\n".encode(),
                b'Content-Disposition: form-data; name="url_mapping"\r\n\r\n',
                json.dumps(mapping).encode(),
                b"\r\n",
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="{field_name}"; '
                    'filename="image.png"\r\n'
                ).encode(),
                b"Content-Type: image/png\r\n\r\n",
                png,
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            )
        )
        response, _ = self.request(
            path,
            data=body,
            content_type=f"multipart/form-data; boundary={boundary}",
            expected_status=expected_status,
        )
        return json.loads(response.decode())


def main() -> int:
    owner = Client()
    owner.authenticate_fixture("dev@arcdb.local")
    created = owner.json(
        "/api/epub_package/init",
        {"id": "11560", "want_raw": True},
    )
    session_id = str(created["session_id"])
    assert len(session_id) == 32, created

    other_user = Client()
    other_user.authenticate_fixture("reader1@arcdb.local")
    denied = other_user.json(
        f"/api/epub_package/finalize/{session_id}",
        {},
        expected_status=404,
    )
    assert denied["error"] == "Session not found", denied

    invalid = owner.multipart(
        f"/api/epub_package/upload_batch/{session_id}",
        mapping={"image_1": "https://not-in-session.example/image.png"},
        expected_status=400,
    )
    assert "not part of this session" in invalid["error"], invalid

    queued = owner.json(
        f"/api/epub_package/finalize/{session_id}", {}, expected_status=202
    )
    assert queued["state"] in {"queued", "processing", "done"}, queued
    job_id = str(queued["job_id"])
    other_user.json(f"/api/jobs/{job_id}", expected_status=404)

    frozen = owner.multipart(
        f"/api/epub_package/upload_batch/{session_id}",
        mapping={"image_1": "https://not-in-session.example/image.png"},
        expected_status=409,
    )
    assert "already finalized" in frozen["error"], frozen

    deadline = time.monotonic() + 30
    completed = queued
    while completed["state"] not in {"done", "failed", "cancelled"}:
        if time.monotonic() >= deadline:
            raise AssertionError(f"Package job did not finish: {completed}")
        time.sleep(0.1)
        completed = owner.json(f"/api/jobs/{job_id}")
    assert completed["state"] == "done", completed
    assert completed["download_url"].endswith(session_id), completed
    epub_bytes, content_type = owner.request(completed["download_url"])
    assert content_type == "application/epub+zip", content_type

    with tempfile.TemporaryDirectory() as tmp:
        epub_path = Path(tmp) / "download.epub"
        epub_path.write_bytes(epub_bytes)
        summary = validate_epub_archive(
            epub_path,
            EpubLimits(
                max_entries=100,
                max_entry_bytes=16 * 1024 * 1024,
                max_total_uncompressed_bytes=32 * 1024 * 1024,
                max_text_entry_bytes=4 * 1024 * 1024,
            ),
        )
        assert summary.html_entry_count >= 1, summary

    owner.json("/api/epub_package/finalize/not-a-session", {}, expected_status=404)
    print("Runtime EPUB package ownership, validation and streaming workflow passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
