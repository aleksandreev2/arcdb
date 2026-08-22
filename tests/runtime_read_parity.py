from __future__ import annotations

import http.cookiejar
import json
import os
import urllib.error
import urllib.parse
import urllib.request

CONTROL_URL = os.environ.get(
    "ARCHIVEDB_CONTROL_URL",
    os.environ.get("ARCHIVEDB_LEGACY_URL", "http://127.0.0.1:5004"),
).rstrip("/")
CANDIDATE_URL = os.environ.get(
    "ARCHIVEDB_CANDIDATE_URL",
    os.environ.get("ARCHIVEDB_SQLITE_URL", "http://127.0.0.1:5005"),
).rstrip("/")
TEST_ORIGIN = os.environ.get("ARCHIVEDB_TEST_ORIGIN", CONTROL_URL).rstrip("/")


class Client:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def request(self, path: str, *, form=None, payload=None):
        headers = {}
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            headers["Origin"] = TEST_ORIGIN
        elif payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Origin"] = TEST_ORIGIN
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers
        )
        with self.opener.open(request, timeout=30) as response:
            body = response.read()
            request_id = response.headers.get("X-Request-ID", "")
            assert len(request_id) == 32 and all(
                char in "0123456789abcdef" for char in request_id
            ), (path, request_id)
            content_type = response.headers.get_content_type()
            if content_type == "application/json":
                body = json.loads(body.decode("utf-8"))
            return response.status, content_type, body, urllib.parse.urlparse(response.geturl()).path

    def assert_origin_rejected(self, origin=None) -> None:
        headers = {"Content-Type": "application/json"}
        if origin is not None:
            headers["Origin"] = origin
        request = urllib.request.Request(
            self.base_url + "/api/library",
            data=b"{}",
            headers=headers,
            method="POST",
        )
        try:
            self.opener.open(request, timeout=30)
        except urllib.error.HTTPError as exc:
            assert exc.code == 403, exc.code
            body = json.loads(exc.read().decode("utf-8"))
            assert body == {
                "status": "error",
                "error": "Cross-origin request rejected.",
            }, body
            return
        raise AssertionError("unsafe request without an allowed origin was accepted")


def assert_same(
    control: Client, candidate: Client, path: str, *, form=None, payload=None
) -> None:
    control_result = control.request(path, form=form, payload=payload)
    candidate_result = candidate.request(path, form=form, payload=payload)
    assert control_result == candidate_result, (
        path,
        control_result[:2],
        candidate_result[:2],
    )


def main() -> int:
    email = "dev@arcdb.local"
    password = "arcdb-dev-123"
    control = Client(CONTROL_URL)
    candidate = Client(CANDIDATE_URL)

    assert_same(control, candidate, "/login", form={"email": email, "password": password})
    for client in (control, candidate):
        client.assert_origin_rejected()
        client.assert_origin_rejected("https://attacker.invalid")
    cases = (
        ("/healthz", {}),
        ("/readyz", {}),
        ("/api/library", {"payload": {"page": 1, "limit": 50}}),
        ("/api/collections", {}),
        ("/api/novel/422601", {}),
        ("/api/trending", {}),
        ("/api/community/overview", {}),
        ("/read/422601", {}),
        ("/api/read/422601/chapter/OEBPS/chapter1.xhtml", {}),
        ("/admin/access", {}),
        ("/api/admin/state-read-probe", {}),
    )
    for path, kwargs in cases:
        assert_same(control, candidate, path, **kwargs)

    for client in (control, candidate):
        status, content_type, body, _ = client.request(
            "/api/read/422601/chapter/OEBPS/chapter1.xhtml"
        )
        assert status == 200 and content_type == "text/html", (status, content_type)
        chapter = body.decode("utf-8")
        assert "Chapter 1" in chapter and "safe after void element" in chapter, chapter
        for unsafe in ("<script", "onclick", "javascript:", "<svg", "<embed"):
            assert unsafe not in chapter.casefold(), (unsafe, chapter)
    print(f"Read-backend API parity passed for {len(cases)} runtime endpoints.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
