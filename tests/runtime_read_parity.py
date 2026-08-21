from __future__ import annotations

import http.cookiejar
import json
import os
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
        elif payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers
        )
        with self.opener.open(request, timeout=30) as response:
            body = response.read()
            content_type = response.headers.get_content_type()
            if content_type == "application/json":
                body = json.loads(body.decode("utf-8"))
            return response.status, content_type, body, urllib.parse.urlparse(response.geturl()).path


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
    cases = (
        ("/api/library", {"payload": {"page": 1, "limit": 50}}),
        ("/api/collections", {}),
        ("/api/novel/422601", {}),
        ("/api/trending", {}),
        ("/api/community/overview", {}),
        ("/read/422601", {}),
        ("/admin/access", {}),
    )
    for path, kwargs in cases:
        assert_same(control, candidate, path, **kwargs)
    print(f"Read-backend API parity passed for {len(cases)} authenticated endpoints.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
