from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from arcdb.production_inventory import (
    InventoryError,
    collect_production_inventory,
    reconcile_production_inventory,
    resolve_inventory_paths,
    write_new_json,
)


class ProductionInventoryTests(unittest.TestCase):
    def _fixture(self, root: Path):
        app = root / "live-app"
        app.mkdir()
        (app / "gallery_app.py").write_text("print('fixture')\n", encoding="utf-8")
        templates = app / "templates"
        templates.mkdir()
        (templates / "reader.html").write_text("<main>fixture</main>\n", encoding="utf-8")
        (app / ".env").write_text("SECRET=must-not-be-hashed\n", encoding="utf-8")
        (app / ".runtime.sha256").write_text("materialization-marker\n", encoding="utf-8")
        (app / "server.log").write_text("reader@example.test\n", encoding="utf-8")
        data = app / "data"
        data.mkdir()
        (data / "payload.json").write_text('{"email":"reader@example.test"}', encoding="utf-8")

        meta = root / "private-metadata"
        meta.mkdir()
        for name in ("users.json", "user_data.json", "collections.json"):
            (meta / name).write_text("{}\n", encoding="utf-8")
        (meta / "production-only.json").write_text(
            '{"reader@example.test":"private-payload"}\n', encoding="utf-8"
        )
        translated_csv = root / "uploaded_novels_tracker.csv"
        translated_csv.write_text("id,title\n1,Fixture\n", encoding="utf-8")

        sqlite = root / "state.sqlite3"
        sqlite.write_bytes(b"SQLite fixture only")
        Path(str(sqlite) + "-wal").write_bytes(b"wal")

        chapters = root / "chapters"
        chapters.mkdir()
        (chapters / "chapter-1.html").write_text("fixture", encoding="utf-8")
        epubs = root / "epubs"
        epubs.mkdir()
        (epubs / "book.epub").write_bytes(b"fixture epub")

        unit = root / "arcdb-web.service"
        unit.write_text(
            "[Service]\n"
            "User=private-service-user\n"
            f"WorkingDirectory={app}\n"
            "Environment=FLASK_SECRET_KEY=private-secret STATE_READ_BACKEND=legacy\n"
            f"EnvironmentFile={root / 'private.env'}\n"
            "ExecStart=/venv/bin/gunicorn --workers 2 --threads=4 --timeout 90 "
            "--bind unix:/run/private-arcdb.sock gallery_app:app\n"
            "Restart=always\n",
            encoding="utf-8",
        )
        cloudflared = root / "cloudflared.yml"
        cloudflared.write_text(
            "tunnel: 11111111-2222-3333-4444-555555555555\n"
            "credentials-file: /private/credentials.json\n"
            "token: private-cloudflare-token\n"
            "ingress:\n"
            "  - hostname: private.example.test\n"
            "    service: http://127.0.0.1:5004\n"
            "  - service: http_status:404\n",
            encoding="utf-8",
        )
        mountinfo = root / "mountinfo"
        mount_point = root.anchor.rstrip("\\/") or "/"
        if root.anchor:
            mount_point += "\\"
        mountinfo.write_text(
            f"36 25 8:1 / {mount_point} rw,relatime - ext4 /dev/volume rw\n",
            encoding="utf-8",
        )
        return (
            app,
            meta,
            translated_csv,
            sqlite,
            chapters,
            epubs,
            unit,
            cloudflared,
            mountinfo,
        )

    def test_inventory_is_read_only_complete_and_public_report_is_path_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (
                app,
                meta,
                translated_csv,
                sqlite,
                chapters,
                epubs,
                unit,
                cloudflared,
                mountinfo,
            ) = self._fixture(root)
            before = {
                path: path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            paths = resolve_inventory_paths(
                app_root=app,
                meta_dir=meta,
                sqlite_db=sqlite,
                metadata_files=(("translated_csv", translated_csv),),
                content_roots=(("chapters", chapters), ("epubs", epubs)),
                systemd_units=(unit,),
                cloudflared_config=cloudflared,
                mountinfo_path=mountinfo,
            )

            private, public = collect_production_inventory(
                paths, source_revision="production-revision-label"
            )

            after = {
                path: path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)
            self.assertEqual(private["status"], "inventory_collected")
            self.assertEqual(public["status"], "inventory_collected")
            self.assertEqual(public["storage"]["metadata"]["unknown_file_count"], 1)
            self.assertTrue(
                public["storage"]["metadata"]["configured_files"]["translated_csv"][
                    "exists"
                ]
            )
            self.assertTrue(public["storage"]["sqlite"]["sidecars"]["wal"])
            self.assertEqual(public["runtime"]["gunicorn"][0]["workers"], 2)
            self.assertEqual(public["runtime"]["gunicorn"][0]["threads"], 4)
            self.assertEqual(public["runtime"]["gunicorn"][0]["bind_type"], "unix")
            self.assertNotIn("bind", public["runtime"]["gunicorn"][0])
            self.assertFalse(public["decision"]["readiness_preflight_authorized"])

            self.assertFalse(public["decision"]["bounded_canary_authorized"])
            self.assertFalse(public["decision"]["primary_read_authorized"])

            source_names = {item["relative_path"] for item in private["source"]["files"]}
            self.assertEqual(source_names, {"gallery_app.py", "templates/reader.html"})
            serialized = json.dumps(public, ensure_ascii=False)
            for forbidden in (
                str(root),
                "private-service-user",
                "reader@example.test",
                "private-payload",
                "private-secret",
                "private-cloudflare-token",
                "private.example.test",
                "11111111-2222-3333-4444-555555555555",
                "production-only.json",
                "private-arcdb.sock",
                "production-revision-label",
            ):
                self.assertNotIn(forbidden, serialized)
            self.assertNotIn('"token"', serialized)

    def test_reconciliation_reports_exact_private_diff_and_only_counts_publicly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (
                app,
                meta,
                translated_csv,
                sqlite,
                chapters,
                epubs,
                unit,
                cloudflared,
                mountinfo,
            ) = self._fixture(root)
            inventory, _ = collect_production_inventory(
                resolve_inventory_paths(
                    app_root=app,
                    meta_dir=meta,
                    sqlite_db=sqlite,
                    metadata_files=(("translated_csv", translated_csv),),
                    content_roots=(("chapters", chapters), ("epubs", epubs)),
                    systemd_units=(unit,),
                    cloudflared_config=cloudflared,
                    mountinfo_path=mountinfo,
                )
            )

            reference = root / "reference"
            reference.mkdir()
            (reference / "gallery_app.py").write_text("print('changed')\n", encoding="utf-8")
            (reference / "expected.py").write_text("expected\n", encoding="utf-8")

            private, public = reconcile_production_inventory(inventory, reference)

            self.assertFalse(public["source"]["matches_reference"])
            self.assertEqual(public["source"]["missing_file_count"], 1)
            self.assertEqual(public["source"]["changed_file_count"], 1)
            self.assertEqual(public["source"]["unknown_file_count"], 1)
            self.assertEqual(private["source_diff"]["missing_from_production"], ["expected.py"])
            self.assertEqual(private["source_diff"]["changed_in_production"], ["gallery_app.py"])
            self.assertEqual(
                private["source_diff"]["unknown_in_production"],
                ["templates/reader.html"],
            )
            self.assertEqual(private["metadata_review"]["unknown_files"], ["production-only.json"])
            serialized = json.dumps(public, ensure_ascii=False)
            self.assertNotIn(str(root), serialized)
            self.assertNotIn("expected.py", serialized)
            self.assertNotIn("gallery_app.py", serialized)
            self.assertNotIn("production-only.json", serialized)
            self.assertFalse(public["decision"]["readiness_preflight_authorized"])

            tampered = copy.deepcopy(inventory)
            tampered["source"]["files"][0]["sha256"] = "0" * 64
            with self.assertRaises(InventoryError) as mismatch:
                reconcile_production_inventory(tampered, reference)
            self.assertEqual(
                mismatch.exception.code, "inventory_checksum_mismatch"
            )

    def test_matching_source_still_requires_review_for_unknown_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (
                app,
                meta,
                translated_csv,
                sqlite,
                chapters,
                epubs,
                unit,
                cloudflared,
                mountinfo,
            ) = self._fixture(root)
            inventory, _ = collect_production_inventory(
                resolve_inventory_paths(
                    app_root=app,
                    meta_dir=meta,
                    sqlite_db=sqlite,
                    metadata_files=(("translated_csv", translated_csv),),
                    content_roots=(("chapters", chapters), ("epubs", epubs)),
                    systemd_units=(unit,),
                    cloudflared_config=cloudflared,
                    mountinfo_path=mountinfo,
                )
            )
            _, public = reconcile_production_inventory(inventory, app)
            self.assertTrue(public["source"]["matches_reference"])
            self.assertTrue(public["decision"]["operator_review_required"])

    def test_reports_are_new_file_only_and_content_labels_are_validated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "report.json"
            write_new_json(report, {"ok": True}, private=True)
            self.assertEqual(json.loads(report.read_text(encoding="utf-8")), {"ok": True})
            with self.assertRaises(InventoryError) as overwrite:
                write_new_json(report, {"ok": False}, private=True)
            self.assertEqual(overwrite.exception.code, "report_exists")

            with self.assertRaises(InventoryError) as duplicate:
                resolve_inventory_paths(
                    app_root=root,
                    meta_dir=root,
                    sqlite_db=root / "db",
                    content_roots=(("epubs", root), ("epubs", root)),
                )
            self.assertEqual(duplicate.exception.code, "duplicate_content_label")

            with self.assertRaises(InventoryError) as invalid:
                resolve_inventory_paths(
                    app_root=root,
                    meta_dir=root,
                    sqlite_db=root / "db",
                    content_roots=(("operator-name", root),),
                )
            self.assertEqual(invalid.exception.code, "invalid_content_label")

            with self.assertRaises(InventoryError) as invalid_metadata:
                resolve_inventory_paths(
                    app_root=root,
                    meta_dir=root,
                    sqlite_db=root / "db",
                    metadata_files=(("operator-name", root / "state"),),
                )
            self.assertEqual(
                invalid_metadata.exception.code, "invalid_metadata_label"
            )

            app = root / "app"
            app.mkdir()
            (app / "app.py").write_text("pass\n", encoding="utf-8")
            meta = root / "metadata"
            meta.mkdir()
            configured = meta / "tracker.csv"
            configured.write_text("id,title\n", encoding="utf-8")
            _, public = collect_production_inventory(
                resolve_inventory_paths(
                    app_root=app,
                    meta_dir=meta,
                    sqlite_db=root / "missing.sqlite3",
                    metadata_files=(("translated_csv", configured),),
                    mountinfo_path=root / "missing-mountinfo",
                )
            )
            self.assertEqual(public["storage"]["metadata"]["file_count"], 1)
            self.assertEqual(
                public["storage"]["metadata"]["unknown_file_count"], 0
            )


if __name__ == "__main__":
    unittest.main()
