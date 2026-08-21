from __future__ import annotations

import http.cookiejar
import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from arcdb.storage.state_parity import (
    verify_collections_parity,
    verify_user_data_parity,
)


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.environ.get("ARCHIVEDB_TEST_BASE_URL", "http://127.0.0.1:5004")
META_DIR = ROOT / "data" / "metadata"
DB_PATH = ROOT / "data" / "arcdb.sqlite3"


class ApiClient:
    def __init__(self) -> None:
        cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cookies)
        )

    def login(self, email: str, password: str) -> None:
        body = urllib.parse.urlencode({"email": email, "password": password}).encode()
        response = self.opener.open(f"{BASE_URL}/login", data=body, timeout=20)
        if response.status != 200:
            raise AssertionError(f"Login failed for {email}: HTTP {response.status}")

    def json(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        expected_status: int = 200,
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{BASE_URL}{path}",
            data=data,
            headers={"Content-Type": "application/json"} if data is not None else {},
            method="POST" if data is not None else "GET",
        )
        try:
            response = self.opener.open(request, timeout=20)
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
        return json.loads(body.decode("utf-8"))


def exercise_primary_collection_workflow() -> tuple[str, str]:
    client = ApiClient()
    client.login("dev@arcdb.local", "arcdb-dev-123")

    before = client.json("/api/collections")["collections"]
    assert before, before

    created = client.json(
        "/api/collection_create", {"name": "Phase 2B integration"}
    )["collection"]
    collection_id = created["id"]
    client.json(
        "/api/collection_create",
        {"name": "phase 2b integration"},
        expected_status=400,
    )
    client.json(
        "/api/collection_rename",
        {"id": collection_id, "name": "Phase 2B renamed"},
    )

    for _ in range(2):
        assigned = client.json(
            "/api/collection_assign",
            {"id": "900001", "collection": collection_id, "add": True},
        )["user_data"]
        assert assigned["900001"]["collections"].count(collection_id) == 1, assigned

    for _ in range(2):
        unassigned = client.json(
            "/api/collection_assign",
            {"id": "900001", "collection": collection_id, "add": False},
        )["user_data"]
        assert collection_id not in unassigned["900001"]["collections"], unassigned

    client.json(
        "/api/collection_assign",
        {"id": "900001", "collection": collection_id, "add": True},
    )
    share_id = client.json(
        "/api/community/share_collection",
        {"collection": collection_id, "message": "Phase 2B CI"},
    )["id"]
    imported = client.json(
        "/api/community/import_collection", {"share_id": share_id}
    )["collection"]
    imported_id = imported["id"]
    assert imported_id != collection_id, imported
    assert imported["count"] == 1, imported

    client.json("/api/collection_delete", {"id": collection_id})
    client.json("/api/collection_delete", {"id": collection_id})
    after_original_delete = client.json("/api/collections")["collections"]
    by_id = {item["id"]: item for item in after_original_delete}
    assert collection_id not in by_id, by_id
    assert by_id[imported_id]["count"] == 1, by_id

    client.json("/api/collection_delete", {"id": imported_id})
    after_all_delete = client.json("/api/collections")["collections"]
    remaining_ids = {item["id"] for item in after_all_delete}
    assert collection_id not in remaining_ids, remaining_ids
    assert imported_id not in remaining_ids, remaining_ids
    return collection_id, imported_id


def exercise_empty_collection_container() -> None:
    client = ApiClient()
    client.login("reader2@arcdb.local", "arcdb-dev-123")
    created = client.json(
        "/api/collection_create", {"name": "Temporary empty-container check"}
    )["collection"]
    client.json("/api/collection_delete", {"id": created["id"]})
    client.json("/api/collection_delete", {"id": created["id"]})
    assert client.json("/api/collections")["collections"] == []


def verify_final_storage(collection_id: str, imported_id: str) -> None:
    user_data_path = META_DIR / "user_data.json"
    collections_path = META_DIR / "collections.json"
    verify_user_data_parity(user_data_path=user_data_path, db_path=DB_PATH)
    verify_collections_parity(collections_path=collections_path, db_path=DB_PATH)

    legacy_collections = json.loads(collections_path.read_text(encoding="utf-8"))
    legacy_user_data = json.loads(user_data_path.read_text(encoding="utf-8"))
    assert legacy_collections["reader2@arcdb.local"] == [], legacy_collections
    for record in legacy_user_data["dev@arcdb.local"].values():
        if isinstance(record, dict):
            memberships = record.get("collections") or []
            assert collection_id not in memberships, record
            assert imported_id not in memberships, record

    conn = sqlite3.connect(DB_PATH)
    try:
        assert conn.execute(
            "SELECT 1 FROM collection_users WHERE user_email=?",
            ("reader2@arcdb.local",),
        ).fetchone()
        assert conn.execute(
            "SELECT COUNT(*) FROM collections WHERE user_email=?",
            ("reader2@arcdb.local",),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM collections WHERE collection_id IN (?, ?)",
            (collection_id, imported_id),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM collection_items WHERE collection_id IN (?, ?)",
            (collection_id, imported_id),
        ).fetchone()[0] == 0
    finally:
        conn.close()


def main() -> int:
    collection_id, imported_id = exercise_primary_collection_workflow()
    exercise_empty_collection_container()
    verify_final_storage(collection_id, imported_id)
    print("Runtime collection API workflow and full JSON/SQLite parity passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
