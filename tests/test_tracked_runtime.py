from __future__ import annotations

import hashlib
import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRACKED_APP = ROOT / "arcdb" / "app.py"
TRACKED_TEMPLATES = ROOT / "arcdb" / "templates"
BOOTSTRAP_PATH = ROOT / "scripts" / "dev_bootstrap.py"
WORKFLOW_DIR = ROOT / ".github" / "workflows"


def _load_bootstrap_module():
    spec = importlib.util.spec_from_file_location("arcdb_dev_bootstrap", BOOTSTRAP_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load dev bootstrap module.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TrackedRuntimeSourceTests(unittest.TestCase):
    def test_bootstrap_uses_tracked_source_without_materialization(self) -> None:
        bootstrap = _load_bootstrap_module()
        self.assertEqual(bootstrap.ensure_source(), TRACKED_APP)

        bootstrap_text = BOOTSTRAP_PATH.read_text(encoding="utf-8")
        self.assertNotIn("materialize_baseline", bootstrap_text)
        self.assertNotIn("runtime_overlays", bootstrap_text)
        self.assertNotIn(".runtime", bootstrap_text)

    def test_required_runtime_files_are_tracked(self) -> None:
        self.assertTrue(TRACKED_APP.is_file())
        self.assertTrue((ROOT / "arcdb" / "jobs.py").is_file())
        self.assertTrue((ROOT / "arcdb" / "library_index.py").is_file())
        self.assertTrue((ROOT / "arcdb" / "path_security.py").is_file())
        self.assertTrue((ROOT / "arcdb" / "package_worker.py").is_file())
        self.assertTrue((ROOT / "scripts" / "run_packager.py").is_file())
        self.assertTrue((ROOT / "arcdb" / "telegram_gateway.py").is_file())
        self.assertTrue((ROOT / "arcdb" / "telegram_service.py").is_file())
        self.assertTrue((ROOT / "scripts" / "run_telegram.py").is_file())
        self.assertTrue((ROOT / "scripts" / "reindex_library.py").is_file())
        self.assertTrue((ROOT / "scripts" / "benchmark_library_index.py").is_file())
        self.assertTrue(
            (ROOT / "deploy" / "systemd" / "arcdb-packager.service.example").is_file()
        )
        self.assertTrue(
            (ROOT / "deploy" / "systemd" / "arcdb-telegram.service.example").is_file()
        )
        expected_templates = {
            "community.html",
            "forgot_password.html",
            "gallery.html",
            "login.html",
            "reader.html",
            "register.html",
            "reset_password.html",
            "verify.html",
        }
        self.assertEqual(
            {path.name for path in TRACKED_TEMPLATES.glob("*.html")},
            expected_templates,
        )

    def test_runtime_workflows_do_not_launch_materialized_source(self) -> None:
        workflows = {
            path.name: path.read_text(encoding="utf-8")
            for path in WORKFLOW_DIR.glob("*.yml")
        }
        combined = "\n".join(workflows.values())
        self.assertNotIn(".runtime/source/gallery_app.py", combined)
        self.assertNotIn("tests/test_runtime_overlays.py", combined)
        self.assertIn("arcdb/app.py", workflows["read-backend-parity.yml"])
        self.assertIn("tests/test_tracked_runtime.py", combined)

    def test_runtime_state_routes_remain_wired(self) -> None:
        text = TRACKED_APP.read_text(encoding="utf-8")
        expected = (
            'shadow_reason="user_status"',
            'shadow_reason="bulk_remove"',
            'shadow_reason="user_progress"',
            'shadow_reason="user_hide"',
            'shadow_reason="local_download"',
            'shadow_reason="telegram_download"',
            'shadow_reason="collection_create"',
            'shadow_reason="collection_rename"',
            'shadow_reason="collection_delete"',
            'shadow_reason="collection_delete_memberships"',
            'shadow_reason="collection_assign"',
            '"community_import_collection"',
            'shadow_reason="community_import_memberships"',
            'shadow_reason="upload_create"',
            'shadow_reason="upload_approve"',
            'shadow_reason="upload_reject"',
            'shadow_reason="auth_register"',
            'shadow_reason="auth_verify"',
            'shadow_reason="auth_reset_request"',
            'shadow_reason="auth_password_reset"',
            '"allowlist_add"',
            '"allowlist_remove"',
            "from arcdb.storage.runtime_state import mirror_user_changes",
            "from arcdb.storage.runtime_state import mirror_collection_user",
            "from arcdb.storage.runtime_state import mirror_upload_changes",
            "from arcdb.storage.runtime_state import mirror_custom_metadata_entry",
            "from arcdb.storage.runtime_state import mirror_allowed_emails",
            "from arcdb.storage.runtime_state import mirror_auth_users_changes",
            "ARCHIVEDB_AUTH_TEST_MODE",
            "from arcdb.storage.runtime_reads import read_users",
            "from arcdb.storage.runtime_reads import read_user_data",
            "from arcdb.storage.runtime_reads import read_collections",
            "from arcdb.storage.runtime_reads import read_user_uploads",
            "from arcdb.storage.runtime_reads import read_custom_meta",
            "from arcdb.storage.runtime_reads import read_allowed_emails",
            "mirror_allowed_emails(_get_allowed_emails_legacy()",
            "arcdb_legacy_write_succeeded",
        )
        for marker in expected:
            self.assertIn(marker, text)
        self.assertIn("STATE_DUAL_WRITE_STRICT", text)
        self.assertNotIn("save_user_data(all_udata)", text)
        probe_start = text.index("def api_admin_state_read_probe")
        probe_end = text.index('@app.route("/api/user_status"', probe_start)
        probe = text[probe_start:probe_end]
        for reader in (
            "load_users()",
            "load_user_data()",
            "load_collections()",
            "load_user_uploads()",
            "load_custom_meta()",
            "get_allowed_emails()",
        ):
            self.assertIn(reader, probe)
        self.assertNotIn("len(", probe)

    def test_runtime_uses_bounded_epub_io_paths(self) -> None:
        text = TRACKED_APP.read_text(encoding="utf-8")
        worker = (ROOT / "arcdb" / "package_worker.py").read_text(encoding="utf-8")
        self.assertIn("copy_upload_limited(storage.stream", text)
        self.assertNotIn("package_epub_streaming(", text)
        self.assertIn("package_epub_streaming(", worker)
        self.assertNotIn("arcdb.app", worker)
        self.assertIn("return _enqueue_epub_package_job(session_id)", text)
        self.assertIn('return jsonify(response), 202', text)
        self.assertIn("_load_owned_epub_session(session_id)", text)
        self.assertIn("copy_zip_entry_atomic(", text)
        self.assertNotIn("all_entries =", text)
        self.assertNotIn("file_storage.save(", text)
        self.assertNotIn("out.write(z.read(", text)
        gallery = (ROOT / "arcdb" / "templates" / "gallery.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("waitForPackageJob", gallery)
        self.assertIn("/api/jobs/${activeJobId}/cancel", gallery)

    def test_reader_uses_parser_allowlist_sanitizer(self) -> None:
        text = TRACKED_APP.read_text(encoding="utf-8")
        sanitizer_path = ROOT / "arcdb" / "html_sanitizer.py"
        sanitizer = sanitizer_path.read_text(encoding="utf-8")
        self.assertIn("from html.parser import HTMLParser", sanitizer)
        self.assertIn("ALLOWED_TAGS = frozenset", sanitizer)
        self.assertIn("ALLOWED_URL_SCHEMES = frozenset", sanitizer)
        self.assertIn("from arcdb.html_sanitizer import sanitize_epub_html", text)
        self.assertIn("return sanitize_epub_html(content)", text)
        for obsolete in (
            "_SCRIPT_BLOCK_RE",
            "_EVENT_ATTR_RE",
            "_JS_URL_RE",
            "def sanitize_chapter_html",
        ):
            self.assertNotIn(obsolete, text)

    def test_web_health_readiness_and_request_timing_are_sanitized(self) -> None:
        text = TRACKED_APP.read_text(encoding="utf-8")
        for marker in (
            '@app.route("/healthz", methods=["GET"])',
            '@app.route("/readyz", methods=["GET"])',
            "LIBRARY_INDEX.check_ready()",
            "check_state_read_backend_ready()",
            "request_id=",
            "route=",
            "method=",
            "status=",
            "duration_ms=",
            'response.headers.setdefault("X-Request-ID", request_id)',
        ):
            self.assertIn(marker, text)
        log_start = text.index("def log_request")
        log_end = text.index("def _content_security_policy", log_start)
        request_log = text[log_start:log_end]
        self.assertNotIn('session.get("user_email"', request_log)
        self.assertNotIn("get_client_ip()", request_log)

    def test_script_csp_uses_nonces_and_blocks_inline_attributes(self) -> None:
        text = TRACKED_APP.read_text(encoding="utf-8")
        self.assertIn("g.csp_nonce = secrets.token_urlsafe(24)", text)
        self.assertIn("script-src 'self' 'nonce-", text)
        self.assertIn("script-src-attr 'none'", text)
        self.assertNotIn("https://cdnjs.cloudflare.com", text)

        templates = {
            path.name: path.read_text(encoding="utf-8")
            for path in TRACKED_TEMPLATES.glob("*.html")
        }
        combined = "\n".join(templates.values())
        script_tags = re.findall(r"<script\b[^>]*>", combined, re.IGNORECASE)
        self.assertTrue(script_tags)
        self.assertTrue(all('nonce="{{ g.csp_nonce }}"' in tag for tag in script_tags))
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", combined, re.IGNORECASE))
        self.assertNotIn("JSZip", combined)
        self.assertNotIn("cdnjs.cloudflare.com", combined)

        auth_css = ROOT / "arcdb" / "static" / "css" / "auth.css"
        version = hashlib.sha256(auth_css.read_bytes()).hexdigest()[:16]
        expected_link = f'/static/css/auth.css?v={version}'
        for name in (
            "login.html", "register.html", "verify.html",
            "forgot_password.html", "reset_password.html",
        ):
            self.assertIn(expected_link, templates[name])
            self.assertNotIn("<style>", templates[name])

    def test_state_changes_are_centrally_origin_protected(self) -> None:
        text = TRACKED_APP.read_text(encoding="utf-8")
        self.assertIn("def enforce_state_change_origin", text)
        self.assertIn('request.method not in {"POST", "PUT", "PATCH", "DELETE"}', text)
        self.assertIn("request_source_allowed(", text)
        self.assertIn('@app.route("/logout", methods=["POST"])', text)
        gallery = (ROOT / "arcdb" / "templates" / "gallery.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('<form action="/logout" method="post"', gallery)
        self.assertNotIn('href="/logout"', gallery)
        dual_write = (ROOT / ".github" / "workflows" / "runtime-dual-write.yml").read_text(
            encoding="utf-8"
        )
        self.assertGreaterEqual(
            dual_write.count("Origin: http://127.0.0.1:5004"), 5
        )
        for workflow in (
            "runtime_auth_workflow.py",
            "runtime_collection_workflow.py",
            "runtime_epub_workflow.py",
            "runtime_metadata_workflow.py",
            "runtime_read_parity.py",
        ):
            runtime = (ROOT / "tests" / workflow).read_text(encoding="utf-8")
            self.assertIn("ARCHIVEDB_TEST_ORIGIN", runtime)

    def test_upload_assets_and_stored_paths_fail_closed(self) -> None:
        text = TRACKED_APP.read_text(encoding="utf-8")
        self.assertIn("from arcdb.path_security import confined_child, confined_path", text)
        cover_start = text.index("def api_uploaded_cover")
        cover_end = text.index("# 12. ROUTES - COMMUNITY", cover_start)
        cover_route = text[cover_start:cover_end]
        self.assertIn('record.get("approved")', cover_route)
        self.assertIn('record.get("uploader_email")', cover_route)
        self.assertIn("current_user not in ADMIN_EMAILS", cover_route)
        self.assertIn("confined_path(", cover_route)

        download_start = text.index("def download_file")
        download_end = text.index("# 12.1 CLIENT-FETCHED", download_start)
        download_route = text[download_start:download_end]
        self.assertIn("confined_path(local_path, root)", download_route)
        self.assertNotIn("local_path.startswith", download_route)

    def test_library_and_reader_runtime_paths_use_persistent_index(self) -> None:
        text = TRACKED_APP.read_text(encoding="utf-8")

        def function_slice(name: str, next_marker: str) -> str:
            start = text.index(f"def {name}")
            end = text.index(next_marker, start)
            return text[start:end]

        load_slice = function_slice("load_gallery_data", "def find_novel")
        self.assertIn("LIBRARY_INDEX.all_items()", load_slice)
        self.assertNotIn("_build_gallery_items", load_slice)

        find_slice = function_slice("find_novel", "def novel_key")
        self.assertIn("LIBRARY_INDEX.lookup", find_slice)
        self.assertNotIn("for novel in", find_slice)

        api_slice = function_slice("api_library", "def _clamped_limit")
        self.assertIn("LIBRARY_INDEX.query", api_slice)
        self.assertNotIn("load_gallery_data", api_slice)
        self.assertNotIn("os.walk", api_slice)

        self.assertIn("return LIBRARY_INDEX.chapters(novel_key(novel))[0]", text)
        self.assertIn("return LIBRARY_INDEX.images(novel_key(novel))", text)
        self.assertEqual(text.count("_build_gallery_items()"), 2)
        self.assertIn(
            "LIBRARY_INDEX.rebuild(\n        _build_gallery_items(),",
            text,
        )


if __name__ == "__main__":
    unittest.main()
